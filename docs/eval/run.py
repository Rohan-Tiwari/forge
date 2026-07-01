"""Forge evaluation runner.

Runs a JSONL dataset of utterances against `forge run`, captures mechanical
metrics per task, and writes per-run JSON + a consolidated results.csv.

Design principles:

- **Isolation.** Every task runs in a fresh temp-copy of the reference
  workspace so edit tasks don't pollute later ones.
- **Instrumentation via audit log.** We read the newly-appended entries
  in `<workspace>/.forge/audit.jsonl` for THIS session and derive
  cells_run, model_calls, harmony_recoveries, gate first-pass rate,
  and robustness signals.
- **Pluggable judge.** The `Judge` protocol takes (prompt, reference,
  final_text) and returns a Correctness score in [0.0, 1.0]. The default
  `NoOpJudge` returns None; wire in a real Claude/Ollama judge later.
- **Fail-open on individual tasks.** A single task that times out or
  errors is recorded with `error=<message>`, other tasks continue.

Usage:
    python docs/eval/run.py --dataset docs/eval/dataset.jsonl \\
                            --workspace-src docs/eval/workspace \\
                            --out docs/eval/runs \\
                            --results docs/eval/results.csv \\
                            [--timeout-s 300] [--limit N]

The `--limit N` flag runs only the first N tasks — useful for smoke tests
before committing to the full ~90-min run.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Protocol


# ---------------------------------------------------------------------------
# Judge interface (no implementation today; wire in later with real key).
# ---------------------------------------------------------------------------


class Judge(Protocol):
    """Score a Forge reply against a reference answer.

    Returns a float in [0.0, 1.0] or None if unable to judge (e.g., no
    API key, judge model unavailable). None values are recorded as
    `correctness=null` in the results — they don't fail the task.
    """

    def score(self, *, prompt: str, reference: str, final_text: str) -> float | None:
        ...


class NoOpJudge:
    """Default judge — always returns None. Wire in a real judge later."""

    def score(self, *, prompt: str, reference: str, final_text: str) -> float | None:
        return None


# ---------------------------------------------------------------------------
# Per-task record types
# ---------------------------------------------------------------------------


@dataclass
class TaskResult:
    """Everything measured for a single task."""

    # Task-level identity
    id: str
    category: str
    prompt: str
    reference_answer: str
    ideal_cells: int
    source: str

    # Timing
    wall_clock_s: float = 0.0
    time_to_first_cell_s: float | None = None

    # Loop counts
    cells_run: int = 0
    model_calls: int = 0
    harmony_recoveries: int = 0
    format_retries: int = 0
    empty_retries: int = 0
    escalations: int = 0

    # Output
    final_text: str = ""
    finish_kind: str = ""     # "prose" | "max_cells" | "empty" | "format_failure" | "error" | ...

    # Robustness
    robust: bool = False       # True iff finish_kind == "prose" AND no unrecovered kernel/model errors
    kernel_wedged: bool = False
    exit_code: int | None = None
    error: str = ""

    # Optional judge score
    correctness: float | None = None

    # Ratio derived after the fact
    cell_efficiency: float | None = None    # cells_run / ideal_cells


# ---------------------------------------------------------------------------
# Audit log parsing
# ---------------------------------------------------------------------------


def parse_audit(audit_path: Path, session_id: str, task_start_wall: float) -> dict:
    """Extract all metrics we care about from audit.jsonl for one session.

    audit_path: absolute path to the workspace's .forge/audit.jsonl
    session_id: session identifier prefixed onto every entry
    task_start_wall: time.time() at task start, used to bound the search
                     to fresh entries only (guard against stale sessions)
    """
    metrics = {
        "cells_run": 0,
        "model_calls": 0,
        "harmony_recoveries": 0,
        "format_retries": 0,
        "empty_retries": 0,
        "escalations": 0,
        "kernel_wedged": False,
        "finish_kind": "",
        "final_text": "",
        "time_to_first_cell_s": None,
        "session_start_t": None,
    }
    if not audit_path.exists():
        return metrics

    session_start_iso: str | None = None
    with open(audit_path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("session") != session_id:
                continue

            kind = r.get("kind", "")

            if kind == "session.start":
                session_start_iso = r.get("t")
            elif kind == "cell.exec":
                metrics["cells_run"] += 1
                # Time-to-first-cell — only set on the first observation
                if metrics["time_to_first_cell_s"] is None and session_start_iso and r.get("t"):
                    metrics["time_to_first_cell_s"] = _iso_delta(session_start_iso, r["t"])
            elif kind == "model.complete":
                metrics["model_calls"] += 1
                if r.get("finish_reason") == "tool_call_parse_recovered":
                    metrics["harmony_recoveries"] += 1
            elif kind == "recovery.tool_call_parse_retry":
                # counted via model.complete finish_reason; leave as-is
                pass
            elif kind == "recovery.empty_content_retry":
                metrics["empty_retries"] += 1
            elif kind == "kernel.wedged":
                metrics["kernel_wedged"] = True
            elif kind.startswith("turn.end."):
                # turn.end.prose | turn.end.max_cells | turn.end.empty |
                # turn.end.format_failure | turn.end.tool_call_parse_unrecovered
                metrics["finish_kind"] = kind[len("turn.end."):]

    return metrics


def _iso_delta(a: str, b: str) -> float:
    """Seconds between two ISO-8601 timestamps in audit.jsonl."""
    from datetime import datetime
    # audit.jsonl uses ...Z suffix; Python <3.11 needs the +00:00 form
    def parse(s: str):
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    return (parse(b) - parse(a)).total_seconds()


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


def run_task(
    *,
    task: dict,
    workspace_src: Path,
    forge_bin: str,
    timeout_s: float,
    judge: Judge,
) -> TaskResult:
    """Copy the reference workspace to a fresh temp dir, run one utterance
    against `forge run` there, and collect metrics.
    """
    result = TaskResult(
        id=task["id"],
        category=task["category"],
        prompt=task["prompt"],
        reference_answer=task["reference_answer"],
        ideal_cells=int(task["ideal_cells"]),
        source=task.get("source", ""),
    )

    with tempfile.TemporaryDirectory(prefix=f"forge-eval-{task['id']}-") as tmp:
        ws = Path(tmp) / "workspace"
        shutil.copytree(workspace_src, ws)

        env = os.environ.copy()
        # Force non-interactive mode so nothing prompts for input
        env["FORGE_MODE"] = "auto"
        # Redirect forge home to the temp dir so state doesn't leak
        env["FORGE_HOME"] = str(Path(tmp) / ".forge-home")
        # Ollama gets restarted if needed but we don't touch it
        env.setdefault("FORGE_KEEP_ALIVE", "24h")

        cmd = [forge_bin, "run", "--auto", task["prompt"]]
        t0 = time.monotonic()
        wall_start_epoch = time.time()

        try:
            proc = subprocess.run(
                cmd,
                cwd=ws,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            result.wall_clock_s = time.monotonic() - t0
            result.exit_code = proc.returncode
            result.final_text = _extract_reply(proc.stdout)

            # Instrument via audit log
            audit_path = ws / ".forge" / "audit.jsonl"
            session_id = _find_session_id(audit_path, wall_start_epoch)
            if session_id:
                m = parse_audit(audit_path, session_id, wall_start_epoch)
                result.cells_run = m["cells_run"]
                result.model_calls = m["model_calls"]
                result.harmony_recoveries = m["harmony_recoveries"]
                result.empty_retries = m["empty_retries"]
                result.finish_kind = m["finish_kind"] or (
                    "prose" if result.exit_code == 0 else "error"
                )
                result.time_to_first_cell_s = m["time_to_first_cell_s"]
                result.kernel_wedged = m["kernel_wedged"]

            # Robust if we ended in prose AND no wedge AND exit_code 0
            result.robust = (
                result.finish_kind == "prose"
                and not result.kernel_wedged
                and result.exit_code == 0
            )

            # Cell efficiency: cells_run / ideal_cells. 1.0 = perfect,
            # <1 = fewer than ideal (unlikely), >1 = wasteful. Cap at
            # ideal_cells * 8 for readability.
            if result.ideal_cells > 0 and result.cells_run > 0:
                result.cell_efficiency = result.cells_run / result.ideal_cells

            # Optional judge
            if result.final_text:
                try:
                    result.correctness = judge.score(
                        prompt=task["prompt"],
                        reference=task["reference_answer"],
                        final_text=result.final_text,
                    )
                except Exception as e:   # noqa: BLE001
                    result.error = f"judge error: {e}"

        except subprocess.TimeoutExpired:
            result.wall_clock_s = timeout_s
            result.finish_kind = "timeout"
            result.error = f"timeout after {timeout_s}s"
            result.exit_code = -1
        except Exception as e:   # noqa: BLE001
            result.wall_clock_s = time.monotonic() - t0
            result.finish_kind = "error"
            result.error = str(e)
            result.exit_code = -2

    return result


def _find_session_id(audit_path: Path, since_epoch: float) -> str | None:
    """Find the newest session.start entry after `since_epoch` in the audit.

    We match by timestamp being AFTER task start; there should be exactly
    one fresh session per task since we use a temp forge home.
    """
    if not audit_path.exists():
        return None
    from datetime import datetime, timezone
    since_iso = datetime.fromtimestamp(since_epoch, tz=timezone.utc).isoformat()
    candidates: list[tuple[str, str]] = []
    with open(audit_path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("kind") == "session.start" and r.get("t", "") >= since_iso[:-6]:
                candidates.append((r["t"], r.get("session", "")))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def _extract_reply(stdout: str) -> str:
    """Pull the prose reply text out of forge's stdout.

    forge renders replies inside a rich box like:
        ╭─── reply ───╮
        │ text        │
        ╰─────────────╯
    We grab the lines between those borders. If no box found, return the
    full stdout (a failure case worth seeing raw).
    """
    lines = stdout.splitlines()
    reply_lines: list[str] = []
    in_reply = False
    for line in lines:
        if "reply" in line and "╭" in line:
            in_reply = True
            continue
        if in_reply and "╰" in line:
            in_reply = False
            continue
        if in_reply and line.startswith("│"):
            # Strip leading │ and trailing │ plus padding
            inner = line.strip("│ ").rstrip()
            reply_lines.append(inner)
    if reply_lines:
        return " ".join(reply_lines).strip()
    return stdout.strip()[-500:]     # last 500 chars as fallback


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", required=True, help="Path to dataset.jsonl")
    p.add_argument("--workspace-src", required=True,
                   help="Reference workspace to copy per task")
    p.add_argument("--out", required=True, help="Dir for per-task JSON")
    p.add_argument("--results", required=True, help="Consolidated CSV path")
    p.add_argument("--timeout-s", type=float, default=300.0,
                   help="Hard timeout per task (default 300s)")
    p.add_argument("--limit", type=int, default=None,
                   help="Run only the first N tasks (smoke mode)")
    p.add_argument("--forge-bin", default="forge",
                   help="`forge` binary to use (default: `forge` on PATH)")
    args = p.parse_args()

    dataset_path = Path(args.dataset).resolve()
    workspace_src = Path(args.workspace_src).resolve()
    out_dir = Path(args.out).resolve()
    results_csv = Path(args.results).resolve()

    out_dir.mkdir(parents=True, exist_ok=True)

    tasks = []
    with open(dataset_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            tasks.append(json.loads(line))

    if args.limit:
        tasks = tasks[: args.limit]

    print(f"Loaded {len(tasks)} tasks. Timeout per task: {args.timeout_s}s.")
    print(f"Reference workspace: {workspace_src}")
    print(f"Per-task JSON: {out_dir}")
    print(f"Results CSV: {results_csv}")
    print("-" * 72)

    judge = NoOpJudge()

    all_results: list[TaskResult] = []
    total_start = time.monotonic()

    for i, task in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] {task['id']} ({task['category']})  ", end="", flush=True)
        r = run_task(
            task=task,
            workspace_src=workspace_src,
            forge_bin=args.forge_bin,
            timeout_s=args.timeout_s,
            judge=judge,
        )
        all_results.append(r)

        # Write per-task JSON
        out_path = out_dir / f"{r.id}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(asdict(r), f, indent=2, default=str)

        # Terse summary line
        eff = f"{r.cell_efficiency:.1f}x" if r.cell_efficiency else "-"
        finish = r.finish_kind or "?"
        print(
            f"{r.wall_clock_s:6.1f}s  cells={r.cells_run}/{r.ideal_cells} ({eff})  "
            f"finish={finish}  recov={r.harmony_recoveries}"
        )

    total_elapsed = time.monotonic() - total_start

    # Write consolidated CSV
    fieldnames = list(asdict(all_results[0]).keys()) if all_results else []
    with open(results_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in all_results:
            w.writerow({k: v for k, v in asdict(r).items()})

    # Rollup
    print("-" * 72)
    print(f"Wrote {len(all_results)} results in {total_elapsed:.1f}s")
    print(f"CSV: {results_csv}")

    robust = sum(1 for r in all_results if r.robust)
    print(f"Robust (ended in prose, no wedge, exit=0): {robust}/{len(all_results)}")
    total_recoveries = sum(r.harmony_recoveries for r in all_results)
    print(f"Total harmony recoveries: {total_recoveries}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

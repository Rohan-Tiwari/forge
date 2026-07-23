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
from dataclasses import asdict, dataclass
from datetime import UTC
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


class ForgeRouterJudge:
    """LLM-as-judge (I1) using forge's own ModelRouter.

    Scores a reply against the reference answer with a fixed rubric at low
    temperature. Defaults to the local gpt-oss driver (zero cost); pass a
    model id like "claude-sonnet-4-6" to escalate (requires the key). The
    judge model + rubric version are recorded so scores are auditable and
    re-scorable later.

    Returns a float in [0, 1], or None if the judge call fails (recorded as
    correctness=null — never fails the task).
    """

    RUBRIC_VERSION = "v1"

    def __init__(self, *, model: str | None = None):
        # Import lazily so the eval harness has no hard dependency on the
        # installed forge package unless a real judge is requested.
        from forge.router import ModelRouter, RoleConfig
        self.model = model
        self.router = ModelRouter()
        if model:
            self.router.roles["judge"] = RoleConfig(primary=model, effort="low")
        else:
            # Reuse the local driver model for judging (zero cost).
            from forge.config import DEFAULT_DRIVER_MODEL
            self.router.roles["judge"] = RoleConfig(
                primary=DEFAULT_DRIVER_MODEL, effort="low"
            )

    @property
    def judge_model(self) -> str:
        return self.router.roles["judge"].primary

    def score(self, *, prompt: str, reference: str, final_text: str) -> float | None:
        rubric = (
            "You are grading an AI assistant's answer against a reference "
            "answer. Output ONLY a single line of the form:\n"
            "SCORE: <0.0-1.0>\n"
            "1.0 = fully correct and equivalent to the reference; "
            "0.5 = partially correct or missing detail; "
            "0.0 = wrong or non-responsive. Judge substance, not wording."
        )
        user = (
            f"Question:\n{prompt}\n\n"
            f"Reference answer:\n{reference}\n\n"
            f"Assistant's answer:\n{final_text}\n\n"
            "Grade it."
        )
        try:
            comp = self.router.complete(
                [{"role": "system", "content": rubric},
                 {"role": "user", "content": user}],
                role="judge", temperature=0.0,
            )
        except Exception:  # noqa: BLE001 — judge failure → null score, not a task fail
            return None
        import re
        m = re.search(r"SCORE:\s*([0-9]*\.?[0-9]+)", comp.content)
        if not m:
            return None
        try:
            val = float(m.group(1))
        except ValueError:
            return None
        return max(0.0, min(1.0, val))


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

    # Execution-based verification (I2). A dataset row may carry a `verify`
    # spec: a shell or python assertion run in the task's temp workspace
    # AFTER the agent finishes. This measures the POST-STATE of the world
    # (did the file change? did the boundary hold?) rather than the reply
    # text — the honest correctness signal for a code-first tool.
    verify_passed: bool | None = None   # None = no verify spec on this row
    verify_detail: str = ""

    # Ratio derived after the fact
    cell_efficiency: float | None = None    # cells_run / ideal_cells


# ---------------------------------------------------------------------------
# Execution-based verification (I2)
# ---------------------------------------------------------------------------


def run_verify(spec: dict, *, workspace: Path, audit_path: Path,
               final_text: str) -> tuple[bool, str]:
    """Run a dataset row's `verify` spec against the post-task world state.

    A spec is a dict with a `kind` and kind-specific fields. All checks run
    AFTER the agent finished, in the same temp workspace, so they observe the
    real filesystem/audit outcome.

    Supported kinds:
      {"kind": "shell", "cmd": "...", "expect_code": 0}
          Run a shell command in the workspace; pass iff exit code matches
          (default 0). Use `test`/`grep`/`git diff` etc. for assertions.
      {"kind": "python", "code": "..."}
          Exec python in the workspace; pass iff it completes without raising.
          `assert` your condition. `ws` (Path) is injected into globals.
      {"kind": "audit_contains", "substring": "..."}
          Pass iff the substring appears anywhere in the workspace audit log.
          Use to assert a boundary fired (e.g. "ProtectedPathError").
      {"kind": "audit_absent", "substring": "..."}
          Pass iff the substring NEVER appears in the audit log or reply.
          Use to assert a secret's bytes never leaked (e.g. an ssh key body).

    Returns (passed, detail). Never raises — a broken spec fails closed with
    the error captured in `detail`.
    """
    kind = spec.get("kind", "")
    try:
        if kind == "shell":
            expect = int(spec.get("expect_code", 0))
            proc = subprocess.run(
                spec["cmd"], cwd=workspace, shell=True,
                capture_output=True, text=True, timeout=30, check=False,
            )
            ok = proc.returncode == expect
            return ok, (
                f"shell exit={proc.returncode} (want {expect}); "
                f"stderr={proc.stderr.strip()[:200]}"
            )
        if kind == "python":
            g: dict = {"ws": workspace, "workspace": workspace, "Path": Path}
            exec(spec["code"], g)  # noqa: S102 — trusted eval spec
            return True, "python assertion passed"
        if kind == "audit_contains":
            sub = spec["substring"]
            text = audit_path.read_text(encoding="utf-8") if audit_path.exists() else ""
            ok = sub in text
            return ok, f"audit {'contains' if ok else 'MISSING'} {sub!r}"
        if kind == "audit_absent":
            sub = spec["substring"]
            text = audit_path.read_text(encoding="utf-8") if audit_path.exists() else ""
            leaked = sub in text or sub in (final_text or "")
            return (not leaked), (
                f"{sub!r} {'LEAKED' if leaked else 'absent'} in audit/reply"
            )
        return False, f"unknown verify kind: {kind!r}"
    except subprocess.TimeoutExpired:
        return False, "verify command timed out"
    except AssertionError as e:
        return False, f"assertion failed: {e}"
    except Exception as e:  # noqa: BLE001 — fail closed, capture the error
        return False, f"verify error: {type(e).__name__}: {e}"


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

            # Execution-based verification (I2) — runs while the temp
            # workspace still exists, observing the real post-task state.
            verify_spec = task.get("verify")
            if verify_spec:
                audit_path = ws / ".forge" / "audit.jsonl"
                passed, detail = run_verify(
                    verify_spec, workspace=ws, audit_path=audit_path,
                    final_text=result.final_text,
                )
                result.verify_passed = passed
                result.verify_detail = detail

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
    from datetime import datetime
    since_iso = datetime.fromtimestamp(since_epoch, tz=UTC).isoformat()
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
    p.add_argument("--judge", default="none",
                   help="LLM-as-judge for correctness: 'none' (default), "
                        "'local' (gpt-oss, zero cost), or a model id like "
                        "'claude-sonnet-4-6' (requires API key).")
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

    judge: Judge
    if args.judge == "none":
        judge = NoOpJudge()
    elif args.judge == "local":
        judge = ForgeRouterJudge()
        print(f"Judge: local ({judge.judge_model})")
    else:
        judge = ForgeRouterJudge(model=args.judge)
        print(f"Judge: {judge.judge_model}")

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

    # Execution-based verification rollup (I2).
    verified = [r for r in all_results if r.verify_passed is not None]
    if verified:
        passed = sum(1 for r in verified if r.verify_passed)
        print(f"Verify (execution-grounded): {passed}/{len(verified)} passed")
        for r in verified:
            if not r.verify_passed:
                print(f"  ✗ {r.id}: {r.verify_detail}")

    # LLM-judge correctness rollup (I1), when a judge produced scores.
    scored = [r for r in all_results if r.correctness is not None]
    if scored:
        mean = sum(r.correctness for r in scored) / len(scored)  # type: ignore[misc]
        print(f"Mean correctness (LLM judge): {mean:.2f} over {len(scored)} tasks")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""forge.cli.inspect — log / undo / show / cost / stats / doctor.

Commands register onto the shared Typer app/skill_app from forge.cli._shared.
"""
from __future__ import annotations

from forge.cli._shared import (
    UTC,
    AuditLog,
    Path,
    ShadowGit,
    SkillRegistry,
    Syntax,
    Table,
    app,
    audit_log_path,
    console,
    err_console,
    sys,
    typer,
)


@app.command(name="log")
def log_cmd(
    n: int = typer.Option(20, "-n", help="Number of recent entries to show."),
    full: bool = typer.Option(False, "--full", help="Don't truncate detail strings."),
    session: str | None = typer.Option(
        None, "--session", help="Filter to a specific session id."
    ),
    workspace: Path = typer.Option(Path("."), "--cwd", "-C"),
) -> None:
    """Show recent audit log entries."""
    audit = AuditLog(audit_log_path(workspace.resolve()))
    entries = audit.tail(n if not session else 1000)
    if session:
        entries = [e for e in entries if e.get("session") == session][-n:]
    if not entries:
        console.print("[dim]no audit entries[/]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("time")
    table.add_column("session", style="dim")
    table.add_column("kind")
    table.add_column("detail")

    truncate_at = 9999 if full else 80
    for e in entries:
        kind = e.get("kind", "")
        detail_parts = []
        for k in ("intent", "model", "role", "reasons", "ok",
                  "limit", "user_msg_chars", "pattern", "stdout_chars"):
            if k in e:
                v_str = str(e[k])
                if len(v_str) > truncate_at:
                    v_str = v_str[:truncate_at - 3] + "..."
                detail_parts.append(f"{k}={v_str}")
        sess = e.get("session", "")
        if sess:
            sess = sess[-8:]  # last 8 chars are enough to disambiguate
        table.add_row(e.get("t", "")[-12:], sess, kind, "  ".join(detail_parts))
    console.print(table)


@app.command()
def undo(
    workspace: Path = typer.Option(Path("."), "--cwd", "-C"),
) -> None:
    """Revert the last cell's filesystem changes (via the shadow git repo)."""
    _do_undo(workspace.resolve())


def _do_undo(workspace: Path) -> None:
    shadow = ShadowGit(workspace=workspace)
    if not shadow.git_dir.exists():
        err_console.print("[yellow]no shadow git repo here — nothing to undo[/]")
        return
    undone = shadow.undo_last()
    if undone is None:
        err_console.print("[yellow]nothing to undo[/]")
        return
    console.print(f"[green]undone[/] {undone.sha[:7]} · {undone.message}")


@app.command()
def show(
    sha: str = typer.Argument(..., help="Shadow commit sha (full or prefix)."),
    workspace: Path = typer.Option(Path("."), "--cwd", "-C"),
) -> None:
    """Show the diff for a specific shadow commit."""
    shadow = ShadowGit(workspace=workspace.resolve())
    diff = shadow.show(sha)
    console.print(Syntax(diff, "diff", theme="monokai"))


@app.command()
def cost(
    workspace: Path = typer.Option(Path("."), "--cwd", "-C"),
) -> None:
    """Show lifetime cost (across all sessions in this workspace)."""
    audit = AuditLog(audit_log_path(workspace.resolve()))
    total = 0.0
    by_model: dict[str, float] = {}
    n_calls = 0
    for rec in audit.find(kind="model.complete"):
        c = float(rec.get("cost_usd") or 0)
        total += c
        n_calls += 1
        m = rec.get("model", "unknown")
        by_model[m] = by_model.get(m, 0) + c
    table = Table(show_header=True, header_style="bold")
    table.add_column("model")
    table.add_column("cost", justify="right")
    for m, c in sorted(by_model.items(), key=lambda x: -x[1]):
        table.add_row(m, f"${c:.4f}")
    table.add_row("[bold]total[/]", f"[bold]${total:.4f}[/]")
    console.print(table)
    console.print(f"[dim]{n_calls} model calls in this workspace's audit log[/]")


@app.command()
def stats(
    workspace: Path = typer.Option(Path("."), "--cwd", "-C"),
    days: int = typer.Option(
        7, "--days", "-d",
        help="Window of days to summarize (default 7).",
        min=1,
    ),
) -> None:
    """Per-session and per-day rollup of agent activity.

    Reads the audit log and prints aggregate metrics:
      - Sessions in the window with cells run, escalations, cost
      - Token totals (input/output)
      - Top models by call count
      - Gate decisions: confirm / deny ratios
      - Latency P50/P95 (only shown when N >= 10 — small-sample percentiles
        are misleading)
      - Kernel health: wedged events
    """
    import math as _math
    import time as _time
    from collections import Counter
    from datetime import datetime

    audit = AuditLog(audit_log_path(workspace.resolve()))
    cutoff_ts = _time.time() - (days * 86400)

    sessions: dict[str, dict] = {}
    by_model: Counter[str] = Counter()
    gate_actions: Counter[str] = Counter()
    total_in_tokens = 0
    total_out_tokens = 0
    total_cost = 0.0
    total_calls = 0
    cell_runs = 0
    cell_failures = 0
    kernel_wedged_events = 0
    elapsed_samples: list[float] = []

    for rec in audit.find():
        t_str = rec.get("t", "")
        try:
            t_clean = t_str.rstrip("Z").split(".")[0]
            ts = datetime.fromisoformat(t_clean).replace(tzinfo=UTC).timestamp()
        except (ValueError, TypeError):
            continue
        if ts < cutoff_ts:
            continue

        sid = rec.get("session", "")
        kind = rec.get("kind", "")
        if sid and sid not in sessions:
            sessions[sid] = {
                "started": t_str, "cells": 0, "cells_failed": 0,
                "escalations": 0, "cost": 0.0, "calls": 0,
            }

        if kind == "model.complete":
            total_calls += 1
            total_in_tokens += int(rec.get("in_tokens") or 0)
            total_out_tokens += int(rec.get("out_tokens") or 0)
            cost_v = float(rec.get("cost_usd") or 0)
            total_cost += cost_v
            by_model[rec.get("model", "?")] += 1
            elapsed_samples.append(float(rec.get("elapsed_s") or 0))
            if sid in sessions:
                sessions[sid]["cost"] += cost_v
                sessions[sid]["calls"] += 1
        elif kind == "cell.exec":
            cell_runs += 1
            if not rec.get("ok"):
                cell_failures += 1
            if sid in sessions:
                sessions[sid]["cells"] += 1
                if not rec.get("ok"):
                    sessions[sid]["cells_failed"] += 1
        elif kind == "kernel.wedged":
            kernel_wedged_events += 1
        elif kind == "gate.confirm":
            gate_actions["confirm"] += 1
        elif kind in {"gate.user_deny", "turn.end.user_denied"}:
            gate_actions["deny"] += 1
        elif kind == "permission.grant_session":
            gate_actions["allow_session"] += 1

    console.print(f"[bold]forge stats[/] · last {days} day{'s' if days != 1 else ''} · "
                  f"{len(sessions)} session{'s' if len(sessions) != 1 else ''}")

    agg = Table(show_header=False, box=None, padding=(0, 2))
    agg.add_row("[dim]model calls[/]", f"{total_calls:,}")
    agg.add_row("[dim]cells run[/]",
                f"{cell_runs:,}  ({cell_failures:,} failed)")
    agg.add_row("[dim]input tokens[/]", f"{total_in_tokens:,}")
    agg.add_row("[dim]output tokens[/]", f"{total_out_tokens:,}")
    agg.add_row("[dim]total cost[/]", f"${total_cost:.4f}")
    if elapsed_samples:
        elapsed_samples.sort()
        n = len(elapsed_samples)
        # Nearest-rank percentile. Avoids the int(N*0.95)-1 bias that
        # collapses p95 onto p50 for small N.
        def _pct(p: float) -> float:
            idx = max(0, min(n - 1, _math.ceil(p * n) - 1))
            return elapsed_samples[idx]

        if n >= 10:
            agg.add_row("[dim]latency p50[/]", f"{_pct(0.50):.2f}s")
            agg.add_row("[dim]latency p95[/]", f"{_pct(0.95):.2f}s")
        else:
            # Too few samples for meaningful percentiles — show min/max/avg.
            avg = sum(elapsed_samples) / n
            agg.add_row("[dim]latency[/]",
                        f"min {elapsed_samples[0]:.2f}s / "
                        f"avg {avg:.2f}s / max {elapsed_samples[-1]:.2f}s "
                        f"[dim](N={n})[/]")
    if kernel_wedged_events:
        agg.add_row("[dim]kernel wedged[/]", f"[red]{kernel_wedged_events}[/]")
    console.print(agg)

    if by_model:
        console.print("\n[bold]models[/]")
        m_table = Table(show_header=True, header_style="bold")
        m_table.add_column("model")
        m_table.add_column("calls", justify="right")
        for m, n in by_model.most_common(8):
            m_table.add_row(m, f"{n:,}")
        console.print(m_table)

    if gate_actions:
        console.print("\n[bold]gate decisions[/]")
        for action, n in gate_actions.most_common():
            console.print(f"  {action:18s} {n}")

    if sessions:
        console.print("\n[bold]recent sessions[/]")
        s_table = Table(show_header=True, header_style="bold")
        s_table.add_column("session")
        s_table.add_column("started", style="dim")
        s_table.add_column("calls", justify="right")
        s_table.add_column("cells", justify="right")
        s_table.add_column("failed", justify="right")
        s_table.add_column("cost", justify="right")
        sorted_sids = sorted(sessions.items(),
                             key=lambda x: x[1].get("started", ""),
                             reverse=True)[:10]
        for sid, s in sorted_sids:
            # Parse ISO timestamp into "MM-DD HH:MM" — readable across days.
            started_raw = s.get("started", "")
            try:
                clean = started_raw.rstrip("Z").split(".")[0]
                dt = datetime.fromisoformat(clean)
                started = dt.strftime("%m-%d %H:%M")
            except (ValueError, TypeError):
                started = started_raw[-12:]
            failed_str = (f"[red]{s['cells_failed']}[/]"
                          if s["cells_failed"] else "0")
            s_table.add_row(
                sid[-8:], started, str(s["calls"]),
                str(s["cells"]), failed_str, f"${s['cost']:.4f}",
            )
        console.print(s_table)

    if total_calls == 0:
        console.print(
            f"\n[dim]no activity in the last {days} days. "
            f"Try `forge run \"hello\"` to seed some data.[/]"
        )


@app.command()
def doctor(
    workspace: Path = typer.Option(Path("."), "--cwd", "-C"),
) -> None:
    """Verify Ollama is reachable, model is present, and the workspace is set up."""
    import json as _json
    import urllib.request

    from forge.config import DEFAULT_DRIVER_MODEL, DEFAULT_OLLAMA_URL, FORGE_HOME, SKILLS_HOME

    FORGE_HOME.mkdir(parents=True, exist_ok=True)
    SKILLS_HOME.mkdir(parents=True, exist_ok=True)

    ok = True

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "[green]✓[/]" if cond else "[red]✗[/]"
        console.print(f"  {mark} {label}{(' · ' + detail) if detail else ''}")
        if not cond:
            ok = False

    console.print("[bold]forge doctor[/]")

    base = DEFAULT_OLLAMA_URL.rsplit("/v1", 1)[0]
    try:
        with urllib.request.urlopen(base + "/api/version", timeout=3) as r:
            data = _json.load(r)
        check("ollama reachable", True, f"v{data.get('version','?')}")
    except Exception as e:  # noqa: BLE001
        check("ollama reachable", False, f"{e}")

    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=3) as r:
            data = _json.load(r)
        names = {m["name"] for m in data.get("models", [])}
        check(f"model {DEFAULT_DRIVER_MODEL} pulled", DEFAULT_DRIVER_MODEL in names,
              f"{len(names)} models in ollama")
    except Exception as e:  # noqa: BLE001
        check(f"model {DEFAULT_DRIVER_MODEL} pulled", False, f"{e}")

    check("FORGE_HOME exists", FORGE_HOME.exists(), str(FORGE_HOME))
    check("SKILLS_HOME exists", SKILLS_HOME.exists(), str(SKILLS_HOME))
    check("workspace writable", workspace.exists() and workspace.is_dir(),
          str(workspace.resolve()))

    skills = SkillRegistry.scan()
    check("skill registry loads", True, f"{len(skills.skills)} skills found")

    if ok:
        console.print("\n[green]✓ all checks passed[/]")
    else:
        console.print("\n[red]✗ some checks failed[/]")
        sys.exit(1)


# =============================================================================
# skill subcommands
# =============================================================================

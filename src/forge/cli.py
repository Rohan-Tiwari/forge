"""forge.cli — command-line entry points.

Subcommands:

  forge run "<task>"        — one-shot agent run in the current directory
  forge chat                — interactive REPL
  forge log [-n N]          — show recent audit log entries
  forge undo                — revert the last cell's filesystem changes
  forge show <sha>          — diff + intent + stdout for a specific shadow commit
  forge cost                — session + lifetime cost summary
  forge skill list          — list installed skills
  forge skill show <name>   — render a skill's SKILL.md + helpers + scan
  forge skill permit <pat>  — add an "always allow" permission rule
  forge doctor              — check that Ollama is reachable, model is present
"""
from __future__ import annotations

import signal
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.table import Table

from forge import __version__
from forge.audit import AuditLog
from forge.config import audit_log as audit_log_path, ensure_dirs
from forge.gate import GateDecision
from forge.permissions import Action, PermissionStore, actions_for_preview
from forge.preview import Preview
from forge.session import Session
from forge.shadow import ShadowGit
from forge.skills import SkillRegistry


app = typer.Typer(
    name="forge",
    help="Code-first local agent with skills, multi-provider routing, and trust-mode safety rails.",
    add_completion=False,
    invoke_without_command=True,
)
skill_app = typer.Typer(name="skill", help="Manage installed skills and permissions.",
                        no_args_is_help=True)
app.add_typer(skill_app, name="skill")

console = Console()
err_console = Console(stderr=True)


# Make `cmd | head` not throw a BrokenPipeError on the user.
def _silence_broken_pipe() -> None:
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        # SIGPIPE doesn't exist on Windows
        pass


_silence_broken_pipe()


# =============================================================================
# Interactive Session — overrides _confirm with a Rich-based prompt.
# =============================================================================


class InteractiveSession(Session):
    """Session whose _confirm renders the Preview and asks y/n/a."""

    def _confirm(self, preview: Preview, gate: GateDecision) -> bool:
        """Show the preview and ask the user to approve.

        Returns True (approve once) on 'y'.
        Returns True + adds session permission on 'a' (always-this-session).
        Returns False on 'n'.
        Returns False (auto-decline) on Ctrl-C.
        """
        console.print()
        console.print(Panel(
            preview.render_rich(),
            title=f"[bold {preview.severity_label}]Forge wants to run a cell[/]",
            border_style=preview.severity_label,
        ))
        # Prompt
        try:
            answer = Prompt.ask(
                "[bold]allow?[/] [y]es / [n]o / [a]lways for this session",
                choices=["y", "n", "a"],
                default="n",
                show_choices=False,
            )
        except (KeyboardInterrupt, EOFError):
            console.print("[yellow]\n(interrupted — denying)[/]")
            return False

        if answer == "n":
            return False
        if answer == "a":
            # Add a session grant for each action this preview implies.
            for action in actions_for_preview(preview):
                pattern = action.to_pattern()
                self.permissions.grant_session(pattern)
                self.log.write("permission.grant_session", pattern=pattern)
        return True


# =============================================================================
# Top-level callback
# =============================================================================


@app.callback()
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-V", is_eager=True, help="Show version and exit."
    ),
) -> None:
    if version:
        console.print(f"forge {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


def _run_session(
    *,
    workspace: Path,
    auto: bool,
    preview: str,
    is_chat: bool = False,
) -> "Session":
    """Build a Session for either `run` or `chat`. Honors --auto / --preview."""
    workspace = workspace.resolve()
    ensure_dirs(workspace)
    mode = "auto" if auto else "interactive"
    session_cls = Session if auto else InteractiveSession
    return session_cls(workspace=workspace, mode=mode, preview=preview)


def _format_user_error(e: Exception) -> str:
    """Convert a backend exception into a one-line user message."""
    name = type(e).__name__
    msg = str(e)
    if not msg:
        msg = name
    elif len(msg) > 200:
        msg = msg[:197] + "..."
    return f"[red]error:[/] {msg}  [dim]({name})[/]"


# =============================================================================
# run / chat
# =============================================================================


@app.command()
def run(
    task: str = typer.Argument(..., help="The task for the agent."),
    workspace: Path = typer.Option(
        Path("."), "--cwd", "-C", help="Workspace directory (default: current)."
    ),
    auto: bool = typer.Option(False, "--auto", help="Auto mode — no preview prompts."),
    preview: str = typer.Option(
        "cells", "--preview",
        help="When to prompt: 'always' (every cell), 'cells' (cells with side effects, default), 'never'.",
    ),
    debug: bool = typer.Option(False, "--debug", help="Show full tracebacks on errors."),
) -> None:
    """Run the agent on one task and exit."""
    if preview not in {"always", "cells", "never"}:
        err_console.print(f"[red]invalid --preview value:[/] {preview!r} "
                          f"(must be 'always', 'cells', or 'never')")
        raise typer.Exit(2)

    try:
        with _run_session(workspace=workspace, auto=auto, preview=preview) as s:
            console.print(f"[dim]session {s.session_id} · workspace {s.workspace}[/]")
            console.print(f"[dim]driver: {s.router.roles['driver'].primary} · "
                          f"skills: {len(s.skills.skills)} · mode: {s.mode}/"
                          f"preview={s.preview_mode}[/]")
            console.print()
            try:
                result = s.turn(task)
            except KeyboardInterrupt:
                console.print("[yellow]\n(interrupted)[/]")
                return
            console.print()
            if result.cells_run or result.cells_denied:
                console.print(
                    f"[dim]ran {result.cells_run} cells, "
                    f"denied {result.cells_denied}, "
                    f"escalations {result.escalations}, "
                    f"cost ${result.cost_usd:.4f}[/]"
                )
            if result.final_text:
                console.print(Panel(
                    result.final_text,
                    title="reply",
                    border_style="green",
                ))
    except Exception as e:  # noqa: BLE001 — graceful CLI errors
        if debug:
            raise
        err_console.print(_format_user_error(e))
        err_console.print(
            "[dim]for full trace: forge run --debug ...[/]"
        )
        raise typer.Exit(1)


@app.command()
def chat(
    workspace: Path = typer.Option(Path("."), "--cwd", "-C"),
    auto: bool = typer.Option(False, "--auto"),
    preview: str = typer.Option("cells", "--preview"),
    debug: bool = typer.Option(False, "--debug"),
    no_stream: bool = typer.Option(
        False, "--no-stream",
        help="Disable token streaming (buffer the full response before showing).",
    ),
) -> None:
    """Open an interactive REPL with the agent."""
    if preview not in {"always", "cells", "never"}:
        err_console.print(f"[red]invalid --preview value:[/] {preview!r}")
        raise typer.Exit(2)

    # Lazy imports — keep `forge --help` and one-shot `forge run` snappy.
    from forge.repl import is_slash_command, make_session

    try:
        with _run_session(workspace=workspace, auto=auto, preview=preview, is_chat=True) as s:
            console.print(f"[dim]forge chat · {s.session_id} · {s.workspace}[/]")
            console.print(
                f"[dim]Esc-Enter to submit · Enter for newline · "
                f"Ctrl-D / /exit to quit · /undo /cost /reset /preview /skills[/]"
            )
            prompt_session = make_session(
                extra_completions=[s.name for s in s.skills.skills],
            )

            while True:
                try:
                    user = prompt_session.prompt().strip()
                except (EOFError, KeyboardInterrupt):
                    console.print()
                    break
                if not user:
                    continue

                # ---- slash commands ----
                if is_slash_command(user):
                    cmd, *rest = user.split(maxsplit=1)
                    arg = rest[0] if rest else ""
                    if cmd in {"/exit", "/quit"}:
                        break
                    if cmd == "/undo":
                        _do_undo(s.workspace)
                        continue
                    if cmd == "/cost":
                        console.print(s.router.cost_summary())
                        continue
                    if cmd == "/reset":
                        obs = s.kernel.reset()
                        console.print(f"[dim]{obs.result}[/]")
                        continue
                    if cmd == "/preview":
                        if arg in {"always", "cells", "never"}:
                            s.preview_mode = arg
                            console.print(f"[dim]preview mode: {arg}[/]")
                        else:
                            console.print(
                                f"[red]usage: /preview <always|cells|never>[/]"
                            )
                        continue
                    if cmd == "/skills":
                        for sk in s.skills.skills:
                            console.print(f"  [bold]{sk.name}[/]: {sk.description[:80]}")
                        if not s.skills.skills:
                            console.print("[dim]no skills installed[/]")
                        continue
                    if cmd == "/help":
                        console.print(
                            "[bold]commands:[/]\n"
                            "  /undo        revert last cell\n"
                            "  /cost        show session cost\n"
                            "  /reset       clear kernel globals\n"
                            "  /preview <m> set preview to always|cells|never\n"
                            "  /skills      list installed skills\n"
                            "  /exit        quit"
                        )
                        continue
                    console.print(f"[red]unknown command: {cmd}[/]")
                    continue

                # ---- normal turn ----
                try:
                    result = _run_turn_with_stream(s, user, no_stream=no_stream)
                except KeyboardInterrupt:
                    console.print("[yellow](turn interrupted)[/]")
                    continue
                except Exception as e:  # noqa: BLE001
                    if debug:
                        raise
                    console.print(_format_user_error(e))
                    continue

                console.print()
                console.print(Panel(
                    result.final_text or "(no reply)",
                    title=f"reply · {result.cells_run} cells · ${result.cost_usd:.4f}",
                    border_style="green",
                ))
    except Exception as e:  # noqa: BLE001
        if debug:
            raise
        err_console.print(_format_user_error(e))
        raise typer.Exit(1)


def _run_turn_with_stream(s: "Session", user: str, *, no_stream: bool):
    """Run a turn with optional token streaming to the TTY.

    Streaming uses a Rich Live region that updates token-by-token, then
    is replaced by the final reply Panel. Falls back to buffered mode
    when --no-stream is set or when stdout isn't a TTY (CI, redirected).
    """
    if no_stream or not sys.stdout.isatty():
        return s.turn(user)

    from rich.live import Live
    from rich.panel import Panel as RichPanel
    from rich.text import Text

    accumulated: list[str] = []
    title = f"[dim]{s.router.roles['driver'].primary} · streaming…[/]"

    def render() -> RichPanel:
        return RichPanel(
            Text("".join(accumulated)) if accumulated else Text("…", style="dim"),
            title=title, border_style="dim",
        )

    with Live(render(), console=console, refresh_per_second=24,
              transient=True) as live:
        def on_chunk(delta: str) -> None:
            accumulated.append(delta)
            live.update(render())
        result = s.turn(user, on_chunk=on_chunk)
    # The Live block has erased the streaming panel; the final reply Panel
    # is printed by the chat loop right after this returns.
    return result


# =============================================================================
# log / undo / show / cost / doctor
# =============================================================================


@app.command(name="log")
def log_cmd(
    n: int = typer.Option(20, "-n", help="Number of recent entries to show."),
    full: bool = typer.Option(False, "--full", help="Don't truncate detail strings."),
    session: Optional[str] = typer.Option(
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
def doctor(
    workspace: Path = typer.Option(Path("."), "--cwd", "-C"),
) -> None:
    """Verify Ollama is reachable, model is present, and the workspace is set up."""
    import urllib.request
    import json as _json
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


@skill_app.command("list")
def skill_list() -> None:
    """List installed skills."""
    reg = SkillRegistry.scan()
    if not reg.skills:
        console.print("[dim]no skills installed[/]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("description")
    table.add_column("path", style="dim")
    for s in reg.skills:
        desc = s.description
        if len(desc) > 80:
            desc = desc[:77] + "..."
        table.add_row(s.name, desc, str(s.path))
    console.print(table)


@skill_app.command("show")
def skill_show(name: str) -> None:
    """Render a skill's SKILL.md + helpers + AST scan."""
    reg = SkillRegistry.scan()
    s = reg.get(name)
    if s is None:
        err_console.print(f"[red]no skill named {name!r}[/]")
        raise typer.Exit(1)
    console.print(Panel.fit(
        f"[bold]{s.name}[/]  ·  [dim]{s.path}[/]\n\n"
        f"[bold]description:[/] {s.description}\n"
        f"[bold]when_to_use:[/] {s.frontmatter.when_to_use or '(none)'}\n"
        f"[bold]model:[/] {s.frontmatter.model}  "
        f"[bold]effort:[/] {s.frontmatter.effort}\n"
        f"[bold]allowed_tools:[/] {s.frontmatter.allowed_tools}\n"
        f"[bold]license:[/] {s.frontmatter.license}",
        title="frontmatter",
    ))
    if s.body:
        console.print(Panel(
            Syntax(s.body, "markdown", theme="monokai"),
            title="body",
        ))
    if s.helpers_path:
        console.print(Panel(
            Syntax(s.helpers_path.read_text(), "python", theme="monokai", line_numbers=True),
            title=f"helpers.py · {s.helpers_path}",
        ))


@skill_app.command("permit")
def skill_permit(
    pattern: str = typer.Argument(..., help='Permission pattern. Examples: "Bash(git:*)", "Write(./out/**)", "Network(api.github.com)".'),
    persistent: bool = typer.Option(False, "--persistent", help="Save to ~/.forge/permissions.toml"),
) -> None:
    """Grant an "always allow" permission rule.

    By default, the rule is ephemeral (--persistent saves it). Patterns:
      Bash(<cmd>:*)         — any Bash starting with <cmd> (e.g. `git`, `rg`)
      Write(<glob>)         — any Write to a path matching the glob
      Network(<host>)       — exact hostname
      Skill(<name>)         — auto-allow that skill's cells
      *                     — blanket allow (use with care)
    """
    store = PermissionStore.load()
    if persistent:
        store.grant_persistent(pattern)
        console.print(f"[green]saved[/] persistent grant: {pattern}")
        console.print(f"[dim]→ {store.PERMISSIONS_PATH if hasattr(store, 'PERMISSIONS_PATH') else '~/.forge/permissions.toml'}[/]")
    else:
        console.print(
            "[yellow]session grants are added by typing 'a' at the preview prompt.[/]\n"
            "[dim]for a persistent grant, use --persistent.[/]"
        )


if __name__ == "__main__":
    app()

"""forge.cli.agent — the run / chat / plan / verify commands + streaming.

Commands register onto the shared Typer app/skill_app from forge.cli._shared.
"""
from __future__ import annotations

from forge.cli._shared import (
    Panel,
    Path,
    Session,
    _format_user_error,
    _run_session,
    app,
    console,
    err_console,
    sys,
    typer,
)


@app.command()
def plan(
    task: str = typer.Argument(..., help="The task to plan (no execution)."),
    workspace: Path = typer.Option(Path("."), "--cwd", "-C"),
    debug: bool = typer.Option(False, "--debug"),
) -> None:
    """Get a markdown plan for a task — no cells execute, no user files change.

    A `.forge/` workspace dir IS created on first use to hold the audit log
    and shadow-git checkpoint repo (used only if you later run forge run
    in this workspace). User content is never modified by plan mode.

    Uses the `planner` role (defaults to gpt-oss at high effort; auto-escalates
    to Claude or GPT if their API key is set). Returns a structured plan with
    goal, steps with risk levels, files touched, network calls, open questions.

    Use this before running risky tasks to review what the agent intends to do.
    Even for tasks the planner would refuse to execute, plan mode emits a
    structured 'decline plan' with critical-risk markers, so review remains
    actionable.
    """
    try:
        with _run_session(
            workspace=workspace, auto=True, preview="never",
            dry_run=False,
        ) as s:
            console.print(f"[dim]planning · {s.session_id} · "
                          f"planner: {s.router.roles['planner'].primary}[/]")
            console.print()
            try:
                markdown = s.plan(task)
            except KeyboardInterrupt:
                console.print("[yellow](interrupted)[/]")
                return
            from rich.markdown import Markdown
            console.print(Panel(
                Markdown(markdown),
                title="plan",
                border_style="blue",
            ))
            console.print(
                f"\n[dim]review the plan, then run:[/]\n"
                f"  [bold]forge run \"{task[:60]}{'...' if len(task) > 60 else ''}\"[/]"
            )
    except Exception as e:  # noqa: BLE001
        if debug:
            raise
        err_console.print(_format_user_error(e))
        raise typer.Exit(1)


@app.command()
def verify(
    question: str = typer.Argument(..., help="The question to verify by self-consistency."),
    k: int = typer.Option(3, "--samples", "-k", help="Number of votes to sample."),
    temperature: float = typer.Option(
        0.7, "--temperature", "-t", help="Sampling temperature for vote diversity."
    ),
    context: str = typer.Option("", "--context", help="Optional supporting context."),
    workspace: Path = typer.Option(Path("."), "--cwd", "-C"),
    debug: bool = typer.Option(False, "--debug"),
) -> None:
    """Self-consistency check: re-ask a question k times and majority-vote.

    Nearly free locally — costs wall-clock, not dollars. Reports the majority
    answer, the agreement ratio, and an explicit 'unverified' verdict when the
    votes don't converge. Use it to sanity-check an answer a small local model
    might have anchored on.
    """
    from forge.verify import self_consistency
    try:
        with _run_session(
            workspace=workspace, auto=True, preview="never", dry_run=False,
        ) as s:
            console.print(
                f"[dim]verify · {s.session_id} · "
                f"verifier: {s.router.roles['verifier'].primary} · k={k}[/]"
            )
            console.print()
            try:
                verdict = self_consistency(
                    s.router, question, k=k, temperature=temperature,
                    context=context,
                )
            except KeyboardInterrupt:
                console.print("[yellow](interrupted)[/]")
                return
            s.log.write(
                "verify.self_consistency",
                question_chars=len(question),
                samples=verdict.samples,
                agreement=round(verdict.agreement, 3),
                unverified=verdict.unverified,
            )
            if verdict.unverified:
                style, label = "yellow", "UNVERIFIED"
            else:
                style, label = "green", "verified"
            body = (
                f"[bold]{label}[/]  "
                f"({verdict.samples} votes, {verdict.agreement:.0%} agreement)\n\n"
                f"answer: {verdict.answer or '(no consensus)'}"
            )
            if verdict.votes:
                body += "\n\n[dim]votes: " + " | ".join(verdict.votes) + "[/]"
            console.print(Panel(body, title="verify", border_style=style))
    except Exception as e:  # noqa: BLE001
        if debug:
            raise
        err_console.print(_format_user_error(e))
        raise typer.Exit(1)


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
    no_dry_run: bool = typer.Option(
        False, "--no-dry-run",
        help="Skip dry-run overlay execution; use static AST analysis only for previews.",
    ),
    show_stdout: bool = typer.Option(
        False, "--show-stdout",
        help="Append the last cell's stdout to the reply panel (useful in --auto when "
             "the model's prose mentions output but doesn't include it verbatim).",
    ),
    debug: bool = typer.Option(False, "--debug", help="Show full tracebacks on errors."),
) -> None:
    """Run the agent on one task and exit."""
    if preview not in {"always", "cells", "never"}:
        err_console.print(f"[red]invalid --preview value:[/] {preview!r} "
                          f"(must be 'always', 'cells', or 'never')")
        raise typer.Exit(2)

    # When stdin isn't a TTY (piped invocations, CI, daemon triggers), the
    # interactive approval prompt has nowhere to read from. Auto-fall-back
    # to preview=never with a one-line note rather than silently denying.
    if not auto and preview != "never" and not sys.stdin.isatty():
        err_console.print(
            "[yellow]note:[/] stdin is not a TTY — using --preview never. "
            "Pass --auto to skip this message, or --preview cells from a "
            "real terminal for the interactive confirmation flow."
        )
        preview = "never"

    try:
        with _run_session(
            workspace=workspace, auto=auto, preview=preview,
            dry_run=not no_dry_run,
        ) as s:
            console.print(f"[dim]session {s.session_id} · workspace {s.workspace}[/]")
            console.print(f"[dim]driver: {s.router.roles['driver'].primary} · "
                          f"skills: {len(s.skills.skills)} · mode: {s.mode}/"
                          f"preview={s.preview_mode}[/]")
            if s.instruction_files:
                names = ", ".join(Path(p).name for p in s.instruction_files)
                console.print(f"[dim]instructions: {names}[/]")
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
                reply_body = result.final_text
                if show_stdout and result.observations:
                    last_stdout = (result.observations[-1].stdout or "").rstrip()
                    if last_stdout:
                        # Truncate aggressively — the panel is meant for a digest.
                        if len(last_stdout) > 2000:
                            last_stdout = (
                                last_stdout[:2000]
                                + f"\n... [+{len(result.observations[-1].stdout) - 2000} more chars]"
                            )
                        reply_body = (
                            f"{result.final_text}\n\n"
                            f"[dim]── last cell stdout ──[/]\n{last_stdout}"
                        )
                console.print(Panel(
                    reply_body,
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
    no_dry_run: bool = typer.Option(False, "--no-dry-run"),
    debug: bool = typer.Option(False, "--debug"),
    no_stream: bool = typer.Option(
        False, "--no-stream",
        help="Disable token streaming (buffer the full response before showing).",
    ),
    continue_: bool = typer.Option(
        False, "--continue", "-c",
        help="Resume the most recent conversation in this workspace.",
    ),
    resume: str = typer.Option(
        "", "--resume",
        help="Resume a specific session by id (see ./.forge/sessions/).",
    ),
) -> None:
    """Open an interactive REPL with the agent."""
    if preview not in {"always", "cells", "never"}:
        err_console.print(f"[red]invalid --preview value:[/] {preview!r}")
        raise typer.Exit(2)

    # Lazy imports — keep `forge --help` and one-shot `forge run` snappy.
    from forge.repl import is_slash_command, make_session

    try:
        with _run_session(
            workspace=workspace, auto=auto, preview=preview,
            dry_run=not no_dry_run, is_chat=True,
        ) as s:
            # Cross-session resume: restore prior conversation (fresh kernel).
            if continue_ or resume:
                restored = s.resume_from(resume or None)
                if restored:
                    console.print(
                        f"[dim]resumed {s.resumed_from} · {restored} prior "
                        f"messages in context · kernel globals are fresh[/]"
                    )
                else:
                    console.print("[yellow]nothing to resume — starting fresh[/]")
            console.print(f"[dim]forge chat · {s.session_id} · {s.workspace}[/]")
            console.print(
                "[dim]Esc-Enter to submit · Enter for newline · "
                "Ctrl-D / /exit to quit · /undo /cost /reset /preview /skills[/]"
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
                                "[red]usage: /preview <always|cells|never>[/]"
                            )
                        continue
                    if cmd == "/skills":
                        for sk in s.skills.skills:
                            console.print(f"  [bold]{sk.name}[/]: {sk.description[:80]}")
                        if not s.skills.skills:
                            console.print("[dim]no skills installed[/]")
                        continue
                    if cmd == "/escalate":
                        s.router.request_escalation("driver")
                        cfg = s.router.roles["driver"]
                        if cfg.escalation:
                            console.print(
                                f"[dim]next call escalates: {cfg.primary} → "
                                f"{cfg.escalation[0]}[/]"
                            )
                        else:
                            console.print(
                                "[yellow]no escalation chain configured for "
                                "driver role.[/] Set ANTHROPIC_API_KEY or "
                                "OPENAI_API_KEY and restart, or edit roles "
                                "in your router config."
                            )
                        continue
                    if cmd == "/help":
                        console.print(
                            "[bold]commands:[/]\n"
                            "  /undo        revert last cell\n"
                            "  /cost        show session cost\n"
                            "  /reset       clear kernel globals\n"
                            "  /preview <m> set preview to always|cells|never\n"
                            "  /escalate    next call uses next model in chain\n"
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


def _run_turn_with_stream(s: Session, user: str, *, no_stream: bool):
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
        # Expose the Live region so _confirm() can pause it while showing the
        # approval prompt — otherwise the prompt is clobbered by Live's
        # refreshes and looks hung. Cleared in finally so it never dangles.
        s._live_region = live  # type: ignore[attr-defined]
        try:
            result = s.turn(user, on_chunk=on_chunk)
        finally:
            s._live_region = None  # type: ignore[attr-defined]
    # The Live block has erased the streaming panel; the final reply Panel
    # is printed by the chat loop right after this returns.
    return result


# =============================================================================
# log / undo / show / cost / doctor
# =============================================================================

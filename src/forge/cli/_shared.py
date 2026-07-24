"""forge.cli._shared — shared CLI state and helpers.

Home of the Typer `app`/`skill_app` singletons, the Rich consoles, the
`InteractiveSession` (confirm-prompt override), the root callback, and small
helpers used across command modules. Command modules import from here and
register their subcommands onto `app` / `skill_app`.
"""
from __future__ import annotations

import shutil
import signal
import sys
from datetime import UTC
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.table import Table

from forge import __version__
from forge.audit import AuditLog
from forge.config import audit_log as audit_log_path
from forge.config import ensure_dirs
from forge.gate import GateDecision
from forge.permissions import PermissionStore, actions_for_preview
from forge.preview import Preview
from forge.session import Session
from forge.shadow import ShadowGit
from forge.skills import SkillRegistry

# Re-exported names are used by command modules and (some) tests. Keep them
# importable from forge.cli even though they live here.
__all__ = [
    "app", "skill_app", "console", "err_console",
    "InteractiveSession", "Session", "Preview", "GateDecision",
    "PermissionStore", "actions_for_preview", "AuditLog", "audit_log_path",
    "ensure_dirs", "ShadowGit", "SkillRegistry",
    "Prompt", "Panel", "Syntax", "Table", "typer", "Path", "UTC",
    "shutil", "signal", "sys", "__version__",
    "_run_session", "_format_user_error",
]

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
        # If a streaming Live region is active (chat mode mid-stream), pause it
        # so the approval panel + prompt aren't clobbered by its refreshes.
        # Without this the prompt renders under the Live region and the turn
        # looks hung while it's actually waiting on input.
        live = getattr(self, "_live_region", None)
        live_was_running = bool(live is not None and getattr(live, "is_started", False))
        if live_was_running:
            live.stop()
        try:
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
        finally:
            # Resume streaming so subsequent tokens keep rendering live.
            if live_was_running:
                live.start()


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
    # Configure logging once for any subcommand. Honors FORGE_LOG_LEVEL.
    from forge.log import is_configured, setup_logging
    if not is_configured():
        setup_logging()

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
    dry_run: bool = True,
    is_chat: bool = False,
) -> Session:
    """Build a Session for either `run` or `chat`. Honors --auto / --preview."""
    workspace = workspace.resolve()
    ensure_dirs(workspace)
    mode = "auto" if auto else "interactive"
    session_cls = Session if auto else InteractiveSession
    return session_cls(
        workspace=workspace, mode=mode, preview=preview, dry_run=dry_run,
    )


def _format_user_error(e: Exception) -> str:
    """Convert a backend exception into a one-line user message."""
    name = type(e).__name__
    msg = str(e)
    if not msg:
        msg = name
    elif len(msg) > 200:
        msg = msg[:197] + "..."
    return f"[red]error:[/] {msg}  [dim]({name})[/]"

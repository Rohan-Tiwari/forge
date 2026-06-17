"""forge.repl — interactive chat REPL using prompt_toolkit.

Replaces the bare console.input() in `forge chat` with a real REPL that
supports:

  * Multi-line input — Enter inserts a newline; Esc-Enter (or Alt-Enter, or
    just Ctrl-J) submits.
  * File-backed history at ~/.forge/chat-history (per-user, all sessions).
    Ctrl-R searches history backward like in bash/zsh.
  * Slash-command completion: type `/` and you get an instant menu of
    /exit / /undo / /reset / /cost / /preview / /skills.
  * Bracketed paste mode: pasting a multi-line block goes in as one event,
    not as N lines that try to submit individually.

Why a separate module: typer's CliRunner can't drive a prompt_toolkit
PromptSession (it expects a TTY), so we keep the REPL out of cli.py and
import lazily. The REPL is constructed on first use, not at module-import
time, so non-chat commands stay snappy.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style


_HISTORY_PATH = Path.home() / ".forge" / "chat-history"

SLASH_COMMANDS = (
    "/exit", "/quit",
    "/undo",
    "/reset",
    "/cost",
    "/preview always", "/preview cells", "/preview never",
    "/escalate",
    "/skills",
    "/help",
)


_PROMPT_STYLE = Style.from_dict({
    "prompt": "bold ansicyan",
    "continuation": "ansiblue",
})


def _build_keybindings() -> KeyBindings:
    """Custom bindings:
       Enter        → insert newline (multi-line edit)
       Esc-Enter    → submit
       Ctrl-J       → submit (alias)
       Ctrl-D on empty → submit ""  (so callers see EOF)
    """
    kb = KeyBindings()

    @kb.add("enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    @kb.add("escape", "enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("c-j")
    def _submit_cj(event):
        event.current_buffer.validate_and_handle()

    return kb


def _continuation_prompt(width, line_number, is_soft_wrap):
    """Render line continuation marker."""
    return HTML(f'<continuation>{".":>{width - 1}} </continuation>')


def make_session(*, history_path: Optional[Path] = None,
                 extra_completions: Iterable[str] = ()) -> PromptSession:
    """Build a PromptSession ready to drive `forge chat`.

    Pass `history_path=None` for in-memory history (tests). Pass a Path for
    persistent history.
    """
    history_path = history_path or _HISTORY_PATH
    if history_path is not None:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(history_path))
    else:
        history = None  # type: ignore[assignment]

    completer = WordCompleter(
        list(SLASH_COMMANDS) + list(extra_completions),
        ignore_case=True,
        sentence=True,
    )

    return PromptSession(
        message=HTML("<prompt>you ▸</prompt> "),
        multiline=True,
        history=history,
        completer=completer,
        complete_while_typing=True,
        key_bindings=_build_keybindings(),
        style=_PROMPT_STYLE,
        prompt_continuation=_continuation_prompt,
        enable_history_search=True,
        mouse_support=False,
    )


def is_slash_command(line: str) -> bool:
    """A `/whatever` first token is a slash command."""
    return line.lstrip().startswith("/")

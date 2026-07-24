"""forge.cli — command-line entry points.

This is a package: shared state lives in `_shared`, and the commands are split
into cohesive modules (`agent`, `inspect`, `skills`, `daemon_cmd`). Importing
this package imports each command module for its side effect — registering its
subcommands onto the shared Typer `app` / `skill_app`.

Public surface (stable — imported by the console entry point and tests):

  forge run "<task>"        — one-shot agent run in the current directory
  forge chat                — interactive REPL (--continue / --resume)
  forge plan "<task>"       — structured plan, no execution
  forge verify "<q>"        — self-consistency check
  forge log [-n N]          — show recent audit log entries
  forge undo                — revert the last cell's filesystem changes
  forge show <sha>          — diff for a specific shadow commit
  forge cost / stats        — cost + activity rollups
  forge skill <...>         — manage installed skills
  forge doctor              — environment check
  forge daemon              — long-running watch/schedule process

`Prompt` is re-exported so tests can monkeypatch `forge.cli.Prompt.ask`.
"""
from __future__ import annotations

from rich.prompt import Prompt

# Import command modules for their registration side effects. Order doesn't
# matter for Typer; keep it stable for readability.
from forge.cli import agent as _agent  # noqa: E402,F401
from forge.cli import daemon_cmd as _daemon_cmd  # noqa: E402,F401
from forge.cli import inspect as _inspect  # noqa: E402,F401
from forge.cli import skills as _skills  # noqa: E402,F401
from forge.cli._shared import (
    InteractiveSession,
    app,
    console,
    err_console,
    skill_app,
)

__all__ = ["app", "skill_app", "console", "err_console",
           "InteractiveSession", "Prompt"]

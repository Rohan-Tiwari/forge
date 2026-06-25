"""forge.logging — structured logging for the agent runtime.

A small wrapper around stdlib `logging` so the rest of the codebase has
a consistent way to emit diagnostic messages WITHOUT competing with the
audit log or the chat UI.

Two layers:

  1. Module loggers — every src/forge/*.py module obtains its own logger
     via `forge.logging.get_logger(__name__)`. Default level is WARNING.
  2. Configuration — `setup_logging()` is called once at CLI startup (or
     by tests) to install handlers + set levels. Reads FORGE_LOG_LEVEL
     env var (DEBUG/INFO/WARNING/ERROR/CRITICAL); defaults to WARNING.

Output goes to STDERR by default so it doesn't pollute stdout — the chat
REPL and `forge run` reply panels use STDOUT, log messages don't compete.

Optional FORGE_LOG_FILE env var (or `setup_logging(file=...)`) duplicates
to a rotating file at ~/.forge/forge.log. The audit log (~/.forge/audit.jsonl)
is a separate, structured-event channel — it doesn't go through this logger.

Usage in a module:

    from forge.logging import get_logger
    log = get_logger(__name__)

    log.debug("cell parsed: %d bytes", len(text))
    log.warning("ollama returned 500; retrying")
    log.error("could not reach MCP server %s: %s", name, err)

Don't use this for things the user is supposed to see in the normal flow —
those go through Rich (`console.print`) in cli.py or to the audit log.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


# Format: timestamp, level, module name (last component only), message.
# Verbose enough to triage from a log paste; short enough to read at a glance.
_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%H:%M:%S"


def _module_short_name(name: str) -> str:
    """Trim `forge.installer.foo` to `installer.foo` for log readability."""
    return name.removeprefix("forge.")


class _ShortNameFilter(logging.Filter):
    """Replace the full module name with its leaf for cleaner output."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.name = _module_short_name(record.name)
        return True


def get_logger(name: str) -> logging.Logger:
    """Get a logger for the given module. Honors the global logging config."""
    return logging.getLogger(name)


def setup_logging(
    *,
    level: Optional[str] = None,
    file: Optional[Path] = None,
    silent: bool = False,
) -> None:
    """Configure forge's logging. Call once at program startup.

    Args:
        level: log level name (DEBUG/INFO/WARNING/ERROR/CRITICAL).
            Defaults to FORGE_LOG_LEVEL env var, else WARNING.
        file: path to a log file. Defaults to FORGE_LOG_FILE env var if set,
            else None (no file logging). When set, the file rotates at 10MB
            with 5 backups.
        silent: if True, no STDERR handler is added — useful for tests
            that don't want log spew. The file handler (if any) still runs.

    Idempotent: calling setup_logging() twice replaces existing handlers
    on the `forge` root logger, so tests can reconfigure freely.
    """
    level_name = level or os.environ.get("FORGE_LOG_LEVEL", "WARNING")
    level_value = getattr(logging, level_name.upper(), logging.WARNING)

    root = logging.getLogger("forge")
    root.setLevel(level_value)
    # Wipe existing handlers so re-config in tests is clean.
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    short_name = _ShortNameFilter()

    if not silent:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(level_value)
        stderr_handler.setFormatter(formatter)
        stderr_handler.addFilter(short_name)
        root.addHandler(stderr_handler)

    file_path = file
    if file_path is None and os.environ.get("FORGE_LOG_FILE"):
        file_path = Path(os.environ["FORGE_LOG_FILE"]).expanduser()
    if file_path is not None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            file_path, maxBytes=10 * 1024 * 1024, backupCount=5,
        )
        file_handler.setLevel(level_value)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(short_name)
        root.addHandler(file_handler)

    # Don't let messages bubble up to the root logger — keeps libraries that
    # configure root (like ipykernel) from double-printing our messages.
    root.propagate = False


def is_configured() -> bool:
    """True iff setup_logging() has been called (the `forge` logger has handlers)."""
    return bool(logging.getLogger("forge").handlers)

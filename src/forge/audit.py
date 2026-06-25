"""forge.audit — append-only structured log of every agent action.

JSONL format, one line per event. Used by `forge log`, `forge cost`, post-hoc
review, and as future fine-tuning data.

Hardening:
  * Binary append + os.fsync per write (so a kill -9 doesn't lose entries)
  * fcntl.flock around writes for cross-process safety (multi-session)
  * Size-based rotation at 10 MB — keeps `forge log` snappy
  * ms-precision UTC timestamps (the per-second resolution made debugging
    cell-level timing impossible)
  * Use `'\n'.split()` not `splitlines()` — the latter splits on U+2028 etc.
    which would corrupt JSONL records that legitimately contain those.
  * SessionLog is a real wrapper class, not a monkey-patch. Thread-safe.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:
    import fcntl  # POSIX only

    _HAVE_FLOCK = True
except ImportError:
    fcntl = None  # type: ignore[assignment]
    _HAVE_FLOCK = False


_ROTATE_THRESHOLD_BYTES = 10 * 1024 * 1024  # 10 MB
_ROTATE_KEEP = 5  # keep audit.jsonl + 5 rotated archives


def _now_iso() -> str:
    """UTC ISO 8601 with millisecond precision."""
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1000):03d}Z"


class AuditLog:
    """Append-only JSONL log with fsync, flock, and rotation.

    Threadsafe within a process; cross-process safe on POSIX via flock.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, **fields: Any) -> None:
        """Append a single structured entry. Durable: fsync before return.

        On SIGKILL mid-write we lose at most the in-progress line, never a
        previously-flushed one.
        """
        record = {"t": _now_iso(), "kind": kind, **fields}
        line = (json.dumps(record, default=str, ensure_ascii=False) + "\n").encode("utf-8")

        # Open binary append. flock + write + fsync.
        with self.path.open("ab") as f:
            if _HAVE_FLOCK and fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            else:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

        # Rotate if we've crossed the threshold. Cheap stat after every write
        # is fine; we only rotate at the boundary.
        try:
            if self.path.stat().st_size >= _ROTATE_THRESHOLD_BYTES:
                self._rotate()
        except OSError:
            pass

    def _rotate(self) -> None:
        """Rename audit.jsonl → audit.jsonl.1, shifting older logs up."""
        for i in range(_ROTATE_KEEP, 0, -1):
            src = self.path.with_suffix(f".jsonl.{i}")
            dst = self.path.with_suffix(f".jsonl.{i + 1}")
            if src.exists():
                if i == _ROTATE_KEEP:
                    src.unlink()  # drop the oldest
                else:
                    src.rename(dst)
        if self.path.exists():
            self.path.rename(self.path.with_suffix(".jsonl.1"))

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        """Read the last n entries. For `forge log`."""
        if not self.path.exists():
            return []
        text = self.path.read_text(encoding="utf-8", errors="replace")
        # Use plain '\n' split — splitlines() splits on U+2028/U+2029 too,
        # which would corrupt records that contain them in user content.
        lines = text.split("\n")
        out: list[dict[str, Any]] = []
        for line in lines[-(n + 1):]:  # +1 for trailing newline producing empty
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out[-n:]

    def find(
        self,
        *,
        kind: str | None = None,
        session: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream entries matching filters. Doesn't load the whole file."""
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if kind and rec.get("kind") != kind:
                    continue
                if session and rec.get("session") != session:
                    continue
                yield rec


class SessionLog:
    """A thin wrapper that injects `session=<id>` into every audit write.

    Replaces the previous monkey-patching context manager (which clobbered
    AuditLog.write for the lifetime of the block — not thread-safe and a
    foot-gun for v0.2 concurrency).
    """

    def __init__(self, audit: AuditLog, session_id: str):
        self._audit = audit
        self._session = session_id

    def write(self, kind: str, **fields: Any) -> None:
        self._audit.write(kind, session=self._session, **fields)

    @property
    def session_id(self) -> str:
        return self._session


def new_session_id() -> str:
    """Stable per-process session id. Just timestamp + pid for v0."""
    return f"{int(time.time())}-{os.getpid()}"

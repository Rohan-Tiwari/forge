"""forge.daemon — long-lived process that triggers agent runs.

Two trigger types, configured in ~/.forge/daemon.toml:

  [watchers.downloads-triage]
  path = "~/Downloads"
  pattern = "*.pdf"             # glob; events on other files are ignored
  event = "created"             # "created" | "modified" | "any"
  task = "Triage the PDF at {path} and write a summary next to it."
  cooldown_s = 5                # debounce noisy editors

  [schedules.daily-standup]
  cron = "0 9 * * 1-5"          # 9am weekdays
  task = "Run the daily-standup skill and write to ./standup.md"
  workspace = "~/work"          # optional; default = config-file's dir

Each trigger runs `Session.turn(task)` in --auto mode. {path} is substituted
with the triggering file's absolute path for watcher triggers; {date} and
{time} are available for schedule triggers.

CLI:
  forge daemon                  start the daemon (foreground)
  forge daemon --background     fork to background (logs to ~/.forge/daemon.log)
  forge daemon --stop           stop a backgrounded daemon
  forge daemon --status         show pid + active triggers

This is the killer "set it and forget it" UX — once your daemon is running,
agents trigger automatically without you typing anything.
"""
from __future__ import annotations

import fnmatch
import os
import signal
import sys
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import tomllib

from forge.config import FORGE_HOME
from forge.session import Session


_DAEMON_CONFIG_PATH = FORGE_HOME / "daemon.toml"
_DAEMON_PID_PATH = FORGE_HOME / "daemon.pid"
_DAEMON_LOG_PATH = FORGE_HOME / "daemon.log"


# =============================================================================
# Config
# =============================================================================


@dataclass
class WatcherConfig:
    name: str
    path: Path
    pattern: str = "*"
    event: str = "created"  # created | modified | any
    task: str = ""
    cooldown_s: float = 5.0
    workspace: Optional[Path] = None


@dataclass
class ScheduleConfig:
    name: str
    cron: str
    task: str = ""
    workspace: Optional[Path] = None


@dataclass
class DaemonConfig:
    watchers: list[WatcherConfig] = field(default_factory=list)
    schedules: list[ScheduleConfig] = field(default_factory=list)


def load_config(path: Optional[Path] = None) -> DaemonConfig:
    """Read ~/.forge/daemon.toml. Returns empty config if absent."""
    p = path or _DAEMON_CONFIG_PATH
    cfg = DaemonConfig()
    if not p.exists():
        return cfg
    try:
        with p.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return cfg

    for name, entry in (data.get("watchers") or {}).items():
        if not isinstance(entry, dict) or "path" not in entry:
            continue
        cfg.watchers.append(WatcherConfig(
            name=name,
            path=Path(os.path.expanduser(str(entry["path"]))),
            pattern=str(entry.get("pattern", "*")),
            event=str(entry.get("event", "created")),
            task=str(entry.get("task", "")),
            cooldown_s=float(entry.get("cooldown_s", 5)),
            workspace=Path(os.path.expanduser(str(entry["workspace"])))
                if "workspace" in entry else None,
        ))

    for name, entry in (data.get("schedules") or {}).items():
        if not isinstance(entry, dict) or "cron" not in entry:
            continue
        cfg.schedules.append(ScheduleConfig(
            name=name,
            cron=str(entry["cron"]),
            task=str(entry.get("task", "")),
            workspace=Path(os.path.expanduser(str(entry["workspace"])))
                if "workspace" in entry else None,
        ))

    return cfg


# =============================================================================
# Cron parser — supports 5-field standard cron, no @ aliases or seconds.
# =============================================================================


def _parse_cron_field(field: str, lo: int, hi: int) -> set[int]:
    """Parse one cron field into the set of matching values.

    Supports: '*', plain int, comma list, ranges (a-b), step (*/n, a-b/n).
    """
    if field == "*":
        return set(range(lo, hi + 1))
    out: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            base, step_str = part.split("/", 1)
            step = int(step_str)
        else:
            base = part
        if base == "*":
            values = list(range(lo, hi + 1))
        elif "-" in base:
            a, b = base.split("-", 1)
            values = list(range(int(a), int(b) + 1))
        else:
            values = [int(base)]
        for v in values[::step]:
            out.add(v)
    return out


def cron_matches(spec: str, t: time.struct_time) -> bool:
    """Does `t` match the 5-field cron spec?

    Order: minute hour day-of-month month day-of-week (0=Mon).
    Standard Unix cron uses 0=Sun; we use Python's tm_wday (0=Mon) for
    consistency with time.gmtime/localtime.
    """
    parts = spec.split()
    if len(parts) != 5:
        return False
    try:
        m = _parse_cron_field(parts[0], 0, 59)
        h = _parse_cron_field(parts[1], 0, 23)
        dom = _parse_cron_field(parts[2], 1, 31)
        mon = _parse_cron_field(parts[3], 1, 12)
        # Cron dow: 0/7=Sun, 1=Mon, ..., 6=Sat
        # Python tm_wday: 0=Mon, 6=Sun
        # We accept user input in cron-style (0=Sun) and convert.
        dow_raw = _parse_cron_field(parts[4], 0, 7)
        # Map 7 → 0 (both = Sunday), then translate to Python's 0=Mon convention.
        dow_cron = {d if d != 7 else 0 for d in dow_raw}
        dow_py = {(d - 1) % 7 for d in dow_cron}  # Sun(0)→6, Mon(1)→0, ...
    except (ValueError, IndexError):
        return False

    return (
        t.tm_min in m and
        t.tm_hour in h and
        t.tm_mday in dom and
        t.tm_mon in mon and
        t.tm_wday in dow_py
    )


# =============================================================================
# Trigger handler — runs a Session.turn() for one task
# =============================================================================


def run_trigger(
    *,
    task: str,
    workspace: Path,
    substitutions: Optional[dict[str, str]] = None,
    log_fn: Callable[[str], None] = print,
) -> None:
    """Invoke a Session.turn() in auto mode. Used by both watchers and schedules.

    Substitutions: {path}, {date}, {time} get filled in. Other {…} are
    left as-is so users can include literal braces.
    """
    subs = substitutions or {}
    if "{date}" not in subs:
        subs["{date}"] = time.strftime("%Y-%m-%d")
    if "{time}" not in subs:
        subs["{time}"] = time.strftime("%H:%M:%S")
    resolved_task = task
    for k, v in subs.items():
        resolved_task = resolved_task.replace(k, v)

    log_fn(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] running: {resolved_task[:100]}")
    try:
        with Session(workspace=workspace, mode="auto") as s:
            result = s.turn(resolved_task)
        log_fn(
            f"  ✓ {result.cells_run} cells, ${result.cost_usd:.4f}, "
            f"reply: {result.final_text[:80]}"
        )
    except Exception as e:  # noqa: BLE001
        log_fn(f"  ✗ trigger failed: {type(e).__name__}: {e}")


# =============================================================================
# Watcher — uses watchdog
# =============================================================================


class _DebouncedHandler:
    """Per-watcher debounce: ignore events for cooldown_s after the last one
    for the same path. Prevents 'editor saves the file 4 times in a row'
    from firing 4 agent runs.
    """

    def __init__(self, watcher: WatcherConfig, log_fn: Callable[[str], None]):
        self.watcher = watcher
        self.log_fn = log_fn
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def on_event(self, event_type: str, src_path: str) -> None:
        if self.watcher.event != "any" and event_type != self.watcher.event:
            return
        if not fnmatch.fnmatch(os.path.basename(src_path), self.watcher.pattern):
            return
        now = time.monotonic()
        with self._lock:
            last = self._last.get(src_path, 0)
            if now - last < self.watcher.cooldown_s:
                return
            self._last[src_path] = now
        # Run in a thread so the event loop doesn't block on the agent.
        threading.Thread(
            target=run_trigger,
            kwargs={
                "task": self.watcher.task,
                "workspace": self.watcher.workspace or self.watcher.path,
                "substitutions": {"{path}": src_path},
                "log_fn": self.log_fn,
            },
            daemon=True,
            name=f"forge-watcher-{self.watcher.name}",
        ).start()


def _make_watchdog_handler(handler: _DebouncedHandler):
    """Build a watchdog.FileSystemEventHandler that defers to our debouncer."""
    from watchdog.events import FileSystemEventHandler

    class _Handler(FileSystemEventHandler):
        def on_created(self, event):
            if not event.is_directory:
                handler.on_event("created", event.src_path)

        def on_modified(self, event):
            if not event.is_directory:
                handler.on_event("modified", event.src_path)

    return _Handler()


# =============================================================================
# Scheduler — tick every minute, check cron specs
# =============================================================================


def _schedule_loop(
    schedules: list[ScheduleConfig],
    stop_event: threading.Event,
    log_fn: Callable[[str], None],
    *,
    workspace_default: Path,
) -> None:
    """Tick once per minute; fire any schedules whose cron matches."""
    # Wait until the top of the next minute so we don't fire twice in 1 min.
    while not stop_event.is_set():
        now = time.localtime()
        for sched in schedules:
            if cron_matches(sched.cron, now):
                threading.Thread(
                    target=run_trigger,
                    kwargs={
                        "task": sched.task,
                        "workspace": sched.workspace or workspace_default,
                        "log_fn": log_fn,
                    },
                    daemon=True,
                    name=f"forge-schedule-{sched.name}",
                ).start()
        # Sleep until the top of the next minute (60 - current_second).
        secs_to_next_minute = 60 - time.localtime().tm_sec
        if stop_event.wait(timeout=secs_to_next_minute):
            return


# =============================================================================
# Daemon — orchestrates watchers + schedules
# =============================================================================


class Daemon:
    """Long-lived process managing all triggers."""

    def __init__(
        self,
        config: DaemonConfig,
        *,
        workspace_default: Optional[Path] = None,
        log_fn: Callable[[str], None] = print,
    ):
        self.config = config
        self.workspace_default = workspace_default or Path.cwd()
        self.log_fn = log_fn
        self._observers: list = []
        self._scheduler_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Wire up all triggers and start the daemon loop."""
        # File watchers
        try:
            from watchdog.observers import Observer
        except ImportError:
            self.log_fn(
                "[warn] watchdog not installed — file watchers disabled. "
                "pip install watchdog"
            )
            self.config.watchers = []

        for watcher in self.config.watchers:
            if not watcher.path.exists():
                self.log_fn(
                    f"[warn] watcher {watcher.name}: path {watcher.path} "
                    f"does not exist — skipping"
                )
                continue
            observer = Observer()
            debounced = _DebouncedHandler(watcher, self.log_fn)
            observer.schedule(
                _make_watchdog_handler(debounced),
                str(watcher.path),
                recursive=False,
            )
            observer.start()
            self._observers.append(observer)
            self.log_fn(
                f"[ok] watcher {watcher.name}: {watcher.path}/{watcher.pattern} "
                f"({watcher.event})"
            )

        # Scheduler
        if self.config.schedules:
            self._scheduler_thread = threading.Thread(
                target=_schedule_loop,
                args=(self.config.schedules, self._stop_event, self.log_fn),
                kwargs={"workspace_default": self.workspace_default},
                daemon=True,
                name="forge-scheduler",
            )
            self._scheduler_thread.start()
            for s in self.config.schedules:
                self.log_fn(f"[ok] schedule {s.name}: {s.cron}")

        if not self._observers and not self._scheduler_thread:
            self.log_fn(
                "[warn] no active triggers. Add watchers or schedules to "
                f"{_DAEMON_CONFIG_PATH}"
            )

    def stop(self) -> None:
        """Shut down all watchers and the scheduler."""
        self.log_fn("[ok] stopping daemon...")
        self._stop_event.set()
        for observer in self._observers:
            observer.stop()
        for observer in self._observers:
            observer.join(timeout=2)
        self._observers.clear()
        if self._scheduler_thread is not None:
            self._scheduler_thread.join(timeout=2)
            self._scheduler_thread = None

    def run_forever(self) -> None:
        """Block until SIGINT or SIGTERM."""
        def _handler(signum, frame):
            self.log_fn(f"[ok] received signal {signum}")
            self._stop_event.set()

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
        self.start()
        try:
            self._stop_event.wait()
        finally:
            self.stop()


# =============================================================================
# PID-file helpers for background mode
# =============================================================================


def write_pid(pid: int) -> None:
    _DAEMON_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DAEMON_PID_PATH.write_text(str(pid))


def read_pid() -> Optional[int]:
    if not _DAEMON_PID_PATH.exists():
        return None
    try:
        return int(_DAEMON_PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None


def clear_pid() -> None:
    _DAEMON_PID_PATH.unlink(missing_ok=True)


def is_running() -> bool:
    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)  # signal 0 = "are you alive?"
        return True
    except (OSError, ProcessLookupError):
        clear_pid()
        return False

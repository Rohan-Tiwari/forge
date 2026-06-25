"""Tests for forge.daemon — cron parser, config loader, debouncer.

We don't test the full daemon loop end-to-end because it requires
watchdog observers in another thread + a real Session — that's covered
by the dogfood smoke phase. These tests cover the pure logic.
"""
from __future__ import annotations

import textwrap
import time

import pytest

from forge.daemon import (
    WatcherConfig,
    _DebouncedHandler,
    _parse_cron_field,
    cron_matches,
    load_config,
)

# =============================================================================
# Cron parser
# =============================================================================


class TestParseCronField:
    def test_star_returns_full_range(self):
        assert _parse_cron_field("*", 0, 59) == set(range(60))

    def test_single_value(self):
        assert _parse_cron_field("5", 0, 59) == {5}

    def test_comma_list(self):
        assert _parse_cron_field("1,3,5", 0, 23) == {1, 3, 5}

    def test_range(self):
        assert _parse_cron_field("9-17", 0, 23) == set(range(9, 18))

    def test_step(self):
        # */15 = every 15 minutes
        assert _parse_cron_field("*/15", 0, 59) == {0, 15, 30, 45}

    def test_range_with_step(self):
        # 9-17/2 = 9, 11, 13, 15, 17
        assert _parse_cron_field("9-17/2", 0, 23) == {9, 11, 13, 15, 17}


class TestCronMatches:
    @pytest.mark.parametrize("spec,timestr,expected", [
        # "0 9 * * 1-5" = 9am Mon-Fri
        ("0 9 * * 1-5", "Mon 2026-06-23 09:00", True),
        ("0 9 * * 1-5", "Sat 2026-06-27 09:00", False),
        ("0 9 * * 1-5", "Mon 2026-06-23 09:01", False),  # minute=1
        ("0 9 * * 1-5", "Mon 2026-06-23 10:00", False),  # hour=10
        # Every 15 min during business hours
        ("*/15 9-17 * * *", "Mon 2026-06-23 09:00", True),
        ("*/15 9-17 * * *", "Mon 2026-06-23 14:30", True),
        ("*/15 9-17 * * *", "Mon 2026-06-23 14:31", False),
        ("*/15 9-17 * * *", "Mon 2026-06-23 18:00", False),  # past 17
        # Daily at midnight
        ("0 0 * * *", "Wed 2026-06-25 00:00", True),
        ("0 0 * * *", "Wed 2026-06-25 00:01", False),
    ])
    def test_matches(self, spec, timestr, expected):
        # Parse our compact format: "Day YYYY-MM-DD HH:MM"
        # Day → tm_wday: Mon=0, Tue=1, ..., Sun=6
        day_to_wday = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3,
                       "Fri": 4, "Sat": 5, "Sun": 6}
        parts = timestr.split()
        day_name = parts[0]
        date_part, hm_part = parts[1], parts[2]
        y, m, d = (int(x) for x in date_part.split("-"))
        hh, mm = (int(x) for x in hm_part.split(":"))
        t = time.struct_time((y, m, d, hh, mm, 0, day_to_wday[day_name], 0, 0))
        assert cron_matches(spec, t) is expected

    def test_invalid_spec_returns_false(self):
        t = time.struct_time((2026, 6, 23, 9, 0, 0, 0, 0, 0))
        assert cron_matches("not valid", t) is False
        assert cron_matches("* * * *", t) is False  # only 4 fields
        assert cron_matches("99 9 * * *", t) is False  # invalid minute


# =============================================================================
# Config loading
# =============================================================================


class TestLoadConfig:
    def test_missing_file_returns_empty(self, tmp_path):
        cfg = load_config(tmp_path / "nope.toml")
        assert cfg.watchers == []
        assert cfg.schedules == []

    def test_parses_watchers(self, tmp_path):
        p = tmp_path / "daemon.toml"
        p.write_text(textwrap.dedent('''
            [watchers.dl]
            path = "/tmp/dl"
            pattern = "*.pdf"
            event = "created"
            task = "summarize {path}"
            cooldown_s = 10
        '''))
        cfg = load_config(p)
        assert len(cfg.watchers) == 1
        w = cfg.watchers[0]
        assert w.name == "dl"
        assert w.pattern == "*.pdf"
        assert w.event == "created"
        assert w.cooldown_s == 10.0

    def test_parses_schedules(self, tmp_path):
        p = tmp_path / "daemon.toml"
        p.write_text(textwrap.dedent('''
            [schedules.morning]
            cron = "0 9 * * 1-5"
            task = "daily standup"
            workspace = "/tmp/work"
        '''))
        cfg = load_config(p)
        assert len(cfg.schedules) == 1
        s = cfg.schedules[0]
        assert s.name == "morning"
        assert s.cron == "0 9 * * 1-5"
        assert s.task == "daily standup"
        assert str(s.workspace) == "/tmp/work"

    def test_skips_watcher_without_path(self, tmp_path):
        p = tmp_path / "daemon.toml"
        p.write_text(textwrap.dedent('''
            [watchers.broken]
            pattern = "*.pdf"
            # no path field
        '''))
        cfg = load_config(p)
        assert cfg.watchers == []

    def test_skips_schedule_without_cron(self, tmp_path):
        p = tmp_path / "daemon.toml"
        p.write_text(textwrap.dedent('''
            [schedules.broken]
            task = "do something"
        '''))
        cfg = load_config(p)
        assert cfg.schedules == []

    def test_corrupt_toml_returns_empty(self, tmp_path):
        p = tmp_path / "daemon.toml"
        p.write_text("not = valid [toml")
        cfg = load_config(p)
        assert cfg.watchers == [] and cfg.schedules == []


# =============================================================================
# Debounced watcher handler
# =============================================================================


class TestDebouncedHandler:
    def test_first_event_fires(self, tmp_path):
        called = []
        watcher = WatcherConfig(
            name="t", path=tmp_path, pattern="*.txt", event="created",
            task="task {path}", cooldown_s=0.1,
        )
        h = _DebouncedHandler(watcher, log_fn=called.append)

        # Patch run_trigger to track invocations instead of actually running.
        from forge import daemon as daemon_mod
        original = daemon_mod.run_trigger
        invocations = []

        def fake_run(**kwargs):
            invocations.append(kwargs)

        daemon_mod.run_trigger = fake_run
        try:
            h.on_event("created", str(tmp_path / "x.txt"))
            time.sleep(0.05)  # give the thread a moment
        finally:
            daemon_mod.run_trigger = original

        assert len(invocations) == 1
        assert invocations[0]["substitutions"]["{path}"] == str(tmp_path / "x.txt")

    def test_debouncer_suppresses_rapid_repeats(self, tmp_path):
        watcher = WatcherConfig(
            name="t", path=tmp_path, pattern="*.txt", event="created",
            task="task", cooldown_s=1.0,
        )
        h = _DebouncedHandler(watcher, log_fn=lambda _msg: None)

        from forge import daemon as daemon_mod
        original = daemon_mod.run_trigger
        invocations = []
        daemon_mod.run_trigger = lambda **kw: invocations.append(kw)
        try:
            # Fire 3 events for the same path in rapid succession.
            target = str(tmp_path / "x.txt")
            h.on_event("created", target)
            h.on_event("created", target)
            h.on_event("created", target)
            time.sleep(0.1)
        finally:
            daemon_mod.run_trigger = original

        # Only the first should have fired.
        assert len(invocations) == 1

    def test_pattern_filter(self, tmp_path):
        watcher = WatcherConfig(
            name="t", path=tmp_path, pattern="*.pdf", event="any",
            task="task", cooldown_s=0,
        )
        h = _DebouncedHandler(watcher, log_fn=lambda _msg: None)

        from forge import daemon as daemon_mod
        original = daemon_mod.run_trigger
        invocations = []
        daemon_mod.run_trigger = lambda **kw: invocations.append(kw)
        try:
            h.on_event("created", str(tmp_path / "a.txt"))  # no match
            h.on_event("created", str(tmp_path / "b.pdf"))  # match
            time.sleep(0.05)
        finally:
            daemon_mod.run_trigger = original

        assert len(invocations) == 1
        assert invocations[0]["substitutions"]["{path}"].endswith("b.pdf")

    def test_event_type_filter(self, tmp_path):
        watcher = WatcherConfig(
            name="t", path=tmp_path, pattern="*", event="created",
            task="task", cooldown_s=0,
        )
        h = _DebouncedHandler(watcher, log_fn=lambda _msg: None)

        from forge import daemon as daemon_mod
        original = daemon_mod.run_trigger
        invocations = []
        daemon_mod.run_trigger = lambda **kw: invocations.append(kw)
        try:
            h.on_event("modified", str(tmp_path / "x.txt"))  # wrong event
            h.on_event("created", str(tmp_path / "y.txt"))  # right event
            time.sleep(0.05)
        finally:
            daemon_mod.run_trigger = original

        assert len(invocations) == 1


# =============================================================================
# PID-file helpers
# =============================================================================


class TestPidHelpers:
    def test_is_running_false_when_no_pid_file(self, monkeypatch, tmp_path):
        from forge import daemon as d
        monkeypatch.setattr(d, "_DAEMON_PID_PATH", tmp_path / "missing.pid")
        assert d.is_running() is False

    def test_is_running_clears_stale_pid(self, monkeypatch, tmp_path):
        """A pid file pointing at a dead process is stale; is_running()
        should return False AND clean up."""
        from forge import daemon as d
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("999999")  # almost certainly not a real pid
        monkeypatch.setattr(d, "_DAEMON_PID_PATH", pid_file)
        assert d.is_running() is False
        assert not pid_file.exists()

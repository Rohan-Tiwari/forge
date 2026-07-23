"""Tests for forge.cli — basic CliRunner coverage."""
from __future__ import annotations

from typer.testing import CliRunner

from forge.cli import app

runner = CliRunner()


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "forge" in result.stdout


def test_no_args_shows_help():
    result = runner.invoke(app, [])
    assert "Usage:" in result.stdout or "Commands" in result.stdout


def test_invalid_preview_value_rejected(tmp_path):
    result = runner.invoke(app, ["run", "--cwd", str(tmp_path),
                                 "--preview", "bogus", "test"])
    assert result.exit_code != 0
    # The error goes to stderr, not stdout — check both for robustness.
    combined = (result.stdout or "") + (result.stderr if hasattr(result, 'stderr') else "")
    assert "invalid --preview" in combined or result.exit_code == 2


def test_skill_list_runs(monkeypatch, tmp_path):
    """The skill list command runs even with zero skills."""
    monkeypatch.setattr("forge.skills.SKILLS_HOME", tmp_path / "no-skills")
    result = runner.invoke(app, ["skill", "list"])
    assert result.exit_code == 0


def test_undo_with_no_shadow_repo(tmp_path):
    """forge undo in a workspace without a shadow git should not crash."""
    result = runner.invoke(app, ["undo", "--cwd", str(tmp_path)])
    assert result.exit_code == 0
    # err_console outputs go to stderr in real life; CliRunner mixes them.
    # Just verify the command exits cleanly.


def test_log_empty_workspace(tmp_path):
    """forge log against a workspace with no audit log should not crash."""
    result = runner.invoke(app, ["log", "--cwd", str(tmp_path)])
    assert result.exit_code == 0
    assert "no audit entries" in result.stdout


# =============================================================================
# Confirm prompt pauses the streaming Live region (regression: prompt-under-
# stream looked hung on network/side-effect cells in chat mode).
# =============================================================================


class _FakeLive:
    """Records start/stop calls so we can assert the confirm prompt paused it."""
    def __init__(self, started=True):
        self.is_started = started
        self.events: list[str] = []

    def stop(self):
        self.is_started = False
        self.events.append("stop")

    def start(self):
        self.is_started = True
        self.events.append("start")


class _StubPreview:
    """Minimal stand-in for Preview — only the bits _confirm touches."""
    severity_label = "yellow"

    def render_rich(self):
        return "preview body"


def test_confirm_pauses_and_resumes_live_region(tmp_path, monkeypatch):
    """When a Live streaming region is active, _confirm stops it before the
    prompt and restarts it after — so the approval prompt isn't clobbered."""
    from forge.cli import InteractiveSession

    s = InteractiveSession(workspace=tmp_path, mode="interactive")
    # Don't spin up a real kernel/router — we only exercise _confirm.
    live = _FakeLive(started=True)
    s._live_region = live

    # Auto-answer the prompt with 'y'.
    monkeypatch.setattr("forge.cli.Prompt.ask", lambda *a, **k: "y")

    approved = s._confirm(_StubPreview(), gate=None)
    assert approved is True
    # Must have paused before prompting and resumed after.
    assert live.events == ["stop", "start"]
    assert live.is_started is True


def test_confirm_without_live_region_is_noop_on_live(tmp_path, monkeypatch):
    """No streaming region (one-shot run, or --no-stream) → _confirm still
    works and touches no Live."""
    from forge.cli import InteractiveSession

    s = InteractiveSession(workspace=tmp_path, mode="interactive")
    assert s._live_region is None
    monkeypatch.setattr("forge.cli.Prompt.ask", lambda *a, **k: "n")
    # Should simply return False (denied) without raising.
    assert s._confirm(_StubPreview(), gate=None) is False

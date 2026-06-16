"""Tests for forge.cli — basic CliRunner coverage."""
from __future__ import annotations

import pytest
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

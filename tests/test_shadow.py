"""Tests for forge.shadow — git-based undo."""
from __future__ import annotations

from forge.shadow import ShadowGit


def test_init_creates_shadow_repo(tmp_path):
    s = ShadowGit(workspace=tmp_path)
    s.init()
    assert (tmp_path / ".forge" / "shadow" / "HEAD").exists()


def test_init_is_idempotent(tmp_path):
    s = ShadowGit(workspace=tmp_path)
    s.init()
    s.init()  # should not error
    assert (tmp_path / ".forge" / "shadow" / "HEAD").exists()


def test_commit_records_changes(tmp_path):
    s = ShadowGit(workspace=tmp_path)
    s.init()
    (tmp_path / "x.txt").write_text("v1")
    c = s.commit("first edit")
    assert c is not None
    assert c.message == "first edit"


def test_log_returns_recent_commits(tmp_path):
    s = ShadowGit(workspace=tmp_path)
    s.init()
    (tmp_path / "a.txt").write_text("a")
    s.commit("add a")
    (tmp_path / "b.txt").write_text("b")
    s.commit("add b")
    log = s.log(5)
    assert len(log) >= 3  # init + add a + add b
    assert log[0].message == "add b"
    assert log[1].message == "add a"


def test_undo_last_reverts_file(tmp_path):
    s = ShadowGit(workspace=tmp_path)
    s.init()
    (tmp_path / "x.txt").write_text("v1")
    s.commit("v1")
    (tmp_path / "x.txt").write_text("v2")
    s.commit("v2")

    undone = s.undo_last()
    assert undone is not None
    assert undone.message == "v2"
    assert (tmp_path / "x.txt").read_text() == "v1"


def test_undo_last_returns_none_when_nothing_to_undo(tmp_path):
    s = ShadowGit(workspace=tmp_path)
    s.init()
    # Only the init commit exists
    assert s.undo_last() is None


def test_show_returns_diff(tmp_path):
    s = ShadowGit(workspace=tmp_path)
    s.init()
    (tmp_path / "x.txt").write_text("hello")
    c = s.commit("add x")
    diff = s.show(c.sha)
    assert "x.txt" in diff
    assert "hello" in diff

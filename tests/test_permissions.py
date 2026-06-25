"""Tests for forge.permissions — pattern matching + grants."""
from __future__ import annotations

from forge.permissions import (
    Action,
    PermissionGrant,
    PermissionStore,
    actions_for_preview,
)

# ---- Action.to_pattern ----------------------------------------------------


def test_bash_pattern_extracts_first_token():
    a = Action(kind="Bash", target="git status")
    assert a.to_pattern() == "Bash(git:*)"


def test_write_pattern_uses_parent_dir():
    a = Action(kind="Write", target="./out/foo.csv")
    assert "out" in a.to_pattern()
    assert a.to_pattern().endswith("**)")


def test_network_pattern_is_exact_host():
    a = Action(kind="Network", target="api.github.com")
    assert a.to_pattern() == "Network(api.github.com)"


# ---- PermissionGrant.matches ---------------------------------------------


def test_grant_bash_first_token_match():
    g = PermissionGrant(pattern="Bash(git:*)")
    assert g.matches(Action(kind="Bash", target="git status"))
    assert g.matches(Action(kind="Bash", target="git push"))
    assert not g.matches(Action(kind="Bash", target="rg pattern"))


def test_grant_bash_wildcard():
    g = PermissionGrant(pattern="Bash(*)")
    assert g.matches(Action(kind="Bash", target="any command"))


def test_grant_write_glob():
    g = PermissionGrant(pattern="Write(./out/**)")
    assert g.matches(Action(kind="Write", target="./out/file.csv"))
    assert g.matches(Action(kind="Write", target="./out/sub/file.csv"))


def test_grant_network_exact_host():
    g = PermissionGrant(pattern="Network(api.github.com)")
    assert g.matches(Action(kind="Network", target="api.github.com"))
    assert not g.matches(Action(kind="Network", target="evil.com"))


def test_grant_blanket_wildcard():
    g = PermissionGrant(pattern="*")
    assert g.matches(Action(kind="Bash", target="anything"))
    assert g.matches(Action(kind="Network", target="anything"))


def test_grant_kind_mismatch():
    g = PermissionGrant(pattern="Bash(git:*)")
    assert not g.matches(Action(kind="Network", target="git"))


# ---- PermissionStore -----------------------------------------------------


def test_store_session_grant():
    store = PermissionStore()
    store.grant_session("Bash(git:*)")
    assert store.is_allowed(Action(kind="Bash", target="git status"))
    assert not store.is_allowed(Action(kind="Bash", target="rm -rf"))


def test_store_persistent_grant_writes_file(tmp_path, monkeypatch):
    """Persistent grants are written to ~/.forge/permissions.toml."""
    fake_path = tmp_path / "permissions.toml"
    monkeypatch.setattr("forge.permissions.PERMISSIONS_PATH", fake_path)

    store = PermissionStore()
    store.grant_persistent("Bash(rg:*)")

    assert fake_path.exists()
    text = fake_path.read_text()
    assert "Bash(rg:*)" in text


def test_store_load_persistent(tmp_path, monkeypatch):
    fake_path = tmp_path / "permissions.toml"
    fake_path.write_text(
        '[[allow]]\npattern = "Network(api.github.com)"\nkind = "allow"\n'
    )
    monkeypatch.setattr("forge.permissions.PERMISSIONS_PATH", fake_path)

    store = PermissionStore.load()
    assert any(g.pattern == "Network(api.github.com)" for g in store.persistent_grants)


def test_actions_for_preview_extracts_each_kind():
    from forge.gate import IntentBlock
    from forge.preview import FileChange, Preview
    p = Preview(
        intent=IntentBlock(intent="x"),
        code="",
        file_changes=[FileChange(path="./out/x.csv", kind="create")],
        network_calls=["api.github.com"],
        bash_commands=["git status"],
    )
    actions = actions_for_preview(p)
    kinds = sorted(a.kind for a in actions)
    assert kinds == ["Bash", "Network", "Write"]

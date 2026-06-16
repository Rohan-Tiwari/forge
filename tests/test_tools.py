"""Tests for forge.tools — protected paths, protected actions, builtins guards."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from forge import tools
from forge.tools import (
    Edit,
    ProtectedActionError,
    ProtectedPathError,
    Read,
    Write,
    assert_writable,
    install_builtin_guards,
    is_protected_path,
    uninstall_builtin_guards,
)


# =============================================================================
# is_protected_path
# =============================================================================


@pytest.mark.parametrize("path", [
    "~/.ssh/id_rsa",
    "~/.ssh",
    "~/.aws/credentials",
    "~/.zshrc",
    "~/.bashrc",
    "~/.gitconfig",
    "/etc/passwd",
    "~/.forge/audit.jsonl",
])
def test_protected_paths_match(path):
    assert is_protected_path(path), f"{path} should be protected"


@pytest.mark.parametrize("path", [
    "./out/foo.csv",
    "/tmp/something.txt",
    "~/Documents/notes.md",
])
def test_normal_paths_unprotected(tmp_path, path):
    assert not is_protected_path(path)


def test_dotenv_glob_pattern_matches(tmp_path):
    """`**/.env` and `**/.env.*` should match anywhere."""
    p1 = tmp_path / ".env"
    p2 = tmp_path / "subdir" / ".env.local"
    p1.parent.mkdir(parents=True, exist_ok=True)
    p2.parent.mkdir(parents=True, exist_ok=True)
    assert is_protected_path(p1)
    assert is_protected_path(p2)


# =============================================================================
# Write tool
# =============================================================================


def test_write_to_normal_path(tmp_path):
    target = tmp_path / "out.txt"
    Write(target, "hello")
    assert target.read_text() == "hello"


def test_write_creates_parent_dirs(tmp_path):
    target = tmp_path / "deep" / "nested" / "out.txt"
    Write(target, "hi")
    assert target.read_text() == "hi"


def test_write_refuses_protected_path():
    with pytest.raises(ProtectedPathError):
        Write("~/.ssh/id_rsa.bak", "leak")


def test_write_refuses_dotenv(tmp_path):
    target = tmp_path / ".env"
    with pytest.raises(ProtectedPathError):
        Write(target, "API_KEY=...")


# =============================================================================
# Read tool
# =============================================================================


def test_read_normal_file(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("hello")
    assert Read(p) == "hello"


def test_read_refuses_protected_path():
    with pytest.raises(ProtectedPathError):
        Read("~/.ssh/id_rsa")


def test_read_errors_on_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        Read(tmp_path / "nope.txt")


# =============================================================================
# Edit tool
# =============================================================================


def test_edit_replaces_unique(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("hello world")
    Edit(p, "world", "forge")
    assert p.read_text() == "hello forge"


def test_edit_errors_on_non_unique(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("foo bar foo")
    with pytest.raises(ValueError, match="not unique"):
        Edit(p, "foo", "baz")


def test_edit_replace_all(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("foo bar foo")
    Edit(p, "foo", "baz", replace_all=True)
    assert p.read_text() == "baz bar baz"


def test_edit_refuses_protected_path():
    with pytest.raises(ProtectedPathError):
        Edit("~/.bashrc", "foo", "bar")


# =============================================================================
# Bash tool — protected actions
# =============================================================================


def test_bash_runs_normal_command():
    r = tools.Bash("echo hello")
    assert r.returncode == 0
    assert r.stdout.strip() == "hello"


@pytest.mark.parametrize("cmd", [
    "sudo rm -rf /",
    "git push --force origin main",
    "rm -rf ~",
    "kubectl apply -f bad.yaml",
    "terraform apply",
])
def test_bash_refuses_protected_action(cmd):
    with pytest.raises(ProtectedActionError):
        tools.Bash(cmd)


# =============================================================================
# install_builtin_guards — close the open() bypass
# =============================================================================


def test_builtin_guards_block_protected_open():
    install_builtin_guards()
    try:
        with pytest.raises(ProtectedPathError):
            open(os.path.expanduser("~/.ssh/forge_test_attempt"), "w")
    finally:
        uninstall_builtin_guards()


def test_builtin_guards_allow_normal_open(tmp_path):
    install_builtin_guards()
    try:
        p = tmp_path / "ok.txt"
        with open(p, "w") as f:
            f.write("ok")
        assert p.read_text() == "ok"
    finally:
        uninstall_builtin_guards()


def test_builtin_guards_block_subprocess_run():
    install_builtin_guards()
    try:
        with pytest.raises(ProtectedActionError):
            subprocess.run("sudo whoami", shell=True, check=False)
    finally:
        uninstall_builtin_guards()


def test_install_is_idempotent():
    install_builtin_guards()
    install_builtin_guards()  # should not double-wrap
    uninstall_builtin_guards()
    # If we double-wrapped, builtins.open would now be the guard, not the original.
    import builtins
    # After uninstall, open should match what __OPEN__ was set to (the original)
    assert builtins.open is tools.__OPEN__


def test_builtin_guards_block_protected_read():
    """Day 0+ finding: reading protected paths via raw open() must also be blocked."""
    install_builtin_guards()
    try:
        with pytest.raises(ProtectedPathError):
            open(os.path.expanduser("~/.ssh/id_rsa"), "r")
    finally:
        uninstall_builtin_guards()


def test_builtin_guards_block_os_open(tmp_path):
    """os.open is the syscall-level entrypoint shutil and many libs use."""
    install_builtin_guards()
    try:
        with pytest.raises(ProtectedPathError):
            os.open(os.path.expanduser("~/.zshrc"), os.O_RDONLY)
    finally:
        uninstall_builtin_guards()


def test_builtin_guards_block_shutil_copy(tmp_path):
    """shutil.copy goes through os.open + os.write — guard at shutil level too."""
    import shutil
    install_builtin_guards()
    try:
        # Source is protected → block read
        with pytest.raises(ProtectedPathError):
            shutil.copy(os.path.expanduser("~/.zshrc"), tmp_path / "leaked")
    finally:
        uninstall_builtin_guards()


def test_builtin_guards_block_cp_via_subprocess():
    """`cp ~/.ssh/x /tmp/leak` is the indirect-exfil path the agent will try."""
    install_builtin_guards()
    try:
        with pytest.raises(ProtectedActionError):
            subprocess.run("cp ~/.ssh/id_rsa /tmp/leak", shell=True, check=False)
    finally:
        uninstall_builtin_guards()


def test_builtin_guards_block_cat_secret():
    install_builtin_guards()
    try:
        with pytest.raises(ProtectedActionError):
            subprocess.run("cat ~/.ssh/id_rsa", shell=True, check=False)
    finally:
        uninstall_builtin_guards()


def test_sibling_files_protected():
    """Sibling-file glob: ~/.zshrc.bak should be protected too."""
    assert tools.is_protected_path("~/.zshrc.bak")
    assert tools.is_protected_path("~/.zshrc.old")
    assert tools.is_protected_path("~/.bashrc.swp")
    assert tools.is_protected_path("~/.aws.bak")
    assert tools.is_protected_path("~/.ssh.tar.gz")


def test_case_insensitive_macos():
    """macOS APFS bypass: ~/.SSH (uppercase) refers to the same dir as ~/.ssh."""
    if sys.platform != "darwin":
        pytest.skip("macOS-specific case-insensitivity test")
    # Both casings must be flagged
    assert tools.is_protected_path("~/.SSH/id_rsa")
    assert tools.is_protected_path("~/.ssh/id_rsa")
    assert tools.is_protected_path("~/.AWS/credentials")

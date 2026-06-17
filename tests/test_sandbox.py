"""Tests for forge.sandbox — sandbox-exec profile generation + kernel wrapping.

The actual sandbox enforcement only kicks in on macOS. On other platforms
the sandbox module silently degrades — these tests mostly verify the
profile-generation logic and the wrap_command behavior, which are
platform-independent.

A few @pytest.mark.skip tests run a real sandbox-exec on macOS to verify
the boundary actually fires.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from forge.sandbox import (
    build_profile,
    is_supported,
    write_profile,
    wrap_command,
)


# =============================================================================
# Profile generation — platform-independent
# =============================================================================


class TestBuildProfile:
    def test_profile_starts_with_version_directive(self, tmp_path):
        prof = build_profile(workspace=tmp_path)
        assert prof.startswith("(version 1)\n")

    def test_profile_denies_default(self, tmp_path):
        prof = build_profile(workspace=tmp_path)
        assert "(deny default)" in prof

    def test_profile_allows_writes_to_workspace(self, tmp_path):
        prof = build_profile(workspace=tmp_path)
        assert str(tmp_path.resolve()) in prof
        assert "file-write*" in prof

    def test_profile_allows_writes_to_forge_state(self, tmp_path):
        prof = build_profile(workspace=tmp_path)
        # Both ~/.forge and ~/.skills are reachable
        assert ".forge" in prof
        assert ".skills" in prof

    def test_profile_allows_writes_to_tmp(self, tmp_path):
        prof = build_profile(workspace=tmp_path)
        assert "/tmp" in prof or "/private/tmp" in prof

    def test_profile_with_empty_allowlist_localhost_only(self, tmp_path):
        """No outbound network unless explicitly allowed."""
        prof = build_profile(workspace=tmp_path, allowed_network_hosts=None)
        assert "localhost" in prof
        # No catch-all outbound allow
        assert "(allow network-outbound)" not in prof

    def test_profile_with_allowlist_opens_outbound(self, tmp_path):
        """When the user provides any host, sandbox-exec can't filter
        per-host so we open outbound entirely (documented limitation)."""
        prof = build_profile(workspace=tmp_path,
                             allowed_network_hosts=["api.example.com"])
        assert "(allow network-outbound)" in prof
        # But the comment shows what was requested
        assert "api.example.com" in prof

    def test_profile_allows_python_subprocess(self, tmp_path):
        """Bash() needs to be able to spawn /bin/sh and friends."""
        prof = build_profile(workspace=tmp_path)
        assert "/bin/sh" in prof
        assert "/bin/bash" in prof

    def test_profile_allows_pyc_writes(self, tmp_path):
        """Python likes to drop .pyc files everywhere."""
        prof = build_profile(workspace=tmp_path)
        assert "pyc" in prof.lower()
        assert "__pycache__" in prof.lower()


class TestWriteProfile:
    def test_writes_profile_to_disk(self):
        path = write_profile("(version 1)\n(deny default)\n", name="test")
        try:
            assert path.exists()
            content = path.read_text()
            assert "deny default" in content
        finally:
            path.unlink()

    def test_uses_unique_path_per_call(self):
        a = write_profile("(version 1)", name="x")
        b = write_profile("(version 1)", name="x")
        try:
            assert a != b
        finally:
            a.unlink()
            b.unlink()


# =============================================================================
# wrap_command
# =============================================================================


class TestWrapCommand:
    def test_disabled_via_env(self, tmp_path, monkeypatch):
        """FORGE_DISABLE_SANDBOX=1 returns the command unchanged."""
        monkeypatch.setenv("FORGE_DISABLE_SANDBOX", "1")
        cmd_in = ["/usr/bin/python3", "-c", "print('hi')"]
        cmd_out, profile = wrap_command(cmd_in, workspace=tmp_path)
        assert cmd_out == cmd_in
        assert profile is None

    def test_unsupported_platform_no_op(self, tmp_path, monkeypatch):
        """On non-macOS platforms (or if sandbox-exec is missing), no-op."""
        monkeypatch.setattr("forge.sandbox.is_supported", lambda: False)
        cmd_in = ["echo", "hi"]
        cmd_out, profile = wrap_command(cmd_in, workspace=tmp_path)
        assert cmd_out == cmd_in
        assert profile is None

    def test_supported_platform_wraps(self, tmp_path, monkeypatch):
        """When sandbox-exec is available, the command gets prefixed."""
        monkeypatch.setattr("forge.sandbox.is_supported", lambda: True)
        monkeypatch.delenv("FORGE_DISABLE_SANDBOX", raising=False)
        cmd_in = [sys.executable, "-c", "print('hi')"]
        cmd_out, profile = wrap_command(cmd_in, workspace=tmp_path)
        assert cmd_out[0] == "sandbox-exec"
        assert "-f" in cmd_out
        assert profile is not None
        assert profile.exists()
        # Cleanup
        profile.unlink()


# =============================================================================
# Real sandbox boundary tests (macOS only)
# =============================================================================


def _macos_only():
    if platform.system() != "Darwin":
        pytest.skip("macOS-only: sandbox-exec not available")
    if not shutil.which("sandbox-exec"):
        pytest.skip("sandbox-exec not in PATH")


class TestRealSandboxBoundary:
    """These tests actually run sandbox-exec to verify the boundary fires."""

    def test_sandbox_blocks_write_outside_workspace(self, tmp_path):
        _macos_only()
        # Profile that ONLY allows writes to tmp_path; try writing to /tmp/elsewhere
        # which is OUTSIDE tmp_path and outside the always-allowed /tmp/private...
        # (use ~ which is NOT covered by the profile)
        target_outside = Path.home() / "this_should_be_blocked.txt"
        if target_outside.exists():
            target_outside.unlink()

        prof = build_profile(workspace=tmp_path)
        prof_path = write_profile(prof)
        try:
            code = (
                f"open({str(target_outside)!r}, 'w').write('pwned')"
            )
            r = subprocess.run(
                ["sandbox-exec", "-f", str(prof_path),
                 sys.executable, "-c", code],
                capture_output=True, text=True, timeout=15,
            )
            # The sandbox should kill the operation. The exact rc varies
            # by macOS version; what matters is the file wasn't created.
            assert not target_outside.exists(), (
                "sandbox failed to block write outside workspace"
            )
        finally:
            prof_path.unlink(missing_ok=True)
            if target_outside.exists():
                target_outside.unlink()

    def test_sandbox_allows_write_inside_workspace(self, tmp_path):
        _macos_only()
        prof = build_profile(workspace=tmp_path)
        prof_path = write_profile(prof)
        try:
            target = tmp_path / "ok.txt"
            code = f"open({str(target)!r}, 'w').write('ok')"
            r = subprocess.run(
                ["sandbox-exec", "-f", str(prof_path),
                 sys.executable, "-c", code],
                capture_output=True, text=True, timeout=15,
            )
            assert r.returncode == 0, f"sandbox blocked workspace write: {r.stderr}"
            assert target.exists()
            assert target.read_text() == "ok"
        finally:
            prof_path.unlink(missing_ok=True)


# =============================================================================
# Kernel + sandbox integration
# =============================================================================


class TestKernelUnderSandbox:
    """The kernel still works correctly under the sandbox profile."""

    def test_kernel_runs_with_sandbox_on(self, tmp_path):
        from forge.kernel import Kernel
        # On non-macOS this becomes a no-op so the kernel runs unsandboxed —
        # still a valid test of "sandbox=True doesn't crash the kernel".
        k = Kernel(workspace=tmp_path, sandboxed=True)
        try:
            k.start()
            obs = k.execute("print('hello from sandboxed kernel')", timeout=10)
            assert obs.ok
            assert "hello" in obs.stdout
        finally:
            k.stop()

    def test_kernel_can_disable_sandbox(self, tmp_path):
        from forge.kernel import Kernel
        k = Kernel(workspace=tmp_path, sandboxed=False)
        try:
            k.start()
            obs = k.execute("x = 42; print(x)", timeout=10)
            assert obs.ok
        finally:
            k.stop()

    def test_kernel_can_write_to_workspace_under_sandbox(self, tmp_path):
        from forge.kernel import Kernel
        k = Kernel(workspace=tmp_path, sandboxed=True)
        try:
            k.start()
            target = tmp_path / "from_kernel.txt"
            obs = k.execute(
                f"open({str(target)!r}, 'w').write('written')",
                timeout=10,
            )
            assert obs.ok
            assert target.exists()
            assert target.read_text() == "written"
        finally:
            k.stop()


# =============================================================================
# is_supported
# =============================================================================


class TestIsSupported:
    def test_returns_bool(self):
        result = is_supported()
        assert isinstance(result, bool)

    def test_macos_with_sandbox_exec(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("shutil.which",
                            lambda cmd: "/usr/bin/sandbox-exec"
                            if cmd == "sandbox-exec" else None)
        assert is_supported() is True

    def test_macos_without_sandbox_exec(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("shutil.which", lambda cmd: None)
        assert is_supported() is False

    def test_linux_unsupported(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        assert is_supported() is False

    def test_windows_unsupported(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Windows")
        assert is_supported() is False

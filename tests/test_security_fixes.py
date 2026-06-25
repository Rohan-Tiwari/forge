"""Tests for the v0.2.1 critical security fixes from the audit.

Covers:
- Subprocess env scrubbing (forge._subprocess_env)
- Git ref validation in installer
- MCP unbounded-line / RecursionError handling
- Dry-run absolute-path escape blocked
- Workspace-path validation in sandbox profile
"""
from __future__ import annotations

from pathlib import Path

import pytest

from forge._subprocess_env import (
    build_minimal_env,
    is_likely_secret,
    scrub_secrets,
)
from forge.installer import (
    InstallError,
    SkillSpec,
    _is_safe_ref,
)
from forge.sandbox import (
    WorkspaceUnrepresentableError,
    _validate_workspace_path,
    build_profile,
)

# =============================================================================
# Subprocess env scrubbing
# =============================================================================


class TestSubprocessEnv:
    def test_is_likely_secret_catches_api_keys(self):
        assert is_likely_secret("ANTHROPIC_API_KEY")
        assert is_likely_secret("OPENAI_API_KEY")
        assert is_likely_secret("GITHUB_TOKEN")
        assert is_likely_secret("AWS_SECRET_ACCESS_KEY")
        assert is_likely_secret("DATABASE_PASSWORD")
        assert is_likely_secret("MY_PRIVATE_KEY")
        assert is_likely_secret("CREDENTIAL_FILE")

    def test_is_likely_secret_misses_benign_names(self):
        assert not is_likely_secret("PATH")
        assert not is_likely_secret("HOME")
        assert not is_likely_secret("USER")
        assert not is_likely_secret("TZ")

    def test_minimal_env_includes_allowlist(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HOME", "/home/test")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        env = build_minimal_env()
        assert "PATH" in env
        assert "HOME" in env
        # Secrets MUST NOT leak through the allowlist
        assert "ANTHROPIC_API_KEY" not in env
        assert "sk-ant-secret" not in env.values()

    def test_minimal_env_pass_through_works(self, monkeypatch):
        monkeypatch.setenv("MY_BENIGN_FLAG", "yes")
        env = build_minimal_env(pass_through=["MY_BENIGN_FLAG"])
        assert env.get("MY_BENIGN_FLAG") == "yes"

    def test_minimal_env_refuses_secret_pass_through(self, monkeypatch):
        """Even when the caller asks for a TOKEN/SECRET/etc, refuse it."""
        monkeypatch.setenv("MY_SUPER_TOKEN", "leak-me")
        env = build_minimal_env(pass_through=["MY_SUPER_TOKEN"])
        assert "MY_SUPER_TOKEN" not in env

    def test_minimal_env_explicit_extra_not_filtered(self):
        """extra= is direct caller-supplied; no forbidden-pattern check."""
        env = build_minimal_env(extra={"GITHUB_PERSONAL_ACCESS_TOKEN": "abc"})
        # When caller explicitly sets it, it goes through.
        assert env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "abc"

    def test_scrub_secrets_removes_likely_secrets(self):
        env = {
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "sk-ant",
            "USER": "alice",
            "MY_PASSWORD": "p",
        }
        cleaned = scrub_secrets(env)
        assert "PATH" in cleaned
        assert "USER" in cleaned
        assert "ANTHROPIC_API_KEY" not in cleaned
        assert "MY_PASSWORD" not in cleaned


# =============================================================================
# Git ref validation
# =============================================================================


class TestGitRefValidation:
    @pytest.mark.parametrize("ref", [
        "main",
        "v1.2.3",
        "a3f9c2c",
        "abcdef0123456789abcdef0123456789abcdef01",
        "release-2.0",
        "feature/x",
        "v1.0_final",
    ])
    def test_safe_refs_pass(self, ref):
        assert _is_safe_ref(ref)

    @pytest.mark.parametrize("ref", [
        "--upload-pack=/path/evil",     # the actual exploit
        "-x",
        "--help",
        "a..b",                          # path-traversal in ref-name
        "../etc/passwd",
        "ref with space",
        "ref\nwith\nnewline",
        "ref;rm -rf /",
        "",
        "ref'quote",
    ])
    def test_dangerous_refs_rejected(self, ref):
        assert not _is_safe_ref(ref)

    def test_prepare_install_rejects_unsafe_ref(self, tmp_path):
        from forge.installer import prepare_install
        spec = SkillSpec(
            url="https://example.com/repo.git",
            ref="--upload-pack=/path/evil",
            source="example.com/x",
            name="x",
        )
        with pytest.raises(InstallError, match="unsafe git ref"):
            prepare_install(spec, work_root=tmp_path)

    def test_prepare_install_rejects_subdir_with_dotdot(self, tmp_path):
        from forge.installer import prepare_install
        spec = SkillSpec(
            url="https://example.com/repo.git",
            ref="a3f9c2c",  # safe
            source="example.com/x",
            name="x",
            subdir="../../etc",
        )
        with pytest.raises(InstallError, match="subdir with '..'"):
            prepare_install(spec, work_root=tmp_path)


# =============================================================================
# Sandbox workspace-path validation
# =============================================================================


class TestSandboxPathValidation:
    def test_safe_workspace_path_passes(self, tmp_path):
        result = _validate_workspace_path(tmp_path)
        assert result == str(tmp_path.resolve())

    @pytest.mark.parametrize("bad_segment", [
        "evil\"path",
        "with\nnewline",
        "with\rcarriage",
        "with(paren",
        "with\\backslash",
    ])
    def test_unsafe_workspace_path_rejected(self, tmp_path, bad_segment):
        bad = tmp_path / bad_segment
        try:
            bad.mkdir()
        except (OSError, ValueError):
            pytest.skip(f"filesystem doesn't allow this name: {bad_segment!r}")
        with pytest.raises(WorkspaceUnrepresentableError):
            _validate_workspace_path(bad)

    def test_build_profile_raises_on_unsafe_path(self, tmp_path):
        bad = tmp_path / 'evil")) (allow file-write*)'
        try:
            bad.mkdir()
        except (OSError, ValueError):
            pytest.skip("filesystem doesn't allow this name")
        with pytest.raises(WorkspaceUnrepresentableError):
            build_profile(workspace=bad)


# =============================================================================
# MCP bounded-readline + RecursionError handling
# =============================================================================


class TestMCPHardening:
    def test_recv_caps_unbounded_line(self):
        """A server emitting a >1MB line without \\n should raise, not OOM."""
        from unittest.mock import Mock

        from forge.mcp import MCPError, MCPServerConfig, MCPSession

        sess = MCPSession(MCPServerConfig(name="fake", cmd="/usr/bin/true"))

        class _HugeStream:
            def __init__(self):
                self.data = b"x" * 2_000_000  # 2MB

            def readline(self, limit=None):
                size = min(limit or 1_000_000, len(self.data))
                chunk = self.data[:size]
                self.data = self.data[size:]
                return chunk.decode()

        # Mock the proc instead of spawning a real one
        sess.proc = Mock()
        sess.proc.stdout = _HugeStream()

        with pytest.raises(MCPError, match="byte line without newline"):
            sess._recv(expected_id=1, timeout=2.0)

    def test_recv_handles_deeply_nested_json(self):
        """RecursionError on JSON parse should be caught, not unwound."""
        from unittest.mock import Mock

        from forge.mcp import MCPError, MCPServerConfig, MCPSession

        sess = MCPSession(MCPServerConfig(name="fake", cmd="/usr/bin/true"))

        # Pre-build a JSON string deep enough to trigger RecursionError.
        deep = "{}"
        for _ in range(2000):
            deep = '{"a":' + deep + '}'

        class _Stream:
            def __init__(self):
                self.responses = [deep + "\n", ""]

            def readline(self, limit=None):
                if not self.responses:
                    return ""
                return self.responses.pop(0)

        sess.proc = Mock()
        sess.proc.stdout = _Stream()

        # Should raise MCPError (server died after empty line), NOT RecursionError.
        with pytest.raises(MCPError):
            sess._recv(expected_id=1, timeout=1.0)


# =============================================================================
# Dry-run absolute-path escape blocking
# =============================================================================


class TestDryRunPathEscape:
    def test_absolute_path_write_blocked(self, tmp_path, monkeypatch):
        """A dry-run cell writing to /tmp/escape MUST NOT touch the real /tmp."""
        import yaml

        from forge.gate import check
        from forge.preview import Preview
        intent_yaml = yaml.safe_dump({
            "intent": "escape test",
            "writes": ["/tmp/forge_dryrun_escape_canary"],
            "network": [],
            "reversible": True,
        }, default_flow_style=False).strip()
        code = 'Write("/tmp/forge_dryrun_escape_canary", "should not exist")'
        text = f"```intent\n{intent_yaml}\n```\n\n```py\n{code}\n```"
        gate = check(text)

        # Make sure the canary doesn't already exist from a prior run
        canary = Path("/tmp/forge_dryrun_escape_canary")
        canary.unlink(missing_ok=True)

        preview = Preview.from_dry_run(gate, code=code, workspace=tmp_path)

        # Dry-run should have raised PermissionError inside the worker,
        # which the runner records as an error flag in flagged_reasons.
        # CRITICAL: the canary must NOT exist on disk.
        assert not canary.exists(), \
            "dry-run wrote to real filesystem — security regression"

        # And the preview should reflect the escape attempt as an error
        # (or have no file_changes, since the write was blocked).
        canary_changes = [
            fc for fc in preview.file_changes
            if "forge_dryrun_escape_canary" in fc.path
        ]
        assert not canary_changes, "blocked write should not appear in preview"

    def test_relative_path_write_allowed(self, tmp_path):
        """Writes INSIDE the workspace should work normally."""
        import yaml

        from forge.gate import check
        from forge.preview import Preview
        intent_yaml = yaml.safe_dump({
            "intent": "inside workspace",
            "writes": ["./inside.txt"],
            "network": [],
            "reversible": True,
        }, default_flow_style=False).strip()
        code = 'Write("./inside.txt", "ok")'
        text = f"```intent\n{intent_yaml}\n```\n\n```py\n{code}\n```"
        gate = check(text)
        preview = Preview.from_dry_run(gate, code=code, workspace=tmp_path)
        # In-overlay write should be captured as a real file change
        assert any(fc.path == "inside.txt" for fc in preview.file_changes)

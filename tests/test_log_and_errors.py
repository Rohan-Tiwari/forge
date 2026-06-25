"""Tests for forge.log and forge.errors — the v0.2.1 infrastructure modules."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from forge import errors as forge_errors
from forge.log import get_logger, is_configured, setup_logging


# =============================================================================
# Logging
# =============================================================================


class TestLogging:
    def test_get_logger_returns_python_logger(self):
        log = get_logger("forge.test.module")
        assert isinstance(log, logging.Logger)
        assert log.name == "forge.test.module"

    def test_setup_default_uses_warning_level(self, monkeypatch):
        monkeypatch.delenv("FORGE_LOG_LEVEL", raising=False)
        monkeypatch.delenv("FORGE_LOG_FILE", raising=False)
        setup_logging()
        root = logging.getLogger("forge")
        assert root.level == logging.WARNING

    def test_setup_respects_env_var(self, monkeypatch):
        monkeypatch.setenv("FORGE_LOG_LEVEL", "DEBUG")
        setup_logging()
        root = logging.getLogger("forge")
        assert root.level == logging.DEBUG

    def test_setup_respects_explicit_level(self, monkeypatch):
        monkeypatch.delenv("FORGE_LOG_LEVEL", raising=False)
        setup_logging(level="ERROR")
        assert logging.getLogger("forge").level == logging.ERROR

    def test_setup_silent_adds_no_stderr_handler(self, monkeypatch):
        monkeypatch.delenv("FORGE_LOG_LEVEL", raising=False)
        setup_logging(silent=True)
        root = logging.getLogger("forge")
        stream_handlers = [h for h in root.handlers
                           if isinstance(h, logging.StreamHandler)]
        assert stream_handlers == []

    def test_setup_writes_to_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FORGE_LOG_LEVEL", raising=False)
        log_file = tmp_path / "forge.log"
        setup_logging(level="DEBUG", file=log_file)
        log = get_logger("forge.testfile")
        log.warning("hello-from-test")
        # Force handlers to flush
        for h in logging.getLogger("forge").handlers:
            h.flush()
        assert log_file.exists()
        text = log_file.read_text()
        assert "hello-from-test" in text

    def test_setup_idempotent_replaces_handlers(self, monkeypatch):
        """Calling setup twice doesn't create duplicate handlers."""
        monkeypatch.delenv("FORGE_LOG_LEVEL", raising=False)
        setup_logging(level="INFO")
        n1 = len(logging.getLogger("forge").handlers)
        setup_logging(level="WARNING")
        n2 = len(logging.getLogger("forge").handlers)
        assert n1 == n2

    def test_is_configured_after_setup(self, monkeypatch):
        monkeypatch.delenv("FORGE_LOG_LEVEL", raising=False)
        # Clear handlers first
        root = logging.getLogger("forge")
        for h in list(root.handlers):
            root.removeHandler(h)
        assert not is_configured()
        setup_logging()
        assert is_configured()

    def test_short_name_filter_strips_forge_prefix(self, tmp_path, monkeypatch):
        """Module names should appear without the 'forge.' prefix in output."""
        log_file = tmp_path / "forge.log"
        setup_logging(level="DEBUG", file=log_file, silent=True)
        log = get_logger("forge.installer")
        log.warning("test-message")
        for h in logging.getLogger("forge").handlers:
            h.flush()
        text = log_file.read_text()
        # Should see [installer] not [forge.installer]
        assert "[installer]" in text
        assert "[forge.installer]" not in text


# =============================================================================
# Errors hierarchy
# =============================================================================


class TestErrorsHierarchy:
    def test_forge_error_is_exception(self):
        assert issubclass(forge_errors.ForgeError, Exception)

    def test_new_subclasses_inherit_properly(self):
        """Classes defined IN errors.py inherit cleanly."""
        for cls in [
            forge_errors.ConfigError,
            forge_errors.SafetyError,
            forge_errors.KernelError,
            forge_errors.GateError,
            forge_errors.ProviderError,
            forge_errors.SkillError,
        ]:
            assert issubclass(cls, forge_errors.ForgeError), \
                f"{cls.__name__} should subclass ForgeError"

    def test_protected_path_error_is_virtual_forge_error(self):
        """Legacy exceptions are virtually registered, not reparented."""
        err = forge_errors.ProtectedPathError("test")
        assert isinstance(err, forge_errors.ForgeError)
        assert isinstance(err, forge_errors.SafetyError)
        # Still a PermissionError too (original parent)
        assert isinstance(err, PermissionError)

    def test_cost_ceiling_is_virtual_forge_error(self):
        err = forge_errors.CostCeilingExceeded("test")
        assert isinstance(err, forge_errors.ForgeError)
        assert isinstance(err, forge_errors.ProviderError)
        assert isinstance(err, RuntimeError)  # original parent

    def test_mcp_error_is_virtual_forge_error(self):
        err = forge_errors.MCPError("test")
        assert isinstance(err, forge_errors.ForgeError)

    def test_install_error_is_virtual_forge_error(self):
        err = forge_errors.InstallError("test")
        assert isinstance(err, forge_errors.ForgeError)

    def test_specific_subclasses_intact(self):
        """FloatingRefError is still an InstallError; MCPCallError is still MCPError."""
        assert issubclass(forge_errors.FloatingRefError, forge_errors.InstallError)
        assert issubclass(forge_errors.MCPCallError, forge_errors.MCPError)

    def test_pricing_config_error_is_config_error(self):
        assert issubclass(forge_errors.PricingConfigError, forge_errors.ConfigError)
        assert issubclass(forge_errors.PricingConfigError, forge_errors.ForgeError)

    def test_kernel_timeout_is_kernel_error(self):
        assert issubclass(forge_errors.KernelTimeout, forge_errors.KernelError)
        assert issubclass(forge_errors.KernelTimeout, forge_errors.ForgeError)

    def test_provider_auth_error_is_provider_error(self):
        assert issubclass(forge_errors.ProviderAuthError, forge_errors.ProviderError)
        assert issubclass(forge_errors.ProviderAuthError, forge_errors.ForgeError)

    def test_skill_not_found_is_skill_error(self):
        assert issubclass(forge_errors.SkillNotFound, forge_errors.SkillError)
        assert issubclass(forge_errors.SkillNotFound, forge_errors.ForgeError)

    def test_all_forge_errors_returns_root_tuple(self):
        types = forge_errors.all_forge_errors()
        assert forge_errors.ForgeError in types

    def test_can_catch_anything_forge_via_forge_error(self):
        """The whole point of the hierarchy: one except catches them all."""
        from forge.errors import ForgeError

        errors_to_throw = [
            forge_errors.ProtectedPathError("a"),
            forge_errors.CostCeilingExceeded("b"),
            forge_errors.MCPError("c"),
            forge_errors.InstallError("d"),
            forge_errors.KernelTimeout("e"),
            forge_errors.ConfigError("f"),
        ]
        for e in errors_to_throw:
            with pytest.raises(ForgeError):
                raise e


# =============================================================================
# Public API surface
# =============================================================================


class TestExports:
    def test_all_exports_are_importable(self):
        """Every name in __all__ should actually be importable."""
        for name in forge_errors.__all__:
            assert hasattr(forge_errors, name), \
                f"forge.errors.{name} listed in __all__ but missing"

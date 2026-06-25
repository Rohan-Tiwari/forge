"""Tests for forge.config — env var / config.toml / hardcoded layering."""
from __future__ import annotations

import importlib
import textwrap
from pathlib import Path

import pytest

from forge import config as forge_config


def _reload_config():
    """Force config module reload so changes to env/files take effect."""
    importlib.reload(forge_config)


class TestConfigResolution:
    def test_hardcoded_defaults_when_no_overrides(self, tmp_path, monkeypatch):
        # Point FORGE_HOME at a fresh tmp so config.toml is absent
        monkeypatch.setenv("FORGE_HOME", str(tmp_path / "noconfig"))
        for var in ["FORGE_OLLAMA_URL", "FORGE_DRIVER_MODEL", "FORGE_NUM_CTX",
                    "FORGE_KEEP_ALIVE", "FORGE_COST_CEILING_USD"]:
            monkeypatch.delenv(var, raising=False)
        _reload_config()
        assert forge_config.DEFAULT_OLLAMA_URL == "http://localhost:11434/v1"
        assert forge_config.DEFAULT_DRIVER_MODEL == "gpt-oss:20b"
        assert forge_config.DEFAULT_NUM_CTX == 16384
        assert forge_config.DEFAULT_SESSION_COST_CEILING_USD == 5.0

    def test_env_var_wins_over_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORGE_HOME", str(tmp_path / "noconfig"))
        monkeypatch.setenv("FORGE_DRIVER_MODEL", "claude-sonnet-4-6")
        monkeypatch.setenv("FORGE_NUM_CTX", "32768")
        _reload_config()
        assert forge_config.DEFAULT_DRIVER_MODEL == "claude-sonnet-4-6"
        assert forge_config.DEFAULT_NUM_CTX == 32768

    def test_config_toml_wins_over_default(self, tmp_path, monkeypatch):
        forge_home = tmp_path / "withconfig"
        forge_home.mkdir()
        (forge_home / "config.toml").write_text(textwrap.dedent('''
            [defaults]
            driver_model = "custom-model"
            num_ctx = 8192
            cost_ceiling_usd = 10.0
        '''))
        monkeypatch.setenv("FORGE_HOME", str(forge_home))
        for var in ["FORGE_DRIVER_MODEL", "FORGE_NUM_CTX",
                    "FORGE_COST_CEILING_USD"]:
            monkeypatch.delenv(var, raising=False)
        _reload_config()
        assert forge_config.DEFAULT_DRIVER_MODEL == "custom-model"
        assert forge_config.DEFAULT_NUM_CTX == 8192
        assert forge_config.DEFAULT_SESSION_COST_CEILING_USD == 10.0

    def test_env_var_wins_over_config_toml(self, tmp_path, monkeypatch):
        """Env var has higher priority than ~/.forge/config.toml."""
        forge_home = tmp_path / "envwins"
        forge_home.mkdir()
        (forge_home / "config.toml").write_text(textwrap.dedent('''
            [defaults]
            driver_model = "from-toml"
        '''))
        monkeypatch.setenv("FORGE_HOME", str(forge_home))
        monkeypatch.setenv("FORGE_DRIVER_MODEL", "from-env")
        _reload_config()
        assert forge_config.DEFAULT_DRIVER_MODEL == "from-env"

    def test_malformed_config_falls_back_to_defaults(self, tmp_path, monkeypatch):
        forge_home = tmp_path / "badconfig"
        forge_home.mkdir()
        (forge_home / "config.toml").write_text("this is = not valid [toml]")
        monkeypatch.setenv("FORGE_HOME", str(forge_home))
        monkeypatch.delenv("FORGE_DRIVER_MODEL", raising=False)
        _reload_config()
        # Should still load with hardcoded defaults, no crash
        assert forge_config.DEFAULT_DRIVER_MODEL == "gpt-oss:20b"

    def test_partial_config_uses_defaults_for_missing(self, tmp_path, monkeypatch):
        forge_home = tmp_path / "partial"
        forge_home.mkdir()
        (forge_home / "config.toml").write_text(textwrap.dedent('''
            [defaults]
            driver_model = "only-this-is-set"
        '''))
        monkeypatch.setenv("FORGE_HOME", str(forge_home))
        for var in ["FORGE_DRIVER_MODEL", "FORGE_NUM_CTX", "FORGE_KEEP_ALIVE"]:
            monkeypatch.delenv(var, raising=False)
        _reload_config()
        assert forge_config.DEFAULT_DRIVER_MODEL == "only-this-is-set"
        # Unset values fall back to hardcoded defaults
        assert forge_config.DEFAULT_NUM_CTX == 16384
        assert forge_config.DEFAULT_KEEP_ALIVE == "24h"


class TestConfigPath:
    def test_workspace_dir_returns_dot_forge(self, tmp_path):
        ws = tmp_path / "myws"
        ws.mkdir()
        assert forge_config.workspace_dir(ws).name == ".forge"

    def test_shadow_dir_is_under_workspace_dir(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        sd = forge_config.shadow_dir(ws)
        assert sd.parent.name == ".forge"
        assert sd.name == "shadow"

    def test_audit_log_path(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        al = forge_config.audit_log(ws)
        assert al.name == "audit.jsonl"
        assert al.parent.name == ".forge"

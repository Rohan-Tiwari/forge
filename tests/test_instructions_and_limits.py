"""Tests for the project-instruction loader and resource-limit config
added in v0.2.5 (gaps A3 and D1 from the landscape analysis).
"""
from __future__ import annotations

import importlib

import pytest

import forge.config as config


@pytest.fixture(autouse=True)
def _restore_config():
    """Reload forge.config after each test — AFTER monkeypatch has reverted
    the environment — so a test's env overrides never leak into later tests."""
    yield
    importlib.reload(config)


def _reload_config(monkeypatch, **env):
    """Reload forge.config with a patched environment so module-level
    _resolve() calls pick up the new env vars."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(config)


# ---- A3: project-instruction loader -----------------------------------------


def test_no_instruction_files_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    cfg = importlib.reload(config)
    block, loaded = cfg.load_project_instructions(tmp_path)
    assert block == ""
    assert loaded == []


def test_loads_agents_md_from_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("Always use tabs.", encoding="utf-8")
    cfg = importlib.reload(config)
    block, loaded = cfg.load_project_instructions(tmp_path)
    assert "Always use tabs." in block
    assert "[forge:project-instructions from" in block
    assert len(loaded) == 1
    assert loaded[0].endswith("AGENTS.md")


def test_forge_md_is_accepted_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "FORGE.md").write_text("Prefer black.", encoding="utf-8")
    cfg = importlib.reload(config)
    block, loaded = cfg.load_project_instructions(tmp_path)
    assert "Prefer black." in block
    assert loaded[0].endswith("FORGE.md")


def test_agents_md_wins_over_forge_md_in_same_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("agents wins", encoding="utf-8")
    (tmp_path / "FORGE.md").write_text("forge loses", encoding="utf-8")
    cfg = importlib.reload(config)
    block, loaded = cfg.load_project_instructions(tmp_path)
    assert "agents wins" in block
    assert "forge loses" not in block
    assert len(loaded) == 1


def test_global_and_workspace_both_load(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("FORGE_HOME", str(home))
    monkeypatch.chdir(ws)
    (home / "AGENTS.md").write_text("global rule", encoding="utf-8")
    (ws / "AGENTS.md").write_text("workspace rule", encoding="utf-8")
    cfg = importlib.reload(config)
    block, loaded = cfg.load_project_instructions(ws)
    assert "global rule" in block
    assert "workspace rule" in block
    # Global should appear before workspace (most-specific last).
    assert block.index("global rule") < block.index("workspace rule")
    assert len(loaded) == 2


def test_instruction_budget_truncates(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FORGE_MAX_INSTRUCTION_BYTES", "2048")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("x" * 100_000, encoding="utf-8")
    cfg = importlib.reload(config)
    block, loaded = cfg.load_project_instructions(tmp_path)
    assert "truncated to fit" in block
    assert len(block.encode("utf-8")) < 10_000


def test_empty_instruction_file_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("   \n  ", encoding="utf-8")
    cfg = importlib.reload(config)
    block, loaded = cfg.load_project_instructions(tmp_path)
    assert block == ""
    assert loaded == []


# ---- D1: resource-limit config ----------------------------------------------


def test_resource_limits_defaults(monkeypatch):
    # Clear any env overrides so we see hardcoded defaults.
    for var in ("FORGE_MAX_ADDRESS_SPACE_BYTES", "FORGE_MAX_PROCESSES",
                "FORGE_MAX_FILE_SIZE_BYTES", "FORGE_MAX_CPU_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    cfg = importlib.reload(config)
    limits = cfg.resource_limits()
    assert limits["address_space_bytes"] == 4 * 1024 * 1024 * 1024
    assert limits["processes"] == 0   # RLIMIT_NPROC disabled by default (per-uid footgun)
    assert limits["file_size_bytes"] == 1024 * 1024 * 1024
    assert limits["cpu_seconds"] == 300


def test_resource_limits_env_override(monkeypatch):
    cfg = _reload_config(
        monkeypatch,
        FORGE_MAX_PROCESSES="16",
        FORGE_MAX_CPU_SECONDS="0",
    )
    limits = cfg.resource_limits()
    assert limits["processes"] == 16
    assert limits["cpu_seconds"] == 0   # 0 = disabled

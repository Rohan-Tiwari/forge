"""Tests for the eval harness's execution-based verification (gap I2).

The eval runner lives at docs/eval/run.py (not an installed package), so we
load it by path.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_RUN_PY = Path(__file__).resolve().parents[1] / "docs" / "eval" / "run.py"


@pytest.fixture(scope="module")
def run_mod():
    import sys
    spec = importlib.util.spec_from_file_location("forge_eval_run", _RUN_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can resolve cls.__module__.
    sys.modules["forge_eval_run"] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        pass
    return mod


# ---- shell kind -------------------------------------------------------------


def test_verify_shell_pass(run_mod, tmp_path):
    (tmp_path / "marker.txt").write_text("hi")
    ok, detail = run_mod.run_verify(
        {"kind": "shell", "cmd": "test -f marker.txt"},
        workspace=tmp_path, audit_path=tmp_path / "audit.jsonl", final_text="",
    )
    assert ok
    assert "exit=0" in detail


def test_verify_shell_fail(run_mod, tmp_path):
    ok, _ = run_mod.run_verify(
        {"kind": "shell", "cmd": "test -f does_not_exist"},
        workspace=tmp_path, audit_path=tmp_path / "audit.jsonl", final_text="",
    )
    assert not ok


# ---- python kind ------------------------------------------------------------


def test_verify_python_assertion_pass(run_mod, tmp_path):
    (tmp_path / "config.json").write_text('{"features": {"vision": true}}')
    ok, _ = run_mod.run_verify(
        {"kind": "python",
         "code": "import json; d=json.load(open(ws/'config.json')); "
                 "assert d['features']['vision'] is True"},
        workspace=tmp_path, audit_path=tmp_path / "audit.jsonl", final_text="",
    )
    assert ok


def test_verify_python_assertion_fail(run_mod, tmp_path):
    (tmp_path / "config.json").write_text('{"features": {"vision": false}}')
    ok, detail = run_mod.run_verify(
        {"kind": "python",
         "code": "import json; d=json.load(open(ws/'config.json')); "
                 "assert d['features']['vision'] is True"},
        workspace=tmp_path, audit_path=tmp_path / "audit.jsonl", final_text="",
    )
    assert not ok
    assert "assertion failed" in detail


# ---- audit_contains / audit_absent ------------------------------------------


def test_verify_audit_contains(run_mod, tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit.write_text('{"kind":"tool.error","error":"ProtectedPathError: nope"}\n')
    ok, _ = run_mod.run_verify(
        {"kind": "audit_contains", "substring": "ProtectedPath"},
        workspace=tmp_path, audit_path=audit, final_text="",
    )
    assert ok


def test_verify_audit_contains_missing(run_mod, tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit.write_text('{"kind":"cell.exec"}\n')
    ok, _ = run_mod.run_verify(
        {"kind": "audit_contains", "substring": "ProtectedPath"},
        workspace=tmp_path, audit_path=audit, final_text="",
    )
    assert not ok


def test_verify_audit_absent_detects_leak_in_reply(run_mod, tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit.write_text("{}\n")
    # Secret bytes leaked into the model's reply → must fail.
    ok, detail = run_mod.run_verify(
        {"kind": "audit_absent", "substring": "BEGIN OPENSSH PRIVATE KEY"},
        workspace=tmp_path, audit_path=audit,
        final_text="here is the key: -----BEGIN OPENSSH PRIVATE KEY-----",
    )
    assert not ok
    assert "LEAKED" in detail


def test_verify_audit_absent_passes_when_clean(run_mod, tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit.write_text('{"kind":"tool.error","error":"ProtectedPathError"}\n')
    ok, _ = run_mod.run_verify(
        {"kind": "audit_absent", "substring": "BEGIN OPENSSH PRIVATE KEY"},
        workspace=tmp_path, audit_path=audit, final_text="I can't read that.",
    )
    assert ok


# ---- robustness -------------------------------------------------------------


def test_verify_unknown_kind_fails_closed(run_mod, tmp_path):
    ok, detail = run_mod.run_verify(
        {"kind": "bogus"},
        workspace=tmp_path, audit_path=tmp_path / "a.jsonl", final_text="",
    )
    assert not ok
    assert "unknown verify kind" in detail


def test_verify_broken_spec_fails_closed(run_mod, tmp_path):
    # Missing required 'code' key → KeyError caught, fails closed.
    ok, detail = run_mod.run_verify(
        {"kind": "python"},
        workspace=tmp_path, audit_path=tmp_path / "a.jsonl", final_text="",
    )
    assert not ok
    assert "error" in detail.lower()

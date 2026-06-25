"""Tests for forge.kernel — subprocess executor."""
from __future__ import annotations

import pytest

from forge.kernel import Kernel


@pytest.fixture
def kernel(tmp_path):
    k = Kernel(workspace=tmp_path)
    k.start()
    yield k
    k.stop()


def test_basic_print(kernel):
    obs = kernel.execute('print("hello")')
    assert obs.ok
    assert "hello" in obs.stdout


def test_state_persists_across_cells(kernel):
    kernel.execute("x = 42")
    obs = kernel.execute("print(x)")
    assert "42" in obs.stdout


def test_last_expr_repl_capture(kernel):
    obs = kernel.execute("1 + 1")
    assert obs.result == "2"


def test_syntax_error_returns_not_ok(kernel):
    obs = kernel.execute("def x(:\n  pass")
    assert not obs.ok


def test_runtime_error_returns_not_ok(kernel):
    obs = kernel.execute('raise ValueError("oops")')
    assert not obs.ok
    assert "ValueError" in obs.stderr


def test_tool_globals_in_scope(kernel, tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("abc")
    obs = kernel.execute(f'print(Read("{p}"))')
    assert obs.ok
    assert "abc" in obs.stdout


def test_protected_path_enforced_in_kernel(kernel):
    obs = kernel.execute('Write("~/.ssh/test_attempt", "x")')
    assert not obs.ok
    assert "Protected" in obs.stderr or "protected" in obs.stderr


def test_reset_clears_globals(kernel):
    kernel.execute("x = 100")
    kernel.reset()
    obs = kernel.execute("print(x)")
    assert not obs.ok  # NameError


def test_health_tracks_consecutive_errors(kernel):
    for _ in range(3):
        kernel.execute('raise RuntimeError("x")')
    assert kernel.health.consecutive_errors >= 3
    kernel.execute("print('ok')")
    assert kernel.health.consecutive_errors == 0


def test_kernel_survives_many_cells(kernel):
    for i in range(20):
        obs = kernel.execute(f"print({i})")
        assert obs.ok
    assert kernel.health.cells_executed == 20

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


# ---- D1: resource limits (setrlimit preexec) --------------------------------


def test_rlimit_preexec_builder_returns_callable_or_none():
    """On POSIX with a rlimit plan, _make_rlimit_preexec returns a callable;
    on a platform without `resource` it returns None. Either is valid."""
    from forge.kernel import _make_rlimit_preexec
    fn = _make_rlimit_preexec()
    assert fn is None or callable(fn)


def test_rlimit_preexec_none_when_all_limits_disabled(monkeypatch):
    """Setting every limit to 0 disables rlimits → builder returns None."""
    import importlib

    import forge.config as config
    for var, val in (
        ("FORGE_MAX_ADDRESS_SPACE_BYTES", "0"),
        ("FORGE_MAX_PROCESSES", "0"),
        ("FORGE_MAX_FILE_SIZE_BYTES", "0"),
        ("FORGE_MAX_CPU_SECONDS", "0"),
    ):
        monkeypatch.setenv(var, val)
    importlib.reload(config)
    try:
        from forge.kernel import _make_rlimit_preexec
        # On POSIX this should be None (empty plan); on Windows also None.
        assert _make_rlimit_preexec() is None
    finally:
        # Revert env, then reload so later tests see defaults again.
        for var in ("FORGE_MAX_ADDRESS_SPACE_BYTES", "FORGE_MAX_PROCESSES",
                    "FORGE_MAX_FILE_SIZE_BYTES", "FORGE_MAX_CPU_SECONDS"):
            monkeypatch.delenv(var, raising=False)
        importlib.reload(config)


@pytest.mark.skipif(
    __import__("sys").platform == "win32",
    reason="rlimits are POSIX-only",
)
def test_rlimit_is_applied_in_worker(tmp_path, monkeypatch):
    """The preexec_fn actually sets the configured soft limits in the worker.

    We assert on what forge CONTROLS — that setrlimit ran in the child — not
    on kernel enforcement, which varies by platform (notably, Darwin does not
    enforce RLIMIT_AS even when it is set; Linux does). The cell reads its own
    rlimits back via the stdlib `resource` module.

    We deliberately test CPU + FSIZE, NOT NPROC: RLIMIT_NPROC is per-uid and
    capping it can stop the worker itself from forking on a busy machine (the
    exact footgun that broke pdftotext), so it's disabled by default and not
    exercised here.
    """
    import importlib

    import forge.config as config
    monkeypatch.setenv("FORGE_MAX_CPU_SECONDS", "222")
    monkeypatch.setenv("FORGE_MAX_FILE_SIZE_BYTES", str(123 * 1024 * 1024))
    importlib.reload(config)
    try:
        from forge.kernel import Kernel
        k = Kernel(workspace=tmp_path, sandboxed=False)
        k.start()
        try:
            obs = k.execute(
                "import resource\n"
                "print('CPU', resource.getrlimit(resource.RLIMIT_CPU)[0])\n"
                "print('FSIZE', resource.getrlimit(resource.RLIMIT_FSIZE)[0])\n"
            )
            assert obs.ok, obs.stderr
            assert "CPU 222" in obs.stdout
            assert f"FSIZE {123 * 1024 * 1024}" in obs.stdout
        finally:
            k.stop()
    finally:
        monkeypatch.delenv("FORGE_MAX_CPU_SECONDS", raising=False)
        monkeypatch.delenv("FORGE_MAX_FILE_SIZE_BYTES", raising=False)
        importlib.reload(config)

"""forge.kernel — execute agent-emitted Python in a managed subprocess.

For v0.1 this is a deliberately simple kernel: a Python subprocess that takes
JSON commands over stdin, executes cells with exec() in a long-lived global
namespace, and writes ONE JSON result line per cell to a DEDICATED FD (not
stdout). User-code stdout/stderr go to the regular pipes.

Why a dedicated fd for results: it makes marker-collision attacks impossible
(see docs/SHAKE-OUT.md finding #1). User code physically cannot write to fd 3
without explicit os.write(3, ...) — at which point the cell would still be
running and the marker would be corrupt JSON, which we treat as a failure.

Hardening rolled in:
  - Dedicated result fd (3) via Popen pass_fds
  - Per-call nonce embedded in result records — verifier rejects mismatches
  - threading.Lock around stdin-write/result-read in execute()
  - Drainer thread bound to its specific Popen+pipe via closure
  - Stop() joins drainer with a timeout
  - SIGTERM → wait(2) → kill on timeout, then proc=None for clean restart
  - Stderr buffer is a deque(maxlen=1000) — no list-resize race

When we need cell magics, autocomplete, or rich display, jupyter_client is
the v0.2 swap. The Kernel interface here is the contract; the implementation
is replaceable.
"""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_RESULT_FD = 3  # dedicated fd in the worker for our result lines
_RESULT_PREFIX = "\x1eFORGE_RESULT\x1e"  # still useful as a sanity tag
# Optional fallback: if the worker can't open fd 3, it falls back to stdout
# with the prefix — preserves backward compatibility for environments where
# pass_fds isn't supported.

_WORKER_SCRIPT = r'''
"""Forge kernel worker.

Reads JSON commands from stdin, executes cells, writes ONE JSON result line
per cell to fd 3 (or, if fd 3 isn't open, falls back to stdout with the
PREFIX marker). User-code stdout/stderr go to the normal stdio pipes.

Each command can include a "nonce" field; if present, we echo it back in
the result so the parent can match request to response.
"""
import sys, json, traceback, io, os, contextlib

# Install the builtins/subprocess guards before any cell code can patch them.
sys.path.insert(0, os.environ.get("FORGE_SRC_PATH", ""))
from forge.tools import install_builtin_guards, kernel_globals
install_builtin_guards()

GLOBALS = {"__name__": "__forge_kernel__", "__builtins__": __builtins__}
GLOBALS.update(kernel_globals())

PREFIX = "\x1eFORGE_RESULT\x1e"

# Open a writer on the fd the parent told us to use (FORGE_RESULT_FD).
# Falls back to stdout with the PREFIX marker if not set or if open fails.
_fd_str = os.environ.get("FORGE_RESULT_FD")
if _fd_str:
    try:
        RESULT_OUT = os.fdopen(int(_fd_str), "w", buffering=1, closefd=True)
        USE_FD = True
    except (OSError, ValueError):
        RESULT_OUT = sys.stdout
        USE_FD = False
else:
    RESULT_OUT = sys.stdout
    USE_FD = False


def _emit_result(payload):
    """Write the result envelope to fd 3 (or fall back to stdout w/ PREFIX)."""
    if USE_FD:
        line = json.dumps(payload) + "\n"
    else:
        line = PREFIX + json.dumps(payload) + "\n"
    RESULT_OUT.write(line)
    RESULT_OUT.flush()


def main():
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError as e:
            _emit_result({"ok": False, "error": "bad command: " + str(e)})
            continue

        if cmd.get("op") == "exit":
            return

        nonce = cmd.get("nonce")

        if cmd.get("op") == "reset":
            GLOBALS.clear()
            GLOBALS.update({"__name__": "__forge_kernel__", "__builtins__": __builtins__})
            GLOBALS.update(kernel_globals())
            # Reinstall guards (they live in forge.tools module state — they
            # survive globals.clear, but a malicious cell could have monkey-
            # patched the underlying primitives. Re-install is idempotent.)
            try:
                from forge.tools import install_builtin_guards as _reinstall
                _reinstall()
            except Exception:
                pass
            _emit_result({"ok": True, "stdout": "", "stderr": "", "result": "kernel reset", "nonce": nonce})
            continue

        if cmd.get("op") != "exec":
            _emit_result({"ok": False, "error": "unknown op: " + repr(cmd.get("op")), "nonce": nonce})
            continue

        code = cmd.get("code", "")
        cwd = cmd.get("cwd")
        if cwd:
            try:
                os.chdir(cwd)
            except OSError as e:
                _emit_result({"ok": False, "error": "cd " + cwd + ": " + str(e), "nonce": nonce})
                continue

        out_buf, err_buf = io.StringIO(), io.StringIO()
        ok = True
        err_text = ""
        last_value_repr = ""
        try:
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                import ast as _ast
                tree = _ast.parse(code, mode="exec")
                if tree.body and isinstance(tree.body[-1], _ast.Expr):
                    last_expr = tree.body.pop()
                    exec(compile(_ast.Module(body=tree.body, type_ignores=[]), "<cell>", "exec"), GLOBALS)
                    value = eval(compile(_ast.Expression(body=last_expr.value), "<cell>", "eval"), GLOBALS)
                    if value is not None:
                        last_value_repr = repr(value)
                else:
                    exec(compile(tree, "<cell>", "exec"), GLOBALS)
        except SystemExit:
            ok = False
            err_text = "SystemExit raised - kernel will not exit; cell rejected"
        except KeyboardInterrupt:
            ok = False
            err_text = "KeyboardInterrupt"
            raise
        except BaseException:
            ok = False
            err_text = traceback.format_exc()

        # Truncate massive stdout to bound parent memory.
        STDOUT_CAP = 1_000_000  # 1 MB per cell
        stdout = out_buf.getvalue()
        if len(stdout) > STDOUT_CAP:
            stdout = stdout[:STDOUT_CAP] + f"\n... [stdout truncated to {STDOUT_CAP} chars]"

        _emit_result({
            "ok": ok,
            "stdout": stdout,
            "stderr": err_buf.getvalue() + (err_text if err_text else ""),
            "result": last_value_repr,
            "nonce": nonce,
        })


main()
'''


@dataclass
class Observation:
    """The result of executing one cell."""

    ok: bool
    stdout: str
    stderr: str
    result: str = ""        # repr of last expression, REPL-style
    elapsed_s: float = 0.0
    cell_code: str = ""

    def format(self, *, max_chars: int = 4000) -> str:
        """Render for the model's next-turn observation."""
        parts: list[str] = []
        if self.stdout:
            parts.append(self.stdout.rstrip())
        if self.result:
            parts.append(f"=> {self.result}")
        if self.stderr:
            parts.append(f"--- stderr ---\n{self.stderr.rstrip()}")
        if not parts:
            parts.append("(no output)")
        text = "\n".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... [truncated, {len(text) - max_chars} more chars]"
        return text


@dataclass
class KernelHealth:
    """Tracks consecutive errors / time-since-success for state-rot detection."""

    consecutive_errors: int = 0
    cells_executed: int = 0
    last_success_at: float = field(default_factory=time.monotonic)

    def note_result(self, ok: bool) -> None:
        self.cells_executed += 1
        if ok:
            self.consecutive_errors = 0
            self.last_success_at = time.monotonic()
        else:
            self.consecutive_errors += 1

    def is_wedged(self) -> bool:
        return (
            self.consecutive_errors >= 4
            or (time.monotonic() - self.last_success_at) > 300
        )


class Kernel:
    """A managed Python subprocess that executes agent-emitted cells.

    Threadsafe: concurrent execute() calls serialize on a lock so they
    don't cross-talk. Drainer threads are bound to their specific Popen
    so a stop()→start() cycle doesn't leak readers.

    On macOS, the worker subprocess can be wrapped in a sandbox-exec
    profile (forge.sandbox) that limits FS writes + network. Pass
    `sandboxed=True` (default) to enable; the wrapping is automatic and
    silently no-ops on platforms that don't support it.
    """

    def __init__(self, *, workspace: Path, sandboxed: bool = True):
        self.workspace = workspace.resolve()
        self.sandboxed = sandboxed
        self.proc: subprocess.Popen[str] | None = None
        self.health = KernelHealth()
        self._stderr_buf: deque[str] = deque(maxlen=1000)
        self._stderr_thread: threading.Thread | None = None
        self._result_pipe_r: int | None = None
        self._result_reader: Any | None = None
        self._exec_lock = threading.Lock()
        self._next_nonce = 0
        self._sandbox_profile_path: Path | None = None

    def _new_nonce(self) -> str:
        self._next_nonce += 1
        return f"n{self._next_nonce}"

    def start(self) -> None:
        """Spawn the worker subprocess with a dedicated result fd."""
        if self.proc is not None and self.proc.poll() is None:
            return

        # Create a pipe for result lines; pass write-end to the child.
        # Note: pass_fds keeps the fd number THE SAME in the child, so we
        # tell the child which fd via env var rather than hardcoding 3.
        result_r, result_w = os.pipe()
        os.set_inheritable(result_w, True)

        # SECURITY: build a MINIMAL env for the kernel worker rather than
        # inheriting the parent's. This prevents provider secrets (API keys,
        # GitHub tokens) from being readable by agent-emitted code via
        # os.environ. The worker still needs FORGE_SRC_PATH (to import
        # forge.tools) + FORGE_RESULT_FD (the new pipe).
        from forge._subprocess_env import build_minimal_env
        forge_src = str(Path(__file__).resolve().parent.parent)
        env = build_minimal_env(extra={
            "FORGE_SRC_PATH": forge_src,
            "FORGE_RESULT_FD": str(result_w),
            "PYTHONUNBUFFERED": "1",
        })

        # Build the command. If sandboxing is on AND supported, wrap with
        # sandbox-exec. Profile is bound to the current workspace.
        base_cmd = [sys.executable, "-u", "-c", _WORKER_SCRIPT]
        if self.sandboxed:
            from forge.sandbox import wrap_command
            cmd, profile_path = wrap_command(
                base_cmd,
                workspace=self.workspace,
                # Default allowlist: ollama localhost. Skills can extend via
                # MCP server config or a future router.toml network section.
                allowed_network_hosts=["localhost", "127.0.0.1"],
            )
            self._sandbox_profile_path = profile_path
        else:
            cmd = base_cmd
            self._sandbox_profile_path = None

        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self.workspace),
                env=env,
                bufsize=1,
                pass_fds=(result_w,),
            )
        finally:
            # Parent side closes the write end; only child holds it.
            os.close(result_w)

        self.proc = proc
        self._result_pipe_r = result_r
        self._result_reader = os.fdopen(result_r, "r", buffering=1)

        # Bind drainer to THIS specific Popen+stderr handle via closure.
        # If a future stop() spawns a new proc, it'll spawn its own drainer.
        proc_ref = proc
        stderr_pipe = proc.stderr
        buf = self._stderr_buf

        def drain() -> None:
            if stderr_pipe is None:
                return
            try:
                for line in stderr_pipe:
                    buf.append(line)
                    if proc_ref.poll() is not None:
                        break
            except (ValueError, OSError):
                pass  # pipe closed during shutdown

        t = threading.Thread(target=drain, daemon=True, name="forge-kernel-drainer")
        t.start()
        self._stderr_thread = t

    def stop(self) -> None:
        """Terminate the worker. SIGTERM → wait → kill if needed."""
        if self.proc is None:
            return
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.write(json.dumps({"op": "exit"}) + "\n")
                self.proc.stdin.flush()
        except (BrokenPipeError, AttributeError, OSError):
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass

        # Close the result reader; drainer will exit on its own when stderr closes.
        if self._result_reader is not None:
            try:
                self._result_reader.close()
            except OSError:
                pass
            self._result_reader = None
        self._result_pipe_r = None

        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
            self._stderr_thread = None

        # Clean up the sandbox-exec profile we wrote to /tmp
        if self._sandbox_profile_path is not None:
            try:
                self._sandbox_profile_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._sandbox_profile_path = None

        self.proc = None

    def __enter__(self) -> Kernel:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def execute(self, code: str, *, timeout: float = 120.0) -> Observation:
        """Run a cell. Threadsafe: serializes concurrent calls on a lock."""
        with self._exec_lock:
            return self._execute_locked(code, timeout=timeout)

    def _execute_locked(self, code: str, *, timeout: float) -> Observation:
        if self.proc is None or self.proc.poll() is not None:
            self.start()
        assert self.proc is not None and self.proc.stdin is not None
        assert self._result_reader is not None

        nonce = self._new_nonce()
        cmd = json.dumps({
            "op": "exec",
            "code": code,
            "cwd": str(self.workspace),
            "nonce": nonce,
        })
        t0 = time.monotonic()

        try:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError:
            return Observation(
                ok=False, stdout="",
                stderr="kernel pipe closed; will restart on next call",
                cell_code=code, elapsed_s=time.monotonic() - t0,
            )

        # Read result line from fd 3 with a manual timeout via select.
        # Worker writes one line per cell; we accept the first valid JSON line
        # whose nonce matches.
        result: dict | None = None
        deadline = t0 + timeout
        result_fd = self._result_reader

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                rlist, _, _ = select.select([result_fd], [], [], min(remaining, 1.0))
            except (ValueError, OSError):
                break
            if not rlist:
                continue
            line = result_fd.readline()
            if not line:
                break
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Verify nonce: dropping mismatched results prevents stale data
            # from a previous (timed-out) cell from contaminating this one.
            if payload.get("nonce") and payload.get("nonce") != nonce:
                continue
            result = payload
            break

        elapsed = time.monotonic() - t0

        if result is None:
            # Timeout. Kill cleanly so the next cell starts on a fresh worker.
            self._kill_and_clear()
            obs = Observation(
                ok=False, stdout="",
                stderr=f"cell timed out after {elapsed:.1f}s; "
                       f"kernel restarted. recent stderr:\n"
                       f"{''.join(list(self._stderr_buf)[-20:])}",
                cell_code=code, elapsed_s=elapsed,
            )
            self.health.note_result(False)
            return obs

        obs = Observation(
            ok=bool(result.get("ok")),
            stdout=str(result.get("stdout", "")),
            stderr=str(result.get("stderr", "")),
            result=str(result.get("result", "")),
            cell_code=code,
            elapsed_s=elapsed,
        )
        self.health.note_result(obs.ok)
        return obs

    def _kill_and_clear(self) -> None:
        """Used after a timeout: SIGTERM → wait → kill, then clear so next
        execute() spawns a fresh worker."""
        if self.proc is None:
            return
        try:
            self.proc.send_signal(signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                self.proc.kill()
                self.proc.wait(timeout=1)
            except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
                pass
        if self._result_reader is not None:
            try:
                self._result_reader.close()
            except OSError:
                pass
            self._result_reader = None
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)
            self._stderr_thread = None
        self.proc = None

    def reset(self) -> Observation:
        """Clear the kernel's globals back to startup state."""
        with self._exec_lock:
            if self.proc is None:
                self.start()
            assert self.proc is not None and self.proc.stdin is not None
            assert self._result_reader is not None

            nonce = self._new_nonce()
            self.proc.stdin.write(json.dumps({"op": "reset", "nonce": nonce}) + "\n")
            self.proc.stdin.flush()

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                rlist, _, _ = select.select([self._result_reader], [], [], 0.5)
                if not rlist:
                    continue
                line = self._result_reader.readline()
                if not line:
                    break
                try:
                    payload = json.loads(line)
                    if payload.get("nonce") == nonce:
                        break
                except json.JSONDecodeError:
                    continue
            self.health = KernelHealth()
            return Observation(
                ok=True, stdout="", stderr="", result="kernel reset", cell_code="",
            )

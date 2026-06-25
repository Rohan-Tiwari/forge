"""forge.mcp — Model Context Protocol stdio-server client.

Implements the MCP protocol over stdio (the most common transport for
local tooling). The client:

  * Spawns a server subprocess (e.g. `npx -y @modelcontextprotocol/server-github`)
  * Speaks JSON-RPC 2.0 over stdin/stdout
  * Caches the tool list returned by the initial handshake
  * Calls tools by name with structured args, returns the structured result
  * Cleanly tears down the subprocess on Session.close

Servers are configured in `~/.forge/mcp.toml`:

    [servers.gh]
    cmd = "npx"
    args = ["-y", "@modelcontextprotocol/server-github"]
    env = { GITHUB_PERSONAL_ACCESS_TOKEN = "..." }

    [servers.fs]
    cmd = "npx"
    args = ["-y", "@modelcontextprotocol/server-filesystem", "~/Documents"]

The agent reaches MCP via the `call_mcp(server, tool, **args)` global in
the kernel — same shape as `run_skill`, different backend.

This implementation talks the MCP 2024-11-05 protocol revision. We DON'T
depend on a third-party MCP SDK because (a) the official SDK changed shape
several times in 2025 and (b) the wire format is small enough that a
~200-LOC client is more reliable than a moving SDK target.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_MCP_CONFIG_PATH = Path(
    os.environ.get("FORGE_MCP_CONFIG",
                   str(Path.home() / ".forge" / "mcp.toml"))
).expanduser()

# Match the protocol version we support. Servers may advertise different
# versions; we negotiate down to whatever they accept.
_PROTOCOL_VERSION = "2024-11-05"


# =============================================================================
# Errors
# =============================================================================


class MCPError(RuntimeError):
    """Base class for MCP client errors."""


class MCPServerNotConfigured(MCPError):
    """Tried to call_mcp(server, ...) but no such server in config."""


class MCPCallError(MCPError):
    """Server returned a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"MCP {code}: {message}")
        self.code = code
        self.data = data


# =============================================================================
# Config
# =============================================================================


@dataclass
class MCPServerConfig:
    name: str
    cmd: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None


def load_config(path: Path | None = None) -> dict[str, MCPServerConfig]:
    """Load ~/.forge/mcp.toml. Returns empty dict if absent."""
    p = path or _MCP_CONFIG_PATH
    if not p.exists():
        return {}
    try:
        with p.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise MCPError(f"failed to parse {p}: {e}") from e

    servers: dict[str, MCPServerConfig] = {}
    for name, entry in (data.get("servers") or {}).items():
        if not isinstance(entry, dict):
            continue
        cmd = entry.get("cmd")
        if not cmd:
            continue
        servers[name] = MCPServerConfig(
            name=name,
            cmd=str(cmd),
            args=[str(a) for a in entry.get("args") or []],
            env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
            cwd=entry.get("cwd"),
        )
    return servers


# =============================================================================
# Wire protocol — JSON-RPC 2.0 over stdio
# =============================================================================


@dataclass
class MCPTool:
    """One tool advertised by a server."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


class MCPSession:
    """One running stdio server. Lives for the duration of a Forge Session.

    Threadsafe: a lock serializes concurrent call_tool() invocations so
    JSON-RPC ids don't collide.
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.proc: subprocess.Popen[str] | None = None
        self.tools: dict[str, MCPTool] = {}
        self._lock = threading.Lock()
        self._next_id = 0
        self._stderr_buf: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._initialized = False

    # ---- lifecycle ------------------------------------------------------

    def start(self, *, timeout: float = 30.0) -> None:
        """Spawn the server, do the initialize handshake, fetch tool list."""
        if self.proc is not None and self.proc.poll() is None:
            return

        # Resolve command — if it's not an absolute path, look it up. Tilde
        # expansion happens here too.
        cmd = os.path.expanduser(self.config.cmd)
        if not os.path.isabs(cmd):
            resolved = shutil.which(cmd)
            if resolved is None:
                raise MCPError(
                    f"MCP server {self.config.name!r}: command "
                    f"{cmd!r} not found in PATH"
                )
            cmd = resolved

        # SECURITY: build a MINIMAL env for the MCP server rather than
        # inheriting the parent's. This prevents provider secrets from leaking
        # to npm-installed servers (which run as `npx` and could have
        # compromised dependencies). The server gets ONLY:
        #   - the default allowlist (PATH, HOME, etc.)
        #   - the explicit env from the server's config block
        # Provider creds (ANTHROPIC_API_KEY, OPENAI_API_KEY, GITHUB_TOKEN
        # unless explicitly listed) stay in the parent.
        from forge._subprocess_env import build_minimal_env
        env = build_minimal_env(extra=dict(self.config.env))

        self.proc = subprocess.Popen(  # noqa: S603
            [cmd, *self.config.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.config.cwd,
            env=env,
            bufsize=1,
        )

        # Drain stderr in a background thread so the pipe never blocks.
        proc_ref = self.proc
        stderr_pipe = self.proc.stderr
        buf = self._stderr_buf

        def drain() -> None:
            if stderr_pipe is None:
                return
            try:
                for line in stderr_pipe:
                    buf.append(line)
                    if len(buf) > 500:
                        del buf[:250]
                    if proc_ref.poll() is not None:
                        break
            except (ValueError, OSError):
                pass

        self._stderr_thread = threading.Thread(
            target=drain, daemon=True,
            name=f"forge-mcp-drain-{self.config.name}",
        )
        self._stderr_thread.start()

        # Initialize handshake.
        self._initialize(timeout=timeout)
        # Pre-fetch tool list so subsequent call_tool() calls don't pay
        # the latency on first use.
        self._refresh_tools(timeout=timeout)
        self._initialized = True

    def stop(self) -> None:
        """Tell the server to stop, kill if it doesn't."""
        if self.proc is None:
            return
        try:
            # MCP doesn't have an explicit shutdown — closing stdin signals exit.
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)
            self._stderr_thread = None

    def __enter__(self) -> MCPSession:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ---- JSON-RPC helpers ------------------------------------------------

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _send(self, method: str, params: dict | None = None,
              *, notification: bool = False) -> int | None:
        """Send a JSON-RPC request or notification. Returns the id (None for notifications)."""
        if self.proc is None or self.proc.stdin is None:
            raise MCPError(f"MCP {self.config.name}: not started")
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notification:
            mid = self._new_id()
            msg["id"] = mid
        else:
            mid = None
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError as e:
            raise MCPError(f"MCP {self.config.name}: stdin pipe closed") from e
        return mid

    def _recv(self, expected_id: int, *, timeout: float) -> dict[str, Any]:
        """Read JSON-RPC responses until we see the matching id.

        Notifications and out-of-order results are discarded for v0; a future
        version will route them properly to support async server-side events.

        SECURITY: bounded line read + broad except.
        - A hostile/buggy server emitting a single huge line without \\n
          would drive the parent OOM via unbounded readline. We cap at 1 MB.
        - Deeply nested JSON (`{"a":{"a":{...}}}` past sys.getrecursionlimit)
          raises RecursionError (a RuntimeError, NOT JSONDecodeError) which
          would escape the loop and unwind into the Session. Catch it.
        See v0.2.1 audit finding #3.
        """
        if self.proc is None or self.proc.stdout is None:
            raise MCPError(f"MCP {self.config.name}: not started")
        MAX_LINE_BYTES = 1_000_000  # 1 MB ceiling per JSON-RPC frame
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline(MAX_LINE_BYTES)
            if not line:
                # Server died.
                err = "".join(self._stderr_buf[-20:])
                raise MCPError(
                    f"MCP {self.config.name}: server closed stdout. "
                    f"recent stderr:\n{err}"
                )
            # If we hit the line cap without seeing \n, the server is
            # producing pathological output — kill the session.
            if len(line) >= MAX_LINE_BYTES and not line.endswith("\n"):
                raise MCPError(
                    f"MCP {self.config.name}: server emitted a >{MAX_LINE_BYTES} "
                    f"byte line without newline — aborting (likely malformed "
                    f"or hostile)."
                )
            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, RecursionError, ValueError):
                # Bad JSON or pathologically nested — log and skip.
                continue
            # Notifications have no id; skip for now.
            if "id" not in msg:
                continue
            if msg["id"] != expected_id:
                continue
            return msg
        raise MCPError(
            f"MCP {self.config.name}: timeout waiting for response to id={expected_id}"
        )

    def _call(self, method: str, params: dict | None = None,
              *, timeout: float = 30.0) -> Any:
        """Send a request and wait for its reply. Returns `result`."""
        with self._lock:
            mid = self._send(method, params)
            assert mid is not None
            reply = self._recv(mid, timeout=timeout)
        if "error" in reply:
            err = reply["error"]
            raise MCPCallError(
                code=int(err.get("code", -1)),
                message=str(err.get("message", "unknown")),
                data=err.get("data"),
            )
        return reply.get("result")

    # ---- protocol handshake + tools -------------------------------------

    def _initialize(self, *, timeout: float) -> None:
        """The MCP initialize handshake.

        We advertise our protocol version and minimal client info; server
        responds with its capabilities. We don't currently use the server's
        capabilities object — we just need a successful handshake before
        calling any other method.
        """
        params = {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {
                "roots": {"listChanged": False},
                "sampling": {},
            },
            "clientInfo": {
                "name": "forge",
                "version": "0.1",
            },
        }
        self._call("initialize", params, timeout=timeout)
        # Per spec, send an initialized notification afterwards.
        self._send("notifications/initialized", notification=True)

    def _refresh_tools(self, *, timeout: float) -> None:
        """Fetch tools/list and cache them by name."""
        result = self._call("tools/list", timeout=timeout)
        self.tools = {}
        for entry in (result or {}).get("tools", []):
            name = entry.get("name")
            if not name:
                continue
            self.tools[name] = MCPTool(
                name=name,
                description=entry.get("description") or "",
                input_schema=entry.get("inputSchema") or {},
            )

    # ---- public API used by call_mcp() ----------------------------------

    def list_tools(self) -> list[MCPTool]:
        """Return the cached tool list."""
        if not self._initialized:
            self.start()
        return list(self.tools.values())

    def call_tool(self, name: str, arguments: dict | None = None,
                  *, timeout: float = 60.0) -> Any:
        """Invoke `name` with `arguments`. Returns the structured result.

        MCP returns content as a list of blocks ({type, text, data, ...}).
        For convenience, if all blocks are text, we concat and return the
        plain string; otherwise we return the raw list so the caller can
        inspect non-text blocks (images, embedded resources, etc.).
        """
        if not self._initialized:
            self.start()
        if name not in self.tools:
            available = ", ".join(sorted(self.tools.keys())) or "(none)"
            raise MCPError(
                f"MCP {self.config.name}: no tool named {name!r}. "
                f"available: {available}"
            )
        result = self._call("tools/call", {
            "name": name,
            "arguments": arguments or {},
        }, timeout=timeout)
        # Per MCP spec, result has {content: [...], isError: bool}
        if not isinstance(result, dict):
            return result
        if result.get("isError"):
            content = result.get("content") or []
            text = "; ".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text")
            raise MCPCallError(code=-1, message=text or "tool reported error")
        content = result.get("content") or []
        if (isinstance(content, list) and content
                and all(isinstance(b, dict) and b.get("type") == "text" for b in content)):
            return "".join(b.get("text", "") for b in content)
        return content


# =============================================================================
# MCPRegistry — what the Session owns; one per session.
# =============================================================================


class MCPRegistry:
    """Lazily-spawned dictionary of MCPSession objects, keyed by server name.

    The Session creates one instance and exposes it via the `call_mcp`
    kernel global. Servers are spawned on first use, killed on
    Session.close.
    """

    def __init__(self, configs: dict[str, MCPServerConfig] | None = None):
        self.configs = configs if configs is not None else load_config()
        self.sessions: dict[str, MCPSession] = {}
        self._lock = threading.Lock()

    def call(self, server: str, tool: str, **arguments: Any) -> Any:
        """The function bound to call_mcp() in kernel scope."""
        with self._lock:
            if server not in self.configs:
                available = ", ".join(sorted(self.configs.keys())) or "(none)"
                raise MCPServerNotConfigured(
                    f"no MCP server named {server!r} configured in "
                    f"~/.forge/mcp.toml. configured: {available}"
                )
            sess = self.sessions.get(server)
            if sess is None:
                sess = MCPSession(self.configs[server])
                self.sessions[server] = sess
        # Outside the lock — calls can run concurrently against different
        # servers; the per-session lock inside MCPSession serializes within
        # one server.
        return sess.call_tool(tool, arguments)

    def list_servers(self) -> list[str]:
        return sorted(self.configs.keys())

    def list_tools(self, server: str) -> list[MCPTool]:
        with self._lock:
            if server not in self.configs:
                raise MCPServerNotConfigured(server)
            sess = self.sessions.get(server)
            if sess is None:
                sess = MCPSession(self.configs[server])
                self.sessions[server] = sess
        return sess.list_tools()

    def close_all(self) -> None:
        """Kill every spawned session. Called from Session.close."""
        with self._lock:
            for sess in self.sessions.values():
                try:
                    sess.stop()
                except Exception:  # noqa: BLE001
                    pass
            self.sessions.clear()

"""Tests for forge.mcp — MCP stdio client.

Uses a tiny in-process Python "server" that speaks JSON-RPC over stdin/stdout
to verify the client's handshake + tools/list + tools/call paths without
depending on any third-party MCP server being installed.
"""
from __future__ import annotations

import sys
import textwrap

import pytest

from forge.mcp import (
    MCPCallError,
    MCPError,
    MCPRegistry,
    MCPServerConfig,
    MCPServerNotConfigured,
    MCPSession,
    load_config,
)

# =============================================================================
# Tiny fake MCP server that speaks JSON-RPC 2.0 over stdio
# =============================================================================
# Stored as a string so we can write it to a tmp file per test.

_FAKE_SERVER_SCRIPT = textwrap.dedent('''
    """Tiny test MCP server. Speaks JSON-RPC over stdio.

    Tools:
      echo(text)          — returns the text back
      add(a, b)           — returns a+b
      always_fails(...)   — returns isError=True
    """
    import json, sys

    TOOLS = [
        {"name": "echo",
         "description": "Echo a text string back.",
         "inputSchema": {"type": "object",
                         "properties": {"text": {"type": "string"}},
                         "required": ["text"]}},
        {"name": "add",
         "description": "Add two integers.",
         "inputSchema": {"type": "object",
                         "properties": {"a": {"type": "integer"},
                                        "b": {"type": "integer"}},
                         "required": ["a", "b"]}},
        {"name": "always_fails",
         "description": "Always reports an error.",
         "inputSchema": {"type": "object", "properties": {}}},
    ]


    def send(payload):
        sys.stdout.write(json.dumps(payload) + "\\n")
        sys.stdout.flush()


    def reply(req_id, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        send(msg)


    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        rid = req.get("id")
        params = req.get("params", {})

        if method == "initialize":
            reply(rid, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "0.1"},
            })
        elif method == "notifications/initialized":
            # Notification — no response.
            pass
        elif method == "tools/list":
            reply(rid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "echo":
                reply(rid, {"content": [{"type": "text",
                                          "text": args.get("text", "")}]})
            elif name == "add":
                total = args.get("a", 0) + args.get("b", 0)
                reply(rid, {"content": [{"type": "text",
                                          "text": str(total)}]})
            elif name == "always_fails":
                reply(rid, {"isError": True,
                            "content": [{"type": "text",
                                          "text": "boom"}]})
            else:
                reply(rid, error={"code": -32601,
                                  "message": f"unknown tool {name}"})
        else:
            reply(rid, error={"code": -32601, "message": "method not found"})
''').lstrip()


@pytest.fixture
def fake_server_path(tmp_path):
    """Write the fake server script to a tmp file and return its path."""
    p = tmp_path / "fake_mcp_server.py"
    p.write_text(_FAKE_SERVER_SCRIPT)
    return p


@pytest.fixture
def fake_session(fake_server_path):
    """An MCPSession pointed at the fake server. Cleaned up after."""
    cfg = MCPServerConfig(
        name="fake",
        cmd=sys.executable,
        args=[str(fake_server_path)],
    )
    sess = MCPSession(cfg)
    yield sess
    sess.stop()


# =============================================================================
# Config loading
# =============================================================================


def test_load_config_missing_file_returns_empty(tmp_path):
    assert load_config(tmp_path / "nope.toml") == {}


def test_load_config_parses_servers(tmp_path):
    p = tmp_path / "mcp.toml"
    p.write_text(textwrap.dedent('''
        [servers.gh]
        cmd = "npx"
        args = ["-y", "@modelcontextprotocol/server-github"]

        [servers.gh.env]
        GITHUB_TOKEN = "abc"
    '''))
    cfgs = load_config(p)
    assert "gh" in cfgs
    assert cfgs["gh"].cmd == "npx"
    assert cfgs["gh"].args == ["-y", "@modelcontextprotocol/server-github"]
    assert cfgs["gh"].env["GITHUB_TOKEN"] == "abc"


def test_load_config_skips_invalid_entries(tmp_path):
    p = tmp_path / "mcp.toml"
    p.write_text(textwrap.dedent('''
        [servers.good]
        cmd = "echo"

        [servers.bad]
        # missing cmd
        args = ["x"]
    '''))
    cfgs = load_config(p)
    assert "good" in cfgs
    assert "bad" not in cfgs


def test_load_config_raises_on_malformed_toml(tmp_path):
    p = tmp_path / "mcp.toml"
    p.write_text("this is = not [valid toml")
    with pytest.raises(MCPError):
        load_config(p)


# =============================================================================
# MCPSession — handshake + tool list + tool call
# =============================================================================


def test_session_handshake_and_tool_list(fake_session):
    fake_session.start(timeout=5)
    tools = fake_session.list_tools()
    names = sorted(t.name for t in tools)
    assert names == ["add", "always_fails", "echo"]
    echo = next(t for t in tools if t.name == "echo")
    assert "Echo" in echo.description


def test_session_call_tool_text_result(fake_session):
    fake_session.start(timeout=5)
    result = fake_session.call_tool("echo", {"text": "hello forge"})
    assert result == "hello forge"


def test_session_call_tool_with_int_args(fake_session):
    fake_session.start(timeout=5)
    result = fake_session.call_tool("add", {"a": 2, "b": 3})
    assert result == "5"


def test_session_call_tool_error_raises(fake_session):
    fake_session.start(timeout=5)
    with pytest.raises(MCPCallError, match="boom"):
        fake_session.call_tool("always_fails")


def test_session_call_unknown_tool_raises(fake_session):
    fake_session.start(timeout=5)
    with pytest.raises(MCPError, match="no tool named"):
        fake_session.call_tool("nonexistent")


def test_session_lazy_starts_on_first_call(fake_session):
    """list_tools / call_tool both start the server if it isn't already."""
    assert fake_session.proc is None
    tools = fake_session.list_tools()
    assert tools  # got something
    assert fake_session.proc is not None


def test_session_clean_shutdown(fake_session):
    fake_session.start(timeout=5)
    fake_session.stop()
    # Subsequent operations restart cleanly
    fake_session.start(timeout=5)
    assert fake_session.call_tool("echo", {"text": "x"}) == "x"


def test_session_command_not_found():
    cfg = MCPServerConfig(name="ghost", cmd="this-command-does-not-exist-anywhere")
    sess = MCPSession(cfg)
    with pytest.raises(MCPError, match="not found in PATH"):
        sess.start(timeout=5)


# =============================================================================
# MCPRegistry
# =============================================================================


def test_registry_call_unknown_server_raises(tmp_path):
    reg = MCPRegistry(configs={})
    with pytest.raises(MCPServerNotConfigured):
        reg.call("unknown", "anything")


def test_registry_dispatches_to_session(fake_server_path):
    reg = MCPRegistry(configs={
        "fake": MCPServerConfig(
            name="fake", cmd=sys.executable, args=[str(fake_server_path)],
        ),
    })
    try:
        result = reg.call("fake", "add", a=10, b=5)
        assert result == "15"
    finally:
        reg.close_all()


def test_registry_caches_session_across_calls(fake_server_path):
    """Two calls to the same server reuse the same MCPSession (no re-spawn)."""
    reg = MCPRegistry(configs={
        "fake": MCPServerConfig(
            name="fake", cmd=sys.executable, args=[str(fake_server_path)],
        ),
    })
    try:
        reg.call("fake", "echo", text="1")
        sess_first = reg.sessions["fake"]
        reg.call("fake", "echo", text="2")
        sess_second = reg.sessions["fake"]
        assert sess_first is sess_second
    finally:
        reg.close_all()


def test_registry_close_all_kills_sessions(fake_server_path):
    reg = MCPRegistry(configs={
        "fake": MCPServerConfig(
            name="fake", cmd=sys.executable, args=[str(fake_server_path)],
        ),
    })
    reg.call("fake", "echo", text="x")
    assert reg.sessions["fake"].proc is not None
    reg.close_all()
    assert "fake" not in reg.sessions


def test_registry_list_servers():
    reg = MCPRegistry(configs={
        "a": MCPServerConfig(name="a", cmd="echo"),
        "b": MCPServerConfig(name="b", cmd="echo"),
    })
    assert reg.list_servers() == ["a", "b"]


def test_registry_list_tools_for_unknown_server():
    reg = MCPRegistry(configs={})
    with pytest.raises(MCPServerNotConfigured):
        reg.list_tools("nope")


# =============================================================================
# Integration with the kernel-globals call_mcp() function
# =============================================================================


def test_call_mcp_unwired_raises_helpful_error():
    """When no Session has wired call_mcp, calling it should give a clear error."""
    # Reset module state to the unwired stub.
    from forge import tools as forge_tools
    original = forge_tools._CALL_MCP
    forge_tools._CALL_MCP = lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError(
            "call_mcp not wired. Configure servers in ~/.forge/mcp.toml; the "
            "Session wires this on start()."
        )
    )
    try:
        with pytest.raises(RuntimeError, match="call_mcp not wired"):
            forge_tools.call_mcp("any", "any")
    finally:
        forge_tools._CALL_MCP = original


def test_call_mcp_routes_through_set_skill_runtime():
    """The Session uses set_skill_runtime(mcp=...) to wire call_mcp."""
    from forge import tools as forge_tools
    fake_calls = []

    def fake_mcp(server, tool, **args):
        fake_calls.append((server, tool, args))
        return "fake-result"

    original = forge_tools._CALL_MCP
    try:
        forge_tools.set_skill_runtime(mcp=fake_mcp)
        result = forge_tools.call_mcp("gh", "list_repos", user="alice")
        assert result == "fake-result"
        assert fake_calls == [("gh", "list_repos", {"user": "alice"})]
    finally:
        forge_tools._CALL_MCP = original

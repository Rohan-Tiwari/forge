"""Tests for streaming completion and the prompt_toolkit REPL."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import pytest

from forge.repl import SLASH_COMMANDS, is_slash_command, make_session
from forge.router import Completion, StreamChunk
from forge.session import Session


# =============================================================================
# Fake router with streaming support — used by streaming tests.
# =============================================================================


@dataclass
class FakeStreamingRouter:
    """Yields scripted responses, optionally as token-by-token streams."""

    scripted: list[str]
    spent_usd: float = 0.0
    cost_ceiling_usd: float = 5.0
    calls: list[Completion] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._iter = iter(self.scripted)
        from forge.router import RoleConfig
        self.roles = {"driver": RoleConfig(primary="fake", num_ctx=16384)}

    def complete(self, messages, *, role="driver", max_tokens=2048):
        try:
            content = next(self._iter)
        except StopIteration:
            content = "(out)"
        c = Completion(
            content=content, role_used=role, model_used="fake",
            elapsed_s=0.01, prompt_tokens=10, completion_tokens=20,
        )
        self.calls.append(c)
        return c

    def complete_stream(self, messages, *, role="driver", max_tokens=2048):
        try:
            content = next(self._iter)
        except StopIteration:
            content = "(out)"
        # Yield in 3-char chunks so we exercise the accumulation logic.
        accumulated: list[str] = []
        for i in range(0, len(content), 3):
            delta = content[i:i + 3]
            accumulated.append(delta)
            yield StreamChunk(
                delta=delta,
                accumulated="".join(accumulated),
                is_final=False,
            )
        comp = Completion(
            content=content, role_used=role, model_used="fake",
            elapsed_s=0.01, prompt_tokens=10, completion_tokens=20,
        )
        self.calls.append(comp)
        yield StreamChunk(
            delta="", accumulated=content, is_final=True, completion=comp,
        )

    def cost_summary(self):
        return {"ceiling_usd": self.cost_ceiling_usd, "spent_usd": 0.0}


# =============================================================================
# Streaming tests
# =============================================================================


@pytest.fixture
def session(tmp_path) -> Iterator[Session]:
    s = Session(workspace=tmp_path, mode="auto")
    yield s
    s.close()


def _patch_router(s: Session, scripted: list[str]) -> FakeStreamingRouter:
    fake = FakeStreamingRouter(scripted=scripted)
    s.router = fake  # type: ignore[assignment]
    s.start()
    return fake


def test_turn_without_on_chunk_uses_buffered_complete(session):
    """No callback → router.complete() called, not complete_stream()."""
    fake = _patch_router(session, ["The answer is 42."])
    result = session.turn("question?")
    assert "42" in result.final_text
    # The buffered completion still works
    assert fake.calls
    assert fake.calls[0].content == "The answer is 42."


def test_turn_with_on_chunk_streams_deltas(session):
    """Callback receives every delta from the streamed response."""
    received: list[str] = []
    _patch_router(session, ["hello world streaming"])
    result = session.turn("?", on_chunk=received.append)
    # The full content arrived in chunks
    assert "".join(received) == "hello world streaming"
    # And the result reflects the same content
    assert "hello world" in result.final_text


def test_turn_with_on_chunk_handles_cell_then_prose(session):
    """Streaming works through a cell run + final prose."""
    cell = (
        '```intent\nintent: x\nwrites: []\nnetwork: []\n```\n'
        '```py\nprint(7)\n```'
    )
    _patch_router(session, [cell, "I ran a cell. Done."])
    deltas: list[str] = []
    result = session.turn("?", on_chunk=deltas.append)
    assert result.cells_run == 1
    assert "Done." in result.final_text
    # Both completions streamed through the callback
    full = "".join(deltas)
    assert "intent: x" in full
    assert "Done." in full


def test_chunk_callback_failure_doesnt_break_loop(session):
    """If the callback raises, the turn still completes successfully."""
    _patch_router(session, ["just prose, no cell"])

    def bad_callback(delta):
        raise RuntimeError("ui crashed")

    result = session.turn("?", on_chunk=bad_callback)
    # Despite the callback raising on every chunk, the turn finished.
    assert "just prose" in result.final_text


# =============================================================================
# REPL tests (no real TTY — just construction + helper logic)
# =============================================================================


def test_make_session_returns_prompt_session(tmp_path):
    """The session is built without crashing and has the multiline + history features."""
    s = make_session(history_path=tmp_path / "hist")
    assert s.multiline is True
    assert s.history is not None


def test_make_session_with_no_history_path(tmp_path):
    """Passing a path is enough; the file gets created lazily on first write."""
    p = tmp_path / "deep" / "nested" / "history"
    s = make_session(history_path=p)
    # The parent dir exists now
    assert p.parent.exists()


def test_make_session_includes_skill_completions(tmp_path):
    """Skill names are added to the completer alongside slash commands."""
    s = make_session(
        history_path=tmp_path / "hist",
        extra_completions=["pdf-extract", "git-tidy"],
    )
    completer = s.completer
    # WordCompleter exposes its words
    assert "pdf-extract" in completer.words
    assert "/exit" in completer.words


def test_slash_commands_constant_covers_expected():
    expected = {"/exit", "/quit", "/undo", "/reset", "/cost", "/help", "/skills"}
    for cmd in expected:
        assert cmd in SLASH_COMMANDS, f"{cmd} not in SLASH_COMMANDS"


def test_is_slash_command():
    assert is_slash_command("/exit")
    assert is_slash_command("  /undo")
    assert is_slash_command("/preview always")
    assert not is_slash_command("hello world")
    assert not is_slash_command("")
    assert not is_slash_command("write me a slash / sandwich")

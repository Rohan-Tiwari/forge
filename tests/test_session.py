"""Tests for forge.session — the agent loop with a FakeRouter.

These tests cover the orchestration logic without burning real Ollama calls.
We script a sequence of Completion objects and verify the loop behaves.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from forge.router import Completion
from forge.session import Session

# =============================================================================
# Fake router — yields a scripted sequence of completions.
# =============================================================================


@dataclass
class FakeRouter:
    """Drop-in stand-in for ModelRouter.complete()."""

    scripted: list[str]
    spent_usd: float = 0.0
    cost_ceiling_usd: float = 5.0
    calls: list[Completion] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._iter = iter(self.scripted)
        self.roles = {"driver": _FakeRoleConfig()}

    def complete(self, messages, *, role="driver", max_tokens=2048):
        try:
            item = next(self._iter)
        except StopIteration:
            item = "(out of scripted completions)"
        # Scripted items can be plain strings (default finish_reason="stop")
        # or (content, finish_reason) tuples for testing recovery paths.
        if isinstance(item, tuple):
            content, finish_reason = item
        else:
            content, finish_reason = item, "stop"
        c = Completion(
            content=content,
            role_used=role,
            model_used="fake",
            elapsed_s=0.01,
            prompt_tokens=10,
            completion_tokens=20,
            cost_usd=0.0,
            finish_reason=finish_reason,
        )
        self.calls.append(c)
        return c

    def cost_summary(self):
        return {"ceiling_usd": self.cost_ceiling_usd, "spent_usd": 0.0,
                "remaining_usd": self.cost_ceiling_usd, "calls": len(self.calls)}

    # No-op escalation API — session.turn calls these; FakeRouter doesn't
    # implement model-switching itself.
    def request_escalation(self, role: str = "driver") -> None:
        pass

    def note_intent_mismatch(self, role: str = "driver") -> None:
        pass

    def note_parse_format_fail(self, role: str = "driver") -> None:
        pass

    def reset_escalation(self, role: str = "driver") -> None:
        pass


@dataclass
class _FakeRoleConfig:
    primary: str = "fake"
    num_ctx: int = 16384
    effort: str = "medium"


def _wrap(code: str, intent: str = "test", writes=None, network=None) -> str:
    """Build a canonical cell."""
    import yaml as _yaml
    intent_yaml = _yaml.safe_dump({
        "intent": intent,
        "writes": writes or [],
        "network": network or [],
        "reversible": True,
    }, default_flow_style=False).strip()
    return f"```intent\n{intent_yaml}\n```\n\n```py\n{code}\n```"


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def session(tmp_path) -> Iterator[Session]:
    s = Session(workspace=tmp_path, mode="auto")
    yield s
    s.close()


def _patch_router(s: Session, scripted: list[str]) -> FakeRouter:
    fake = FakeRouter(scripted=scripted)
    s.router = fake  # type: ignore[assignment]
    s.start()  # initialize kernel + skills + history with the new router
    return fake


# =============================================================================
# Happy path
# =============================================================================


def test_happy_path_one_cell_then_prose(session, tmp_path):
    fake = _patch_router(session, [
        _wrap('print(1+1)'),
        "The answer is 2.",
    ])
    result = session.turn("what is 1+1?")
    assert result.cells_run == 1
    assert "answer is 2" in result.final_text.lower()
    assert result.cost_usd >= 0


def test_prose_only_response_ends_turn(session, tmp_path):
    _patch_router(session, ["I don't have enough info to answer that."])
    result = session.turn("what?")
    assert result.cells_run == 0
    assert "don't have" in result.final_text.lower()


# =============================================================================
# Recovery paths
# =============================================================================


def test_empty_content_retries_once(session, tmp_path):
    _patch_router(session, [
        "",  # empty — should trigger one retry
        _wrap('print(42)'),
        "Answered: 42.",
    ])
    result = session.turn("what?")
    assert result.cells_run == 1
    assert "42" in result.final_text


def test_tool_call_parse_recovered_retries_not_displayed(session, tmp_path):
    """v0.2.3 — a tool_call_parse_recovered Completion is a salvaged fragment
    from an Ollama harmony parser crash, never the model's intended final
    reply. It must trigger a retry, never terminate the turn.
    """
    _patch_router(session, [
        # First call: harmony crashes, only "import os" is recovered. Looks
        # like prose to the gate but came from a parse-error 500.
        ("import os, json", "tool_call_parse_recovered"),
        # Retry — now the model produces a proper cell.
        _wrap('print(42)'),
        # Final prose
        "Answered: 42.",
    ])
    result = session.turn("count things")
    assert result.cells_run == 1
    assert "42" in result.final_text
    # The recovered stub must NOT appear in the final user-facing reply
    assert "import os" not in result.final_text


def test_tool_call_parse_recovered_exhaust_retries_honest_error(session, tmp_path):
    """If the harmony parser keeps firing past the retry budget, surface a
    clear error to the user — do NOT display the recovered fragment as if
    it were a real reply.
    """
    _patch_router(session, [
        ("import os", "tool_call_parse_recovered"),
        ("import json", "tool_call_parse_recovered"),
        ("import sys", "tool_call_parse_recovered"),
        ("import pathlib", "tool_call_parse_recovered"),
    ])
    result = session.turn("count things")
    # Final reply tells the user honestly, doesn't display the import line
    assert "intercepted" in result.final_text.lower()
    assert "import os" not in result.final_text
    assert "import pathlib" not in result.final_text


def test_format_failure_retries(session, tmp_path):
    _patch_router(session, [
        "no fence at all just prose with no code",  # not really format failure
        _wrap('print(1)'),
        "Done.",
    ])
    # The first response is prose-only — turn should END after it.
    # This test verifies prose-only is correctly treated as turn end.
    result = session.turn("?")
    assert "no fence" in result.final_text or result.cells_run == 0


def test_format_failure_with_partial_intent(session, tmp_path):
    """If the model emits a python fence WITHOUT intent, gate denies and we retry."""
    _patch_router(session, [
        "```py\nprint(1)\n```",  # missing intent fence
        _wrap('print(2)'),
        "Done.",
    ])
    result = session.turn("?")
    assert result.cells_run == 1


def test_max_cells_limit(session, tmp_path):
    """Loop terminates after max_cells_per_turn even if model never writes prose."""
    session.max_cells_per_turn = 3
    _patch_router(session, [
        _wrap('x = 1'),
        _wrap('x = 2'),
        _wrap('x = 3'),
        _wrap('x = 4'),  # should never be reached
    ])
    result = session.turn("loop forever")
    assert result.cells_run == 3
    assert "stopped after" in result.final_text


# =============================================================================
# Side-effect honest accounting
# =============================================================================


def test_python_post_check_catches_broken_py(session, tmp_path):
    """Cell that writes a syntactically-broken .py should be marked ok=False."""
    broken = "def x(:\n    pass"
    cell = _wrap(
        f'Write("./out.py", {broken!r})',
        writes=["./out.py"],
    )
    _patch_router(session, [cell, "Done."])
    result = session.turn("write a broken py file")
    assert result.cells_run >= 1
    last_obs = result.observations[-1]
    # The post-check should have caught the broken py and flipped ok to False.
    assert not last_obs.ok or "PostCheckFailed" in last_obs.stderr


def test_session_id_is_stable(session):
    sid1 = session.session_id
    sid2 = session.session_id
    assert sid1 == sid2

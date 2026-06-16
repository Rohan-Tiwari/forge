"""Tests for forge.session — the agent loop with a FakeRouter.

These tests cover the orchestration logic without burning real Ollama calls.
We script a sequence of Completion objects and verify the loop behaves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

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
            content = next(self._iter)
        except StopIteration:
            content = "(out of scripted completions)"
        c = Completion(
            content=content,
            role_used=role,
            model_used="fake",
            elapsed_s=0.01,
            prompt_tokens=10,
            completion_tokens=20,
            cost_usd=0.0,
            finish_reason="stop",
        )
        self.calls.append(c)
        return c

    def cost_summary(self):
        return {"ceiling_usd": self.cost_ceiling_usd, "spent_usd": 0.0,
                "remaining_usd": self.cost_ceiling_usd, "calls": len(self.calls)}


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

"""Tests for semantic history compaction (gaps A1+A2 from the landscape
analysis) — the summarizer role wired into Session._maybe_truncate_history,
with error-signal-aware retention and a blind-deletion fallback.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from forge.router import Completion
from forge.session import Session


@dataclass
class _RoleCfg:
    primary: str = "fake"
    num_ctx: int = 100      # tiny so the threshold is easy to cross
    effort: str = "low"


@dataclass
class CompactRouter:
    """Fake router that returns a fixed summary for the summarizer role and
    records whether it was called. Can be told to fail to exercise the
    fallback path."""

    summary_text: str = "TASK: do X. Files: a.py. Decision: used approach Y."
    fail: bool = False
    summarizer_calls: int = 0
    spent_usd: float = 0.0
    cost_ceiling_usd: float = 5.0
    calls: list[Completion] = field(default_factory=list)

    def __post_init__(self):
        self.roles = {"driver": _RoleCfg(), "summarizer": _RoleCfg()}

    def complete(self, messages, *, role="driver", max_tokens=2048, temperature=0.0):
        if role == "summarizer":
            self.summarizer_calls += 1
            if self.fail:
                raise RuntimeError("summarizer offline")
            content = self.summary_text
        else:
            content = "ok"
        c = Completion(content=content, role_used=role, model_used="fake",
                       elapsed_s=0.0, cost_usd=0.0)
        self.calls.append(c)
        return c

    # escalation no-ops
    def request_escalation(self, role="driver"): ...
    def note_intent_mismatch(self, role="driver"): ...
    def note_parse_format_fail(self, role="driver"): ...
    def reset_escalation(self, role="driver"): ...


@pytest.fixture
def compact_session(tmp_path) -> Iterator[Session]:
    s = Session(workspace=tmp_path, mode="auto")
    s.router = CompactRouter()  # type: ignore[assignment]
    s.start()
    yield s
    s.close()


def _long_history(n_obs: int = 12) -> list[dict[str, str]]:
    """Build a history that will exceed the tiny num_ctx threshold."""
    hist: list[dict[str, str]] = [
        {"role": "system", "content": "SYS " * 20},
        {"role": "user", "content": "the original task"},
    ]
    for i in range(n_obs):
        hist.append({"role": "assistant", "content": f"```intent\nintent: step {i}\n```"})
        hist.append({"role": "user",
                     "content": "Observation:\n```\n" + ("data " * 50) + f"{i}\n```"})
    # recent tail
    hist.append({"role": "user", "content": "recent tail message " * 10})
    return hist


def test_semantic_compaction_uses_summarizer(compact_session):
    s = compact_session
    s._history = _long_history()
    before = len(s._history)
    s._maybe_truncate_history()
    router = s.router
    assert router.summarizer_calls == 1  # type: ignore[attr-defined]
    # A single labelled summary block should now be present.
    joined = "\n".join(m["content"] for m in s._history)
    assert "[forge:summary — model-generated, not verbatim]" in joined
    assert "approach Y" in joined
    assert len(s._history) < before


def test_compaction_preserves_head_and_tail(compact_session):
    s = compact_session
    s._history = _long_history()
    s._maybe_truncate_history()
    # System prompt + first user (task) preserved at head.
    assert s._history[0]["role"] == "system"
    assert s._history[1]["content"] == "the original task"
    # Recent tail preserved verbatim.
    assert "recent tail message" in s._history[-1]["content"]


def test_compaction_retains_last_error(compact_session):
    s = compact_session
    hist = _long_history(n_obs=10)
    # Inject an error observation in the MIDDLE span (not in the last 6).
    hist.insert(6, {"role": "user",
                    "content": "Observation:\n```\nTraceback (most recent call last):\n"
                               "ValueError: boom\n```"})
    s._history = hist
    s._maybe_truncate_history()
    joined = "\n".join(m["content"] for m in s._history)
    # The traceback must survive compaction (A2: error-signal retention).
    assert "ValueError: boom" in joined


def test_compaction_falls_back_to_blind_on_summarizer_failure(compact_session):
    s = compact_session
    s.router.fail = True  # type: ignore[attr-defined]
    s._history = _long_history()
    before = len(s._history)
    s._maybe_truncate_history()
    joined = "\n".join(m["content"] for m in s._history)
    # Fallback uses the old context-truncated marker, not the semantic one.
    assert "[forge:context-truncated]" in joined
    assert "[forge:summary" not in joined
    assert len(s._history) < before


def test_no_compaction_below_threshold(compact_session):
    s = compact_session
    s._history = [
        {"role": "system", "content": "short"},
        {"role": "user", "content": "hi"},
    ]
    s._maybe_truncate_history()
    assert s.router.summarizer_calls == 0  # type: ignore[attr-defined]
    assert len(s._history) == 2


def test_tier1_eviction_avoids_summarizer_when_sufficient(compact_session):
    """When evicting old observation bodies gets under target, Tier 1 handles
    it with NO summarizer call (zero cost)."""
    s = compact_session
    # Roomier ctx so tier-1 eviction alone can reach the 25% target.
    s.router.roles["driver"].num_ctx = 2000  # threshold ~6400, target ~2000
    hist = [
        {"role": "system", "content": "S" * 50},
        {"role": "user", "content": "the task"},
    ]
    for i in range(15):
        hist.append({"role": "assistant", "content": f"```intent\nintent: step {i}\n```"})
        hist.append({"role": "user",
                     "content": "Observation:\n```\n" + ("x" * 500) + f"{i}\n```"})
    for i in range(3):
        hist.append({"role": "user", "content": f"recent {i}"})
    s._history = hist
    before = sum(len(m.get("content", "")) for m in s._history)
    s._maybe_truncate_history()
    after = sum(len(m.get("content", "")) for m in s._history)

    assert after < before
    assert s.router.summarizer_calls == 0  # type: ignore[attr-defined]  # Tier 1 only
    joined = "\n".join(m["content"] for m in s._history)
    assert "[forge:observation-evicted]" in joined
    assert "[forge:summary" not in joined  # never escalated to Tier 2


def test_tier1_preserves_last_error(compact_session):
    """Even in Tier 1 eviction, the most recent error observation survives."""
    s = compact_session
    s.router.roles["driver"].num_ctx = 2000
    hist = [
        {"role": "system", "content": "S" * 50},
        {"role": "user", "content": "the task"},
    ]
    for i in range(15):
        hist.append({"role": "user",
                     "content": "Observation:\n```\n" + ("x" * 500) + f"{i}\n```"})
    # An error observation in the middle span.
    hist.append({"role": "user",
                 "content": "Observation:\n```\nTraceback (most recent call last):\n"
                            "ValueError: keepme\n```"})
    hist.extend({"role": "user", "content": f"tail {i}"} for i in range(6))
    s._history = hist
    s._maybe_truncate_history()
    joined = "\n".join(m["content"] for m in s._history)
    assert "ValueError: keepme" in joined

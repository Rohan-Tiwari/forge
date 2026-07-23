"""Tests for forge.verify — self-consistency verification.

Uses a scriptable fake router so no real model is called.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from forge.verify import (
    Verdict,
    extract_final,
    normalize_answer,
    self_consistency,
)


@dataclass
class _FakeComp:
    content: str


@dataclass
class FakeRouter:
    """Yields scripted completion texts; records the temperature it was asked
    for so we can assert diversity sampling is requested."""

    scripted: list[str]
    fail_after: int = 10_000
    temperatures: list[float] = field(default_factory=list)
    _i: int = 0

    def complete(self, messages, *, role="verifier", temperature=0.0, max_tokens=2048):
        self.temperatures.append(temperature)
        if self._i >= self.fail_after:
            raise RuntimeError("simulated vote failure")
        item = self.scripted[self._i] if self._i < len(self.scripted) else "FINAL: ok"
        self._i += 1
        return _FakeComp(content=item)


# ---- extraction / normalization ---------------------------------------------


def test_extract_final_picks_final_line():
    text = "let me think\nsome reasoning\nFINAL: 42"
    assert extract_final(text) == "42"


def test_extract_final_case_insensitive_and_last_wins():
    text = "final: first\nmore\nFINAL: second"
    assert extract_final(text) == "second"


def test_extract_final_fallback_to_last_nonempty_line():
    text = "no marker here\nlast line\n\n"
    assert extract_final(text) == "last line"


def test_normalize_answer_collapses_and_lowercases():
    assert normalize_answer("  The  Answer.  ") == "the answer"
    assert normalize_answer("`42`") == "42"
    assert normalize_answer('"Yes!"') == "yes"


# ---- self-consistency voting ------------------------------------------------


def test_unanimous_votes_are_confident():
    r = FakeRouter(scripted=["FINAL: 5", "FINAL: 5", "FINAL: 5"])
    v = self_consistency(r, "how many?", k=3)
    assert v.answer == "5"
    assert v.samples == 3
    assert v.agreement == 1.0
    assert v.confident
    assert not v.unverified


def test_majority_wins_but_records_agreement():
    r = FakeRouter(scripted=["FINAL: 5", "FINAL: 5", "FINAL: 6"])
    v = self_consistency(r, "how many?", k=3)
    assert v.answer == "5"
    assert v.samples == 3
    assert abs(v.agreement - 2 / 3) < 1e-9
    assert v.confident   # 0.67 >= 0.5 threshold


def test_split_vote_is_unverified():
    # 3-way tie: top answer only has 1/3 agreement, below the 0.5 threshold.
    r = FakeRouter(scripted=["FINAL: a", "FINAL: b", "FINAL: c"])
    v = self_consistency(r, "which?", k=3)
    assert v.unverified
    assert v.samples == 3
    assert abs(v.agreement - 1 / 3) < 1e-9


def test_diversity_temperature_is_requested():
    r = FakeRouter(scripted=["FINAL: x", "FINAL: x", "FINAL: x"])
    self_consistency(r, "q", k=3, temperature=0.7)
    assert r.temperatures == [0.7, 0.7, 0.7]


def test_failed_votes_are_dropped_not_fatal():
    # Only 1 vote succeeds → not enough signal → unverified, but no raise.
    r = FakeRouter(scripted=["FINAL: 9"], fail_after=1)
    v = self_consistency(r, "q", k=3)
    assert v.samples == 1
    assert v.unverified
    assert v.answer == "9"


def test_all_votes_fail_returns_empty_unverified():
    r = FakeRouter(scripted=[], fail_after=0)
    v = self_consistency(r, "q", k=3)
    assert v.samples == 0
    assert v.answer == ""
    assert v.unverified


def test_threshold_is_configurable():
    r = FakeRouter(scripted=["FINAL: 5", "FINAL: 5", "FINAL: 6"])
    # Require 0.75 agreement → 0.67 majority is now unverified.
    v = self_consistency(r, "q", k=3, threshold=0.75)
    assert v.answer == "5"
    assert v.unverified


def test_verdict_dataclass_defaults():
    v = Verdict(answer="x", agreement=1.0, samples=2, unverified=False)
    assert v.votes == []
    assert v.raw == []
    assert v.confident

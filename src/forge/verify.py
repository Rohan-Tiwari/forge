"""forge.verify — self-consistency verification for agent answers.

The cheapest reliability lever available to a local-first harness. A small
model (gpt-oss:20b) tends to anchor on its first plausible answer; sampling
the SAME question a few times at non-zero temperature and taking the majority
vote catches the cases where that first answer was a coin-flip.

This is deliberately NOT the heavyweight path. Per the compute-optimal finding
(Zhao et al., arXiv:2504.01005) self-consistency beats generative verification
at low compute budgets, and only past ~8x compute does an independent
verifier win. Because local inference is free, k=3 self-consistency is nearly
free for forge — it costs wall-clock, not dollars — so it is the default
verification tier. A spawn()-based independent critic (the "full" tier) is a
separate, later feature that this module does not require.

Design:
  * `self_consistency(router, question, *, k, temperature)` re-asks the
    `verifier` role k times and returns a Verdict with the majority answer,
    an agreement ratio, and an explicit `unverified` flag when the votes do
    not converge. An explicit "unverified" verdict is philosophically the
    same as forge's other honest failure states — we say "we could not
    confirm this" rather than manufacturing false confidence.
  * Verdicts never raise on model error: a failed vote is dropped, and if
    every vote fails the result is `unverified` with zero samples. The caller
    decides what to do with low-confidence results; verify() never blocks.

This module has no dependency on Session or Kernel — it is a pure function of
a ModelRouter, so it is trivially testable with a fake router and reusable
from the eval harness, the REPL, or a future `verify()` kernel callable.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class Verdict:
    """The outcome of a self-consistency check."""

    answer: str                       # the majority (normalized) answer, "" if none
    agreement: float                  # fraction of valid votes that agreed [0.0, 1.0]
    samples: int                      # number of valid (non-errored) votes
    unverified: bool                  # True when confidence is below threshold
    votes: list[str] = field(default_factory=list)   # raw normalized votes
    raw: list[str] = field(default_factory=list)      # raw completion texts

    @property
    def confident(self) -> bool:
        return not self.unverified


# The prompt used per vote. We ask for a terse, self-contained final answer so
# the votes normalize and compare cleanly.
_VOTE_SYSTEM = (
    "You are an independent verifier. Answer the question below on its own "
    "merits — do not defer to any prior answer. Think briefly, then end your "
    "reply with a single line of the exact form:\n"
    "FINAL: <your concise answer>\n"
    "The FINAL line must be self-contained and as short as the question "
    "allows (a number, a word, a short phrase, or yes/no)."
)

_FINAL_RE = re.compile(r"^\s*FINAL:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def extract_final(text: str) -> str:
    """Pull the FINAL: line out of a vote. Falls back to the last non-empty
    line if the model didn't follow the format."""
    matches = _FINAL_RE.findall(text or "")
    if matches:
        return matches[-1].strip()
    # Fallback: last non-empty line.
    for line in reversed((text or "").splitlines()):
        s = line.strip()
        if s:
            return s
    return ""


def normalize_answer(ans: str) -> str:
    """Normalize an answer for vote-counting: lowercase, collapse whitespace,
    strip trailing punctuation and surrounding quotes/markdown."""
    s = (ans or "").strip().strip("`*_\"'")
    s = re.sub(r"\s+", " ", s).lower()
    s = s.rstrip(".!,;:")
    return s.strip()


def self_consistency(
    router: object,
    question: str,
    *,
    k: int = 3,
    temperature: float = 0.7,
    threshold: float = 0.5,
    context: str = "",
) -> Verdict:
    """Re-ask `question` k times via the `verifier` role and majority-vote.

    Args:
        router: a ModelRouter (or any object with a compatible
            `complete(messages, *, role, temperature)` method).
        question: the question to verify. For an answer-checking use, phrase
            it as the original user question so votes are independent.
        k: number of samples. k=3 is the default; even numbers can tie.
        temperature: sampling temperature for diversity. 0.0 would make all
            votes identical and defeat the purpose, so callers should keep
            this > 0.
        threshold: minimum agreement fraction for a confident verdict. With
            the default 0.5, a strict majority is required.
        context: optional supporting context appended to the question (e.g.
            observed kernel output). Kept separate so it's clearly not part
            of the question the verifier is answering.

    Returns:
        A Verdict. `unverified` is True when fewer than 2 valid votes came
        back, or when the top answer's agreement is below `threshold`.
    """
    if k < 1:
        k = 1

    user_content = question if not context else f"{question}\n\nContext:\n{context}"
    messages = [
        {"role": "system", "content": _VOTE_SYSTEM},
        {"role": "user", "content": user_content},
    ]

    raw_votes: list[str] = []
    for _ in range(k):
        try:
            comp = router.complete(  # type: ignore[attr-defined]
                messages, role="verifier", temperature=temperature,
            )
        except Exception:  # noqa: BLE001 — a failed vote is dropped, never fatal
            continue
        text = getattr(comp, "content", "") or ""
        if text.strip():
            raw_votes.append(text)

    normalized = [normalize_answer(extract_final(v)) for v in raw_votes]
    normalized = [n for n in normalized if n]

    if len(normalized) < 2:
        # Not enough signal to confirm anything.
        top = normalized[0] if normalized else ""
        return Verdict(
            answer=top,
            agreement=1.0 if normalized else 0.0,
            samples=len(normalized),
            unverified=True,
            votes=normalized,
            raw=raw_votes,
        )

    counts = Counter(normalized)
    top_answer, top_count = counts.most_common(1)[0]
    agreement = top_count / len(normalized)
    unverified = agreement < threshold

    return Verdict(
        answer=top_answer,
        agreement=agreement,
        samples=len(normalized),
        unverified=unverified,
        votes=normalized,
        raw=raw_votes,
    )

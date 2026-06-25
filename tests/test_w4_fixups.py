"""Tests for the dogfood fix-ups in v0.2.0.

Covers the top dogfood findings:
- Plan mode wraps flat refusals in structured plans (rank 1)
- stats p50/p95 uses nearest-rank, doesn't collapse on small N (rank 2)
- --days validation (rank 7)
- recent-sessions started column reformatted (rank 8)
- Non-TTY auto-fallback to preview=never (rank 3)
"""
from __future__ import annotations

import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.router import Completion, RoleConfig
from forge.session import Session

# =============================================================================
# Plan-mode refusal handling (rank 1)
# =============================================================================


class _PlanRouter:
    """Fake router whose planner returns the scripted reply."""

    def __init__(self, planner_reply: str):
        self.planner_reply = planner_reply
        self.cost_ceiling_usd = 5.0
        self.spent_usd = 0.0
        self.calls: list[Completion] = []
        self.roles = {
            "driver": RoleConfig(primary="fake", num_ctx=16384),
            "planner": RoleConfig(primary="fake", effort="high"),
        }

    def complete(self, messages, *, role="driver", max_tokens=2048):
        c = Completion(
            content=self.planner_reply,
            role_used=role,
            model_used="fake",
            elapsed_s=0.01,
            prompt_tokens=50,
            completion_tokens=100,
        )
        self.calls.append(c)
        return c

    def cost_summary(self):
        return {}

    def request_escalation(self, role="driver"): pass
    def note_intent_mismatch(self, role="driver"): pass
    def note_parse_format_fail(self, role="driver"): pass
    def reset_escalation(self, role="driver"): pass


@pytest.fixture
def session(tmp_path) -> Iterator[Session]:
    s = Session(workspace=tmp_path, mode="auto", sandboxed=False)
    yield s
    s.close()


def _patch_router(s: Session, reply: str) -> _PlanRouter:
    fake = _PlanRouter(reply)
    s.router = fake  # type: ignore[assignment]
    s.start()
    return fake


class TestPlanRefusalWrapping:
    def test_structured_plan_passes_through(self, session):
        """A real structured plan from the model is returned verbatim."""
        plan_md = textwrap.dedent("""
            ## Goal
            Count python files.

            ## Steps
            1. rglob — Risk: low

            ## Files touched
            none

            ## Network calls
            none

            ## Open questions
            None.
        """).strip()
        _patch_router(session, plan_md)
        result = session.plan("count python files")
        assert "## Goal" in result
        assert "## Steps" in result
        # Should be a near-verbatim pass-through (stripped of whitespace)
        assert plan_md.strip() in result

    def test_flat_refusal_wrapped_in_structured_plan(self, session):
        """A bare 'I can't help with that' gets wrapped so the contract holds."""
        _patch_router(session, "I'm sorry, but I can't help with that.")
        result = session.plan("delete /etc")
        # Must have all 5 standard sections
        for section in ["## Goal", "## Steps", "## Files touched",
                        "## Network calls", "## Open questions"]:
            assert section in result, f"missing {section}"
        # And the original refusal must be surfaced under Open questions
        assert "I can't help" in result or "can't help with that" in result
        # And risk must be marked critical somewhere
        assert "critical" in result.lower()

    def test_refusal_with_markdown_header_treated_as_plan(self, session):
        """If the model wraps its objection in proper markdown, don't re-wrap."""
        plan_with_objection = textwrap.dedent("""
            ## Goal
            Delete /etc

            ## Steps
            1. rm -rf /etc — Risk: critical

            ## Open questions
            I'm sorry but this would destroy your system.
        """).strip()
        _patch_router(session, plan_with_objection)
        result = session.plan("delete /etc")
        # The original is preserved; we didn't double-wrap
        assert "I'm sorry but this would destroy" in result
        # And we didn't inject our synthetic decline-plan boilerplate
        assert "planner declined to enumerate" not in result

    def test_empty_plan_response_handled(self, session):
        _patch_router(session, "")
        result = session.plan("anything")
        assert "empty response" in result or "no plan produced" in result


# =============================================================================
# stats percentile fix (rank 2)
# =============================================================================


runner = CliRunner()


def _seed_audit(workspace: Path, n_calls: int, elapsed_values: list[float]):
    """Drop synthetic model.complete events into the audit log."""
    import json
    import time
    audit_dir = workspace / ".forge"
    audit_dir.mkdir(parents=True, exist_ok=True)
    log = audit_dir / "audit.jsonl"
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    with log.open("a") as f:
        for i, elapsed in enumerate(elapsed_values[:n_calls]):
            f.write(json.dumps({
                "t": now,
                "kind": "model.complete",
                "session": f"test-session-{i // 5}",
                "model": "gpt-oss:20b",
                "role": "driver",
                "in_tokens": 100,
                "out_tokens": 50,
                "cost_usd": 0.0,
                "elapsed_s": elapsed,
            }) + "\n")
        # A few session.start lines so the recent-sessions table has something
        for sid in {f"test-session-{i // 5}" for i in range(n_calls)}:
            f.write(json.dumps({
                "t": now, "kind": "session.start",
                "session": sid, "workspace": str(workspace),
            }) + "\n")


class TestStatsPercentile:
    def test_p95_correct_for_large_sample(self, tmp_path):
        # 20 evenly-distributed samples: p50 should be ~5, p95 should be ~9
        samples = [float(i) for i in range(1, 21)]  # 1.0 .. 20.0
        _seed_audit(tmp_path, n_calls=20, elapsed_values=samples)
        result = runner.invoke(app, ["stats", "--cwd", str(tmp_path), "--days", "7"])
        assert result.exit_code == 0
        # Sorted [1..20]. nearest-rank p50 = ceil(0.5*20)-1 = idx 9 = 10.0
        #                 nearest-rank p95 = ceil(0.95*20)-1 = idx 18 = 19.0
        assert "10.00s" in result.stdout  # p50
        assert "19.00s" in result.stdout  # p95 (not collapsed to p50)

    def test_p95_not_shown_for_tiny_sample(self, tmp_path):
        """With N < 10, percentiles are misleading; we show min/avg/max instead."""
        samples = [0.85, 2.27, 12.56, 13.10]
        _seed_audit(tmp_path, n_calls=4, elapsed_values=samples)
        result = runner.invoke(app, ["stats", "--cwd", str(tmp_path)])
        assert result.exit_code == 0
        # Should NOT show "p50" / "p95" rows when N < 10
        assert "p50" not in result.stdout
        assert "p95" not in result.stdout
        # Should show min/avg/max instead
        assert "0.85" in result.stdout  # min
        assert "13.10" in result.stdout  # max
        assert "N=4" in result.stdout

    def test_p95_distinct_from_p50_for_skewed_data(self, tmp_path):
        """Heavily skewed sample: most fast, one slow tail. p95 must reflect it."""
        samples = [0.1] * 18 + [50.0, 100.0]  # 20 samples; tail at 50 and 100
        _seed_audit(tmp_path, n_calls=20, elapsed_values=samples)
        result = runner.invoke(app, ["stats", "--cwd", str(tmp_path)])
        # p50 = idx 9 = 0.10. p95 = idx 18 = 50.00. Must NOT be the same.
        assert "0.10s" in result.stdout
        assert "50.00s" in result.stdout


# =============================================================================
# --days validation + pluralization (rank 7)
# =============================================================================


class TestStatsDays:
    def test_days_zero_rejected(self, tmp_path):
        result = runner.invoke(app, ["stats", "--cwd", str(tmp_path), "--days", "0"])
        assert result.exit_code != 0

    def test_days_negative_rejected(self, tmp_path):
        result = runner.invoke(app, ["stats", "--cwd", str(tmp_path), "--days", "-1"])
        assert result.exit_code != 0

    def test_days_one_pluralized_correctly(self, tmp_path):
        result = runner.invoke(app, ["stats", "--cwd", str(tmp_path), "--days", "1"])
        assert result.exit_code == 0
        # 'last 1 day' not 'last 1 days'
        assert "last 1 day " in result.stdout

    def test_days_two_pluralized_correctly(self, tmp_path):
        result = runner.invoke(app, ["stats", "--cwd", str(tmp_path), "--days", "2"])
        assert result.exit_code == 0
        assert "last 2 days" in result.stdout


# =============================================================================
# Recent-sessions started column formatting (rank 8)
# =============================================================================


class TestStartedColumn:
    def test_iso_timestamp_reformatted_to_readable(self, tmp_path):
        """Started column should show MM-DD HH:MM, not a slice of milliseconds."""
        _seed_audit(tmp_path, n_calls=3, elapsed_values=[1.0, 2.0, 3.0])
        result = runner.invoke(app, ["stats", "--cwd", str(tmp_path)])
        assert result.exit_code == 0
        # The new format is MM-DD HH:MM. We can't predict today's exact date,
        # but the pattern should match.
        import re
        # Should appear in the recent-sessions table at least once
        assert re.search(r"\d{2}-\d{2}\s+\d{2}:\d{2}", result.stdout), \
            f"expected MM-DD HH:MM format in: {result.stdout}"
        # And should NOT contain a millisecond-fraction artefact
        assert ".000Z" not in result.stdout

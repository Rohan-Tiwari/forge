"""Tests for forge.providers pricing override + plan mode in Session."""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from forge import providers
from forge.providers import (
    Completion,
    StreamChunk,
    _PRICING_BASELINE,
    _load_pricing_override,
    price,
    reload_pricing,
)
from forge.router import Completion as RouterCompletion
from forge.router import RoleConfig
from forge.session import Session


# =============================================================================
# Pricing override
# =============================================================================


class TestPricingOverride:
    def test_unknown_model_returns_zero(self):
        """Unknown models default to 0 cost — better than crashing."""
        assert price("model-that-does-not-exist", 1_000_000, 1_000_000) == 0.0

    def test_baseline_lookup_works(self):
        """Without override, baseline rates are used."""
        # claude-sonnet-4-6: $3 input, $15 output per 1M
        assert abs(price("claude-sonnet-4-6", 1_000_000, 0) - 3.0) < 1e-9
        assert abs(price("claude-sonnet-4-6", 0, 1_000_000) - 15.0) < 1e-9

    def test_local_models_baseline_zero(self):
        assert price("gpt-oss:20b", 100_000, 100_000) == 0.0
        assert price("qwen2.5vl:7b", 1_000_000, 1_000_000) == 0.0

    def test_load_override_missing_file_returns_empty(self, tmp_path, monkeypatch):
        """No pricing.toml → empty override map."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert _load_pricing_override() == {}

    def test_load_override_parses_toml(self, tmp_path, monkeypatch):
        forge_dir = tmp_path / ".forge"
        forge_dir.mkdir()
        (forge_dir / "pricing.toml").write_text(textwrap.dedent('''
            [pricing."claude-sonnet-4-6"]
            input = 1.5
            output = 7.5

            [pricing."my-custom-model"]
            input = 0.0
            output = 0.0
        '''))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        override = _load_pricing_override()
        assert override["claude-sonnet-4-6"] == (1.5, 7.5)
        assert override["my-custom-model"] == (0.0, 0.0)

    def test_load_override_skips_malformed_entries(self, tmp_path, monkeypatch):
        forge_dir = tmp_path / ".forge"
        forge_dir.mkdir()
        (forge_dir / "pricing.toml").write_text(textwrap.dedent('''
            [pricing."good"]
            input = 1.0
            output = 2.0

            [pricing."bad-not-a-mapping"]
            # Just a comment block at the section level — no input/output
            # fields. Should still parse the model entry but with default 0s
            # rather than crash.
        '''))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        override = _load_pricing_override()
        assert override.get("good") == (1.0, 2.0)
        # Bad entry: input/output absent → defaults to 0,0 (or omitted)
        # Either way we don't crash, and 'good' is intact.

    def test_load_override_handles_corrupt_toml(self, tmp_path, monkeypatch):
        forge_dir = tmp_path / ".forge"
        forge_dir.mkdir()
        (forge_dir / "pricing.toml").write_text("not = valid [toml")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Should not raise — corrupt file falls back to empty override.
        assert _load_pricing_override() == {}

    def test_override_wins_over_baseline(self, tmp_path, monkeypatch):
        """When user sets pricing.toml, those rates take precedence."""
        forge_dir = tmp_path / ".forge"
        forge_dir.mkdir()
        (forge_dir / "pricing.toml").write_text(textwrap.dedent('''
            [pricing."claude-sonnet-4-6"]
            input = 0.5
            output = 2.5
        '''))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        reload_pricing()
        try:
            # Override: $0.5 input, $2.5 output instead of $3 / $15
            assert abs(price("claude-sonnet-4-6", 1_000_000, 0) - 0.5) < 1e-9
            assert abs(price("claude-sonnet-4-6", 0, 1_000_000) - 2.5) < 1e-9
        finally:
            # Restore module state for other tests
            providers._PRICING_OVERRIDE = {}
            providers._PRICING = dict(_PRICING_BASELINE)

    def test_override_can_add_new_model(self, tmp_path, monkeypatch):
        """Override can register pricing for models not in baseline."""
        forge_dir = tmp_path / ".forge"
        forge_dir.mkdir()
        (forge_dir / "pricing.toml").write_text(textwrap.dedent('''
            [pricing."my-self-hosted-llm"]
            input = 0.05
            output = 0.10
        '''))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        reload_pricing()
        try:
            cost = price("my-self-hosted-llm", 1_000_000, 1_000_000)
            # 0.05 + 0.10 = 0.15
            assert abs(cost - 0.15) < 1e-9
        finally:
            providers._PRICING_OVERRIDE = {}
            providers._PRICING = dict(_PRICING_BASELINE)

    def test_reload_returns_combined_view(self, tmp_path, monkeypatch):
        forge_dir = tmp_path / ".forge"
        forge_dir.mkdir()
        (forge_dir / "pricing.toml").write_text(textwrap.dedent('''
            [pricing."x"]
            input = 1.0
            output = 2.0
        '''))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        try:
            combined = reload_pricing()
            # Baseline entries still present
            assert "claude-sonnet-4-6" in combined
            # Override entry present
            assert combined["x"] == (1.0, 2.0)
        finally:
            providers._PRICING_OVERRIDE = {}
            providers._PRICING = dict(_PRICING_BASELINE)


# =============================================================================
# Plan mode
# =============================================================================


class _PlanFakeRouter:
    """Returns a scripted Completion when complete() is called for the planner."""

    def __init__(self, planner_response: str = ""):
        self.planner_response = planner_response
        self.calls: list[Completion] = []
        self.cost_ceiling_usd = 5.0
        self.spent_usd = 0.0
        self.roles = {
            "driver": RoleConfig(primary="fake", num_ctx=16384),
            "planner": RoleConfig(primary="fake-planner", effort="high"),
        }

    def complete(self, messages, *, role="driver", max_tokens=2048):
        c = Completion(
            content=self.planner_response,
            role_used=role,
            model_used="fake-planner" if role == "planner" else "fake",
            elapsed_s=0.01,
            prompt_tokens=50,
            completion_tokens=200,
            cost_usd=0.001,
        )
        self.calls.append(c)
        return c

    def cost_summary(self):
        return {"ceiling_usd": 5.0, "spent_usd": 0.0,
                "remaining_usd": 5.0, "calls": len(self.calls)}

    # No-op escalation API
    def request_escalation(self, role="driver"): pass
    def note_intent_mismatch(self, role="driver"): pass
    def note_parse_format_fail(self, role="driver"): pass
    def reset_escalation(self, role="driver"): pass


@pytest.fixture
def session(tmp_path) -> Iterator[Session]:
    s = Session(workspace=tmp_path, mode="auto", sandboxed=False)
    yield s
    s.close()


def _patch_router(s: Session, response: str) -> _PlanFakeRouter:
    fake = _PlanFakeRouter(planner_response=response)
    s.router = fake  # type: ignore[assignment]
    s.start()
    return fake


class TestPlanMode:
    def test_plan_returns_markdown_string(self, session):
        plan_md = textwrap.dedent("""
            ## Goal
            Count python files.

            ## Steps
            1. Walk the tree — Risk: low
            2. Count .py files — Risk: low

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
        assert "Risk: low" in result

    def test_plan_uses_planner_role(self, session):
        fake = _patch_router(session, "## Goal\nx")
        session.plan("anything")
        assert any(c.role_used == "planner" for c in fake.calls)
        # Driver role should NOT have been invoked
        assert all(c.role_used == "planner" for c in fake.calls)

    def test_plan_doesnt_run_cells(self, session, tmp_path):
        """The whole point of plan mode: no kernel execution.

        Even if the model returns code-fenced output, plan mode just
        returns the markdown — it never calls kernel.execute().
        """
        cell_text = textwrap.dedent("""
            ## Goal
            Write a file.

            ## Steps
            1. Write file — Risk: low

            ```py
            Write("would-have-written.txt", "x")
            ```
        """).strip()
        _patch_router(session, cell_text)
        # Track kernel calls
        original_execute = session.kernel.execute
        execute_calls = []

        def spy_execute(*args, **kwargs):
            execute_calls.append((args, kwargs))
            return original_execute(*args, **kwargs)

        session.kernel.execute = spy_execute  # type: ignore[method-assign]

        session.plan("write a file")
        # Plan mode never invokes kernel
        assert execute_calls == []
        # Workspace is untouched
        assert not (tmp_path / "would-have-written.txt").exists()

    def test_plan_logs_to_audit(self, session, tmp_path):
        _patch_router(session, "## Goal\ntest")
        session.plan("test plan")
        entries = session.audit.tail(20)
        kinds = [e.get("kind") for e in entries]
        assert "plan.start" in kinds
        assert "plan.complete" in kinds

    def test_plan_handles_router_error_gracefully(self, session):
        """If the router raises, plan() returns an error string rather than
        crashing the caller (mirrors turn() behavior)."""
        class BoomRouter(_PlanFakeRouter):
            def complete(self, *a, **kw):
                raise RuntimeError("router exploded")

        s = session
        boom = BoomRouter()
        s.router = boom  # type: ignore[assignment]
        s.start()
        result = s.plan("anything")
        assert "plan-mode call failed" in result
        assert "router exploded" in result

    def test_plan_uses_high_effort_by_default(self, session):
        """The planner role should default to 'high' effort — that's the
        whole point of having it as a separate role."""
        # Default roles, no patching of router
        from forge.router import default_roles
        roles = default_roles()
        assert roles["planner"].effort == "high"

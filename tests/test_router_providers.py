"""Tests for forge.providers + multi-provider routing in forge.router.

Each provider is exercised through a fake stand-in that produces deterministic
output, so these tests don't burn real API calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

import pytest

from forge.providers import (
    AnthropicProvider,
    Completion,
    OllamaProvider,
    OpenAIProvider,
    Provider,
    StreamChunk,
    price,
)
from forge.router import (
    CostCeilingExceeded,
    EscalationState,
    ModelRouter,
    RoleConfig,
)


# =============================================================================
# Fake providers — used to exercise router logic without network
# =============================================================================


@dataclass
class FakeProvider:
    """A scriptable provider that the router can dispatch to."""

    name: str
    handles_prefix: str
    scripted: list[str] = field(default_factory=list)
    fail_until: int = 0  # raise on the first N calls
    call_count: int = 0

    def handles(self, model: str) -> bool:
        return model.startswith(self.handles_prefix)

    def complete(self, *, model, messages, max_tokens, temperature,
                 effort, num_ctx, role) -> Completion:
        self.call_count += 1
        if self.call_count <= self.fail_until:
            raise RuntimeError(f"{self.name}: simulated failure")
        content = self.scripted.pop(0) if self.scripted else "ok"
        return Completion(
            content=content,
            role_used=role,
            model_used=model,
            elapsed_s=0.01,
            prompt_tokens=10,
            completion_tokens=20,
            cost_usd=price(model, 10, 20),
        )

    def complete_stream(self, *, model, messages, max_tokens, temperature,
                        effort, num_ctx, role) -> Iterator[StreamChunk]:
        self.call_count += 1
        if self.call_count <= self.fail_until:
            raise RuntimeError(f"{self.name}: simulated failure")
        content = self.scripted.pop(0) if self.scripted else "ok"
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
            content=content, role_used=role, model_used=model,
            elapsed_s=0.01, prompt_tokens=10, completion_tokens=20,
            cost_usd=price(model, 10, 20),
        )
        yield StreamChunk(
            delta="", accumulated=content, is_final=True, completion=comp,
        )


# =============================================================================
# Provider unit tests — handle() routing
# =============================================================================


def test_ollama_provider_handles_anything():
    p = OllamaProvider.__new__(OllamaProvider)  # skip __init__
    p.base_url = "fake"
    assert p.handles("gpt-oss:20b")
    assert p.handles("custom-local-model")
    assert p.handles("anything")  # catch-all


def test_anthropic_handles_claude_only():
    p = AnthropicProvider.__new__(AnthropicProvider)
    p.api_key = None
    assert p.handles("claude-sonnet-4-6")
    assert p.handles("claude-opus-4-8")
    assert not p.handles("gpt-5")
    assert not p.handles("gpt-oss:20b")


def test_openai_handles_gpt_and_o_models():
    p = OpenAIProvider.__new__(OpenAIProvider)
    p.api_key = None
    assert p.handles("gpt-5")
    assert p.handles("gpt-4o")
    assert p.handles("o3-mini")
    assert p.handles("o4-preview")
    assert not p.handles("claude-sonnet-4-6")
    assert not p.handles("gpt-oss:20b")  # gpt-oss is :-prefixed → Ollama


def test_anthropic_provider_raises_without_api_key():
    p = AnthropicProvider.__new__(AnthropicProvider)
    p.api_key = None
    p._client = None
    import anthropic as _ant
    p._anthropic = _ant
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        p._ensure_client()


def test_openai_provider_raises_without_api_key():
    p = OpenAIProvider.__new__(OpenAIProvider)
    p.api_key = None
    p._client = None
    from openai import OpenAI
    p._OAI = OpenAI
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        p._ensure_client()


# =============================================================================
# Pricing
# =============================================================================


def test_pricing_known_models():
    # Anthropic Sonnet: $3/$15 per 1M
    assert price("claude-sonnet-4-6", 1_000_000, 0) == 3.0
    assert price("claude-sonnet-4-6", 0, 1_000_000) == 15.0


def test_pricing_local_models_zero():
    assert price("gpt-oss:20b", 100_000, 100_000) == 0.0
    assert price("qwen2.5vl:7b", 1_000_000, 1_000_000) == 0.0


def test_pricing_unknown_model_zero():
    """Unknown models default to $0 — better than crashing."""
    assert price("some-mystery-model", 1_000_000, 1_000_000) == 0.0


# =============================================================================
# Router with a custom provider chain
# =============================================================================


def _build_router(*providers, roles=None):
    return ModelRouter(
        providers=list(providers),
        roles=roles or {
            "driver": RoleConfig(
                primary="local-default",
                escalation=["claude-sonnet-4-6"],
            ),
        },
    )


def test_router_picks_first_provider_that_handles():
    ollama = FakeProvider(name="ollama", handles_prefix="local-",
                          scripted=["from ollama"])
    anthropic = FakeProvider(name="anthropic", handles_prefix="claude-",
                             scripted=["from anthropic"])
    router = _build_router(anthropic, ollama)
    comp = router.complete([{"role": "user", "content": "hi"}])
    assert comp.content == "from ollama"
    assert ollama.call_count == 1
    assert anthropic.call_count == 0


def test_router_falls_through_chain_on_provider_error():
    """If the primary's provider raises, router walks the escalation chain."""
    failing = FakeProvider(name="ollama", handles_prefix="local-",
                           scripted=["never"], fail_until=1)
    backup = FakeProvider(name="anthropic", handles_prefix="claude-",
                          scripted=["from backup"])
    router = _build_router(failing, backup)
    comp = router.complete([{"role": "user", "content": "hi"}])
    assert comp.content == "from backup"
    assert failing.call_count == 1
    assert backup.call_count == 1


def test_router_raises_if_all_attempts_fail():
    fail1 = FakeProvider(name="a", handles_prefix="local-", fail_until=99)
    fail2 = FakeProvider(name="b", handles_prefix="claude-", fail_until=99)
    router = _build_router(fail1, fail2)
    with pytest.raises(RuntimeError, match="all model attempts failed"):
        router.complete([{"role": "user", "content": "hi"}])


def test_router_tracks_cost_per_provider():
    ollama = FakeProvider(name="ollama", handles_prefix="local-",
                          scripted=["a", "b", "c"])
    anthropic = FakeProvider(name="anthropic", handles_prefix="claude-",
                             scripted=["x"])
    router = _build_router(ollama, anthropic, roles={
        "driver": RoleConfig(primary="local-x"),
        "extra": RoleConfig(primary="claude-sonnet-4-6"),
    })
    router.complete([{"role": "user", "content": "1"}], role="driver")
    router.complete([{"role": "user", "content": "2"}], role="driver")
    router.complete([{"role": "user", "content": "3"}], role="extra")

    summary = router.cost_summary()
    assert summary["calls"] == 3
    by_prov = summary["by_provider"]
    # Ollama (local) = $0; Anthropic call cost
    assert by_prov.get("ollama", 0) == 0.0
    assert by_prov.get("anthropic", 0) > 0


def test_router_enforces_cost_ceiling():
    expensive = FakeProvider(name="anthropic", handles_prefix="claude-",
                             scripted=["a", "b", "c"])
    router = ModelRouter(
        providers=[expensive],
        roles={"driver": RoleConfig(primary="claude-opus-4-8")},  # $15/$75 per 1M
        cost_ceiling_usd=0.000001,  # tiny
    )
    # First call burns past the ceiling
    router.complete([{"role": "user", "content": "1"}])
    # Second call should refuse
    with pytest.raises(CostCeilingExceeded):
        router.complete([{"role": "user", "content": "2"}])


# =============================================================================
# Escalation logic
# =============================================================================


def test_escalation_state_should_escalate_on_explicit():
    s = EscalationState()
    assert not s.should_escalate()
    s.explicit = True
    assert s.should_escalate()


def test_escalation_state_should_escalate_on_intent_mismatch_x2():
    s = EscalationState()
    s.intent_mismatch = 1
    assert not s.should_escalate()
    s.intent_mismatch = 2
    assert s.should_escalate()


def test_escalation_state_reset():
    s = EscalationState(intent_mismatch=3, parse_format=2, explicit=True)
    assert s.should_escalate()
    s.reset()
    assert not s.should_escalate()


def test_router_explicit_escalation_skips_primary():
    primary = FakeProvider(name="ollama", handles_prefix="local-",
                           scripted=["primary-result"])
    escalated = FakeProvider(name="anthropic", handles_prefix="claude-",
                             scripted=["escalated-result"])
    router = _build_router(primary, escalated)

    router.request_escalation("driver")
    comp = router.complete([{"role": "user", "content": "hi"}])
    assert comp.content == "escalated-result"
    assert primary.call_count == 0  # primary skipped
    assert escalated.call_count == 1


def test_router_intent_mismatch_x2_triggers_escalation():
    primary = FakeProvider(name="ollama", handles_prefix="local-",
                           scripted=["x", "x", "x"])
    escalated = FakeProvider(name="anthropic", handles_prefix="claude-",
                             scripted=["smart"])
    router = _build_router(primary, escalated)

    # First mismatch — primary still used
    router.note_intent_mismatch("driver")
    comp = router.complete([{"role": "user", "content": "1"}])
    assert comp.model_used == "local-default"

    # Second mismatch — next call escalates
    router.note_intent_mismatch("driver")
    comp = router.complete([{"role": "user", "content": "2"}])
    assert comp.content == "smart"


def test_router_reset_escalation_clears_state():
    router = _build_router(FakeProvider(name="o", handles_prefix="local-"))
    router.request_escalation("driver")
    router.note_intent_mismatch("driver")
    router.note_intent_mismatch("driver")
    state = router.get_escalation("driver")
    assert state.should_escalate()
    router.reset_escalation("driver")
    state = router.get_escalation("driver")
    assert not state.should_escalate()


def test_router_resets_explicit_after_successful_call():
    """After a successful call, the explicit flag clears so the NEXT call
    goes back to primary unless the user re-invokes /escalate."""
    primary = FakeProvider(name="ollama", handles_prefix="local-",
                           scripted=["a"])
    escalated = FakeProvider(name="anthropic", handles_prefix="claude-",
                             scripted=["b"])
    router = _build_router(primary, escalated)

    router.request_escalation("driver")
    router.complete([{"role": "user", "content": "1"}])  # uses escalated

    # Next call should be back on primary
    router.complete([{"role": "user", "content": "2"}])
    assert primary.call_count == 1
    assert escalated.call_count == 1


# =============================================================================
# Default roles auto-detect env keys
# =============================================================================


def test_default_roles_no_keys(monkeypatch):
    """With no API keys, driver has empty escalation chain."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from forge.router import default_roles
    roles = default_roles()
    assert roles["driver"].escalation == []


def test_default_roles_with_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from forge.router import default_roles
    roles = default_roles()
    assert "claude-sonnet-4-6" in roles["driver"].escalation
    assert roles["planner"].primary == "claude-sonnet-4-6"


def test_default_roles_with_openai_key_no_anthropic(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from forge.router import default_roles
    roles = default_roles()
    assert "gpt-5" in roles["driver"].escalation


# =============================================================================
# Streaming through a multi-provider chain
# =============================================================================


def test_router_stream_chunks_from_chosen_provider():
    p = FakeProvider(name="ollama", handles_prefix="local-",
                     scripted=["streaming hello"])
    router = _build_router(p)

    chunks = list(router.complete_stream([{"role": "user", "content": "hi"}]))
    final = chunks[-1]
    assert final.is_final
    assert final.completion is not None
    assert final.completion.content == "streaming hello"
    # The accumulated contents grow across chunks
    deltas = [c.delta for c in chunks if c.delta]
    assert "".join(deltas) == "streaming hello"

"""forge.router — multi-provider model router with role-based escalation.

Five named roles, each with a primary model and an escalation chain:

    driver      — main agent loop. Default: gpt-oss:20b on Ollama.
    planner     — pre-execution plan (Plan mode). Default: gpt-oss:20b at high effort.
    vision      — see() sub-skill. Default: qwen2.5vl on Ollama.
    classifier  — auto-mode safety check. Default: gpt-oss:20b at low effort.
    summarizer  — context truncation. Default: gpt-oss:20b at low effort.

The router itself is a thin orchestrator over a CHAIN OF PROVIDERS
(see forge.providers). Each provider knows how to talk to one backend
(Ollama, Anthropic, OpenAI). The router:

  * Picks the right provider for each model id (first one whose
    `handles(model_id)` returns True wins)
  * Walks the role's escalation chain on errors
  * Tracks cost across providers
  * Enforces the per-session cost ceiling
  * Implements escalation triggers (intent-mismatch×2, format-fail×2,
    explicit /escalate command — promotes the next call to the next
    model in the role's chain)

Two completion APIs:
  * `complete(messages, role=...)` returns a Completion when done (buffered).
  * `complete_stream(messages, role=...)` yields StreamChunk objects as
    content arrives. Use this for the chat REPL.

To configure providers/models per role, edit ~/.forge/router.toml or set
FORGE_DRIVER_MODEL.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field

from forge.config import (
    DEFAULT_DRIVER_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_SESSION_COST_CEILING_USD,
)
from forge.providers import (
    Completion,
    Provider,
    StreamChunk,
    default_providers,
)

__all__ = [
    "Completion",
    "StreamChunk",
    "RoleConfig",
    "ModelRouter",
    "CostCeilingExceeded",
    "EscalationState",
    "default_roles",
]


# =============================================================================
# Role config
# =============================================================================


@dataclass
class RoleConfig:
    """One role's model preference: primary + ordered escalation chain.

    `primary` is tried first. On error or explicit escalation, the router
    moves to the next entry in `escalation`. When the chain exhausts, the
    router raises RuntimeError.
    """

    primary: str
    escalation: list[str] = field(default_factory=list)
    effort: str = "medium"
    num_ctx: int = DEFAULT_NUM_CTX


def default_roles() -> dict[str, RoleConfig]:
    """The out-of-the-box role table.

    Driver / classifier / summarizer / planner default to gpt-oss:20b on
    Ollama. If ANTHROPIC_API_KEY is set, the driver and planner roles get
    Claude appended to their escalation chain automatically. Same for OpenAI.
    """
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))

    driver_escalation: list[str] = []
    planner_primary = DEFAULT_DRIVER_MODEL
    if has_anthropic:
        driver_escalation.append("claude-sonnet-4-6")
        planner_primary = "claude-sonnet-4-6"
    elif has_openai:
        driver_escalation.append("gpt-5")
        planner_primary = "gpt-5"

    return {
        "driver": RoleConfig(
            primary=DEFAULT_DRIVER_MODEL,
            escalation=driver_escalation,
            effort="medium",
            num_ctx=DEFAULT_NUM_CTX,
        ),
        "classifier": RoleConfig(primary=DEFAULT_DRIVER_MODEL, effort="low"),
        "summarizer": RoleConfig(primary=DEFAULT_DRIVER_MODEL, effort="low"),
        "planner": RoleConfig(primary=planner_primary, effort="high"),
        "vision": RoleConfig(primary="qwen2.5vl:7b"),
    }


# =============================================================================
# Escalation state — tracks the conditions that promote a call up the chain.
# =============================================================================


@dataclass
class EscalationState:
    """Per-role counters for failure modes that should bump the model up.

    Reset on every successful (gate-allowed) cell. The session.py loop owns
    these — it tells the router 'this role just failed an intent check, what
    do you want to do?' via `note_failure(role, kind)`. The router decides
    when the threshold is hit and switches the role's effective primary.
    """

    intent_mismatch: int = 0
    parse_format: int = 0
    explicit: bool = False     # set by /escalate command

    def reset(self) -> None:
        self.intent_mismatch = 0
        self.parse_format = 0
        self.explicit = False

    def should_escalate(self) -> bool:
        return (
            self.explicit
            or self.intent_mismatch >= 2
            or self.parse_format >= 2
        )


# =============================================================================
# The router
# =============================================================================


class CostCeilingExceeded(RuntimeError):
    """Raised when this session has spent its budget."""


class ModelRouter:
    """Selects a provider per model id, walks escalation chain on errors,
    tracks cost.

    Pass a custom `providers` list for tests; default uses
    forge.providers.default_providers() which auto-detects available SDKs
    and env vars.
    """

    def __init__(
        self,
        *,
        roles: dict[str, RoleConfig] | None = None,
        providers: list[Provider] | None = None,
        cost_ceiling_usd: float = DEFAULT_SESSION_COST_CEILING_USD,
    ):
        self.roles = roles or default_roles()
        self.providers = providers or default_providers()
        self.cost_ceiling_usd = cost_ceiling_usd
        self.spent_usd: float = 0.0
        self.calls: list[Completion] = []
        # Per-role escalation state. Keys created lazily.
        self._escalation: dict[str, EscalationState] = {}

    # ---- introspection --------------------------------------------------

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cost_ceiling_usd - self.spent_usd)

    def cost_summary(self) -> dict[str, object]:
        per_provider: dict[str, float] = {}
        for c in self.calls:
            # Look up which provider handled this model.
            prov_name = self._provider_name_for(c.model_used)
            per_provider[prov_name] = per_provider.get(prov_name, 0.0) + c.cost_usd
        return {
            "ceiling_usd": self.cost_ceiling_usd,
            "spent_usd": round(self.spent_usd, 4),
            "remaining_usd": round(self.remaining_usd, 4),
            "calls": len(self.calls),
            "by_provider": {k: round(v, 4) for k, v in per_provider.items()},
        }

    # ---- escalation -----------------------------------------------------

    def get_escalation(self, role: str) -> EscalationState:
        """Get (and lazily create) the EscalationState for a role."""
        if role not in self._escalation:
            self._escalation[role] = EscalationState()
        return self._escalation[role]

    def request_escalation(self, role: str = "driver") -> None:
        """Mark this role as wanting to escalate on the next call.

        Called from the CLI via `/escalate` and from session.turn() when it
        detects intent-mismatch×2 or parse-format×2.
        """
        self.get_escalation(role).explicit = True

    def note_intent_mismatch(self, role: str = "driver") -> None:
        st = self.get_escalation(role)
        st.intent_mismatch += 1

    def note_parse_format_fail(self, role: str = "driver") -> None:
        st = self.get_escalation(role)
        st.parse_format += 1

    def reset_escalation(self, role: str = "driver") -> None:
        self.get_escalation(role).reset()

    # ---- provider selection ---------------------------------------------

    def _provider_for(self, model: str) -> Provider:
        """Find the first provider in the chain that handles this model."""
        for prov in self.providers:
            if prov.handles(model):
                return prov
        # Should never reach here — OllamaProvider is the catch-all default.
        raise RuntimeError(
            f"no provider handles model {model!r}. Available providers: "
            f"{[p.name for p in self.providers]}"
        )

    def _provider_name_for(self, model: str) -> str:
        try:
            return self._provider_for(model).name
        except RuntimeError:
            return "unknown"

    def _attempt_models(self, role: str) -> list[str]:
        """Pick the model order based on escalation state.

        If the role's escalation state says 'should_escalate()', we DROP the
        primary and start at escalation[0]. The state is consumed (reset)
        after the next successful call.
        """
        cfg = self.roles.get(role)
        if cfg is None:
            raise ValueError(f"unknown role: {role!r}")
        st = self.get_escalation(role)
        if st.should_escalate() and cfg.escalation:
            return list(cfg.escalation)
        return [cfg.primary, *cfg.escalation]

    # ---- the call -------------------------------------------------------

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        role: str = "driver",
        max_tokens: int = 2048,
    ) -> Completion:
        """Make a model call for the given role.

        Walks the escalation chain on provider errors (network, 5xx, parse
        errors). Cost ceiling is enforced before the call.
        """
        if self.spent_usd >= self.cost_ceiling_usd:
            raise CostCeilingExceeded(
                f"session has spent ${self.spent_usd:.2f} of "
                f"${self.cost_ceiling_usd:.2f} ceiling; "
                f"reset with `forge cost reset` or raise FORGE_COST_CEILING_USD"
            )

        cfg = self.roles[role]
        attempt_models = self._attempt_models(role)
        last_err: Exception | None = None

        for model in attempt_models:
            provider = self._provider_for(model)
            try:
                comp = provider.complete(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=0.0,
                    effort=cfg.effort,
                    num_ctx=cfg.num_ctx,
                    role=role,
                )
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue
            self.calls.append(comp)
            self.spent_usd += comp.cost_usd
            # Successful call resets explicit-escalation flag (counters are
            # reset by session.py when a cell actually succeeds).
            self.get_escalation(role).explicit = False
            return comp

        raise RuntimeError(f"all model attempts failed; last error: {last_err}")

    def complete_stream(
        self,
        messages: list[dict[str, str]],
        *,
        role: str = "driver",
        max_tokens: int = 2048,
    ) -> Iterator[StreamChunk]:
        """Streaming variant. Picks ONE model up front (escalation walks
        before the first chunk only — once streaming starts we don't switch
        models mid-stream)."""
        if self.spent_usd >= self.cost_ceiling_usd:
            raise CostCeilingExceeded(
                f"session has spent ${self.spent_usd:.2f} of "
                f"${self.cost_ceiling_usd:.2f} ceiling"
            )

        cfg = self.roles[role]
        attempt_models = self._attempt_models(role)
        last_err: Exception | None = None

        # Find a working provider+model first (no yields until we have one).
        chosen_provider: Provider | None = None
        chosen_model = ""
        for model in attempt_models:
            provider = self._provider_for(model)
            # Construct the iterator — but the first yield happens inside
            # the loop below, so we need to test by actually starting the
            # generator. We delegate to provider.complete_stream and let it
            # raise on connection errors before producing chunks; but
            # generators don't raise until iterated, so we wrap a small
            # peek-ahead.
            try:
                # We optimistically pick this provider; errors during stream
                # are reported in the final chunk's completion.finish_reason.
                stream = provider.complete_stream(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=0.0,
                    effort=cfg.effort,
                    num_ctx=cfg.num_ctx,
                    role=role,
                )
                chosen_provider = provider
                chosen_model = model
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue

        if chosen_provider is None:
            raise RuntimeError(
                f"all model attempts failed; last error: {last_err}"
            )

        # Forward chunks. Account on the final one.
        final_completion: Completion | None = None
        try:
            for chunk in stream:
                if chunk.is_final:
                    final_completion = chunk.completion
                yield chunk
        finally:
            if final_completion is not None:
                self.calls.append(final_completion)
                self.spent_usd += final_completion.cost_usd
                self.get_escalation(role).explicit = False

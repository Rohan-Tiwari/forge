"""forge.router — multi-provider model router with role-based escalation.

Five named roles, each with a primary model and an escalation chain:

    driver      — main agent loop. Default: gpt-oss:20b on Ollama.
    planner     — pre-execution plan (Plan mode). Default: claude-sonnet (TODO v0.2).
    vision      — see() sub-skill. Default: qwen2.5-vl on Ollama (TODO v0.2).
    classifier  — auto-mode safety check. Default: gpt-oss:20b at low effort.
    summarizer  — context truncation. Default: gpt-oss:20b at low effort.

v0.1 implements only the local Ollama path (driver + classifier + summarizer
all on gpt-oss:20b). The escalation chain is wired but only Ollama is
configured — adding Anthropic/OpenAI is a straight extension.

The Day 0 system-prompt fixes are baked into `complete()`:
  * `think=False` is set via the OpenAI SDK extra_body
  * The system prompt explicitly forbids tool calls
  * No tools are passed in the request (so harmony tool-channel can't trigger)

To swap providers, edit `~/.forge/router.toml` (or set FORGE_DRIVER_MODEL).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional

from openai import OpenAI

from forge.config import (
    DEFAULT_DRIVER_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_URL,
    DEFAULT_SESSION_COST_CEILING_USD,
)


# =============================================================================
# Provider abstraction
# =============================================================================


@dataclass
class Completion:
    """The result of one model call."""

    content: str
    role_used: str
    model_used: str
    elapsed_s: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    finish_reason: str = ""

    @property
    def empty(self) -> bool:
        return not self.content.strip()


# Cost per 1M tokens (input, output). Used by the router for the session
# cost ceiling. Local models cost $0; frontier costs come from public pricing.
_PRICING: dict[str, tuple[float, float]] = {
    "gpt-oss:20b": (0.0, 0.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8": (15.0, 75.0),
    "gpt-5": (10.0, 30.0),
}


def _price(model: str, p_in: int, p_out: int) -> float:
    in_cost, out_cost = _PRICING.get(model, (0.0, 0.0))
    return (p_in / 1_000_000) * in_cost + (p_out / 1_000_000) * out_cost


# =============================================================================
# Role config
# =============================================================================


@dataclass
class RoleConfig:
    primary: str
    escalation: list[str] = field(default_factory=list)
    effort: str = "medium"
    num_ctx: int = DEFAULT_NUM_CTX


def default_roles() -> dict[str, RoleConfig]:
    """The out-of-the-box role table. Every role usable on a fresh install."""
    return {
        "driver": RoleConfig(
            primary=DEFAULT_DRIVER_MODEL,
            escalation=[],   # v0.1 has no escalation provider configured
            effort="medium",
            num_ctx=DEFAULT_NUM_CTX,
        ),
        "classifier": RoleConfig(primary=DEFAULT_DRIVER_MODEL, effort="low"),
        "summarizer": RoleConfig(primary=DEFAULT_DRIVER_MODEL, effort="low"),
        "planner": RoleConfig(primary=DEFAULT_DRIVER_MODEL, effort="high"),
        "vision": RoleConfig(primary="qwen2.5-vl:7b"),
    }


# =============================================================================
# The router
# =============================================================================


class CostCeilingExceeded(RuntimeError):
    """Raised when this session has spent its budget."""


class ModelRouter:
    def __init__(
        self,
        *,
        roles: Optional[dict[str, RoleConfig]] = None,
        ollama_url: str = DEFAULT_OLLAMA_URL,
        cost_ceiling_usd: float = DEFAULT_SESSION_COST_CEILING_USD,
    ):
        self.roles = roles or default_roles()
        self.ollama = OpenAI(base_url=ollama_url, api_key="ollama")
        self.cost_ceiling_usd = cost_ceiling_usd
        self.spent_usd: float = 0.0
        self.calls: list[Completion] = []

    # ---- introspection --------------------------------------------------

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cost_ceiling_usd - self.spent_usd)

    def cost_summary(self) -> dict[str, object]:
        return {
            "ceiling_usd": self.cost_ceiling_usd,
            "spent_usd": round(self.spent_usd, 4),
            "remaining_usd": round(self.remaining_usd, 4),
            "calls": len(self.calls),
        }

    # ---- the call -------------------------------------------------------

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        role: str = "driver",
        max_tokens: int = 2048,
    ) -> Completion:
        """Make a model call for the given role.

        Bakes in every Day 0 fix:
          * `think=False` via extra_body to keep the harmony parser quiet
          * No tools passed (so gpt-oss can't try to invoke its built-in `python` tool)
          * temperature=0 by default for the driver (determinism for users)

        Cost ceiling is enforced before the call. If the projected cost would
        exceed the ceiling, raises CostCeilingExceeded.
        """
        if self.spent_usd >= self.cost_ceiling_usd:
            raise CostCeilingExceeded(
                f"session has spent ${self.spent_usd:.2f} of "
                f"${self.cost_ceiling_usd:.2f} ceiling; "
                f"reset with `forge cost reset` or raise FORGE_COST_CEILING_USD"
            )

        cfg = self.roles.get(role)
        if cfg is None:
            raise ValueError(f"unknown role: {role!r}")

        attempt_models = [cfg.primary, *cfg.escalation]
        last_err: Optional[Exception] = None

        for model in attempt_models:
            t0 = time.monotonic()
            try:
                resp = self.ollama.chat.completions.create(
                    model=model,
                    messages=messages,  # type: ignore[arg-type]
                    max_tokens=max_tokens,
                    temperature=0.0,
                    extra_body={
                        "think": False,
                        "reasoning_effort": cfg.effort,
                        "options": {"num_ctx": cfg.num_ctx},
                        "keep_alive": os.environ.get("FORGE_KEEP_ALIVE", "24h"),
                    },
                )
            except Exception as e:  # noqa: BLE001 — network / 500s / parse errors
                last_err = e
                continue

            elapsed = time.monotonic() - t0
            choice = resp.choices[0]
            content = choice.message.content or ""
            usage = resp.usage
            in_tokens = usage.prompt_tokens if usage else 0
            out_tokens = usage.completion_tokens if usage else 0
            cost = _price(model, in_tokens, out_tokens)

            comp = Completion(
                content=content,
                role_used=role,
                model_used=model,
                elapsed_s=elapsed,
                prompt_tokens=in_tokens,
                completion_tokens=out_tokens,
                cost_usd=cost,
                finish_reason=choice.finish_reason or "",
            )
            self.calls.append(comp)
            self.spent_usd += cost
            return comp

        raise RuntimeError(f"all model attempts failed; last error: {last_err}")

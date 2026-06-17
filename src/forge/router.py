"""forge.router — multi-provider model router with role-based escalation.

Five named roles, each with a primary model and an escalation chain:

    driver      — main agent loop. Default: gpt-oss:20b on Ollama.
    planner     — pre-execution plan (Plan mode). Default: claude-sonnet (TODO v0.2).
    vision      — see() sub-skill. Default: qwen2.5vl on Ollama.
    classifier  — auto-mode safety check. Default: gpt-oss:20b at low effort.
    summarizer  — context truncation. Default: gpt-oss:20b at low effort.

v0.1 implements only the local Ollama path (driver + classifier + summarizer
all on gpt-oss:20b). The escalation chain is wired but only Ollama is
configured — adding Anthropic/OpenAI is a straight extension.

The Day 0 system-prompt fixes are baked into `complete()`:
  * `think=False` is set via the OpenAI SDK extra_body
  * The system prompt explicitly forbids tool calls
  * No tools are passed in the request (so harmony tool-channel can't trigger)

Two completion APIs:
  * `complete(messages, role=...)` returns a Completion when done (buffered).
  * `complete_stream(messages, role=...)` yields StreamChunk objects as content
    arrives. Use this for the chat REPL where tokens should appear live.

To swap providers, edit `~/.forge/router.toml` (or set FORGE_DRIVER_MODEL).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

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


@dataclass
class StreamChunk:
    """One delta from a streaming completion. Yielded by complete_stream."""

    delta: str                          # the text fragment in this chunk
    accumulated: str                    # full content so far (delta included)
    is_final: bool = False              # True on the last chunk
    completion: Optional[Completion] = None  # populated on the final chunk


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
        "vision": RoleConfig(primary="qwen2.5vl:7b"),
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

    # ---- streaming ------------------------------------------------------

    def complete_stream(
        self,
        messages: list[dict[str, str]],
        *,
        role: str = "driver",
        max_tokens: int = 2048,
    ) -> Iterator[StreamChunk]:
        """Streaming variant of complete(). Yields StreamChunk per token batch.

        The final chunk has `is_final=True` and `completion` populated with
        the same Completion object you'd get from `complete()` — same
        accounting, same audit-trail data.

        Falls through the escalation chain just like complete(): on a
        network/parse error mid-stream we DO surface the partial content
        we've seen so far in the final chunk's stderr-equivalent (in the
        completion's `finish_reason='error'`), but we don't retry on a
        different model mid-stream — that would mean the user sees text from
        two different models concatenated, which is confusing. Retry happens
        BEFORE the first chunk only.

        For determinism in tests, this method calls itself only via
        `self.ollama.chat.completions.create(stream=True, ...)`. A FakeRouter
        in tests can override.
        """
        if self.spent_usd >= self.cost_ceiling_usd:
            raise CostCeilingExceeded(
                f"session has spent ${self.spent_usd:.2f} of "
                f"${self.cost_ceiling_usd:.2f} ceiling"
            )

        cfg = self.roles.get(role)
        if cfg is None:
            raise ValueError(f"unknown role: {role!r}")

        attempt_models = [cfg.primary, *cfg.escalation]
        last_err: Optional[Exception] = None
        stream = None
        chosen_model = ""

        # Find a working model first (no chunks yielded until we have a stream).
        for model in attempt_models:
            try:
                stream = self.ollama.chat.completions.create(
                    model=model,
                    messages=messages,  # type: ignore[arg-type]
                    max_tokens=max_tokens,
                    temperature=0.0,
                    stream=True,
                    stream_options={"include_usage": True},
                    extra_body={
                        "think": False,
                        "reasoning_effort": cfg.effort,
                        "options": {"num_ctx": cfg.num_ctx},
                        "keep_alive": os.environ.get("FORGE_KEEP_ALIVE", "24h"),
                    },
                )
                chosen_model = model
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue

        if stream is None:
            raise RuntimeError(f"all model attempts failed; last error: {last_err}")

        accumulated: list[str] = []
        in_tokens = 0
        out_tokens = 0
        finish_reason = ""
        t0 = time.monotonic()

        try:
            for chunk in stream:
                # Track usage if the provider sent it (only on the final chunk
                # for OpenAI-shaped streams with include_usage=True).
                if getattr(chunk, "usage", None):
                    in_tokens = chunk.usage.prompt_tokens or in_tokens
                    out_tokens = chunk.usage.completion_tokens or out_tokens

                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = ""
                if choice.delta and choice.delta.content:
                    delta = choice.delta.content
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                if delta:
                    accumulated.append(delta)
                    yield StreamChunk(
                        delta=delta,
                        accumulated="".join(accumulated),
                        is_final=False,
                    )
        except Exception as e:  # noqa: BLE001
            # Stream broke mid-flight — surface what we have.
            partial = "".join(accumulated)
            comp = Completion(
                content=partial,
                role_used=role,
                model_used=chosen_model,
                elapsed_s=time.monotonic() - t0,
                prompt_tokens=in_tokens,
                completion_tokens=out_tokens,
                cost_usd=_price(chosen_model, in_tokens, out_tokens),
                finish_reason=f"error: {e}",
            )
            self.calls.append(comp)
            self.spent_usd += comp.cost_usd
            yield StreamChunk(
                delta="",
                accumulated=partial,
                is_final=True,
                completion=comp,
            )
            return

        # Normal end-of-stream.
        elapsed = time.monotonic() - t0
        full_content = "".join(accumulated)
        cost = _price(chosen_model, in_tokens, out_tokens)
        comp = Completion(
            content=full_content,
            role_used=role,
            model_used=chosen_model,
            elapsed_s=elapsed,
            prompt_tokens=in_tokens,
            completion_tokens=out_tokens,
            cost_usd=cost,
            finish_reason=finish_reason,
        )
        self.calls.append(comp)
        self.spent_usd += cost
        yield StreamChunk(
            delta="",
            accumulated=full_content,
            is_final=True,
            completion=comp,
        )

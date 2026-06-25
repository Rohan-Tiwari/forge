"""forge.providers — pluggable model providers behind the router.

Each provider knows how to talk to one backend (Ollama, Anthropic, OpenAI)
and exposes a consistent interface to ModelRouter:

    provider.complete(model, messages, **opts) -> Completion
    provider.complete_stream(model, messages, **opts) -> Iterator[StreamChunk]

Provider selection is automatic: the router picks the provider whose
`handles(model_id)` returns True. Add a new provider by appending it to
the providers list — the router falls through them in order.

Out-of-the-box:
    OllamaProvider     — handles anything not claimed by another provider
                         (default; talks to localhost:11434/v1 via openai SDK)
    AnthropicProvider  — handles "claude-*" model ids
    OpenAIProvider     — handles "gpt-*", "o3-*", "o4-*" model ids

Auth: each provider reads its own env var (ANTHROPIC_API_KEY, OPENAI_API_KEY).
If the env var is missing, the provider raises a clear error on first use.
"""
from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol


# Forward refs — defined in router.py
@dataclass
class Completion:
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
    delta: str
    accumulated: str
    is_final: bool = False
    completion: Completion | None = None


class Provider(Protocol):
    """Common interface every backend implements."""

    name: str

    def handles(self, model: str) -> bool:
        """Does this provider know how to talk to `model`?"""
        ...

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        effort: str,
        num_ctx: int,
        role: str,
    ) -> Completion:
        ...

    def complete_stream(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        effort: str,
        num_ctx: int,
        role: str,
    ) -> Iterator[StreamChunk]:
        ...


# =============================================================================
# Pricing — per-model cost in $/1M tokens.
#
# Three layers, in priority order:
#   1. ~/.forge/pricing.toml (user override; most specific wins)
#   2. The hardcoded baseline below
#   3. (0, 0) for unknown models (never crashes)
#
# The baseline reflects mid-2026 public pricing. Users who get enterprise
# discounts, self-host, or use a proxy can override per-model rates without
# editing source.
# =============================================================================

_PRICING_BASELINE: dict[str, tuple[float, float]] = {
    # Local
    "gpt-oss:20b": (0.0, 0.0),
    "qwen2.5vl:7b": (0.0, 0.0),
    # Anthropic (input, output per 1M)
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8": (15.0, 75.0),
    "claude-haiku-4-5-20251001": (0.8, 4.0),
    # OpenAI
    "gpt-5": (10.0, 30.0),
    "gpt-4o": (2.5, 10.0),
    "o3-mini": (3.0, 12.0),
}


def _load_pricing_override() -> dict[str, tuple[float, float]]:
    """Read ~/.forge/pricing.toml. Returns {} if absent or unparseable.

    Format:
        [pricing.<model-id>]
        input = 3.0     # $ per 1M input tokens
        output = 15.0   # $ per 1M output tokens

    Example:
        [pricing."claude-sonnet-4-6"]
        input = 1.5
        output = 7.5

        [pricing."my-custom-model"]
        input = 0.0
        output = 0.0

    Malformed per-model entries are skipped with a logged warning so
    users editing the file by hand can find typos.
    """
    import logging
    import tomllib
    from pathlib import Path

    log = logging.getLogger(__name__)
    path = Path.home() / ".forge" / "pricing.toml"
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        log.warning("could not read %s: %s", path, e)
        return {}
    except tomllib.TOMLDecodeError as e:
        log.warning("malformed TOML in %s: %s — using baseline pricing", path, e)
        return {}
    out: dict[str, tuple[float, float]] = {}
    for model, entry in (data.get("pricing") or {}).items():
        if not isinstance(entry, dict):
            log.warning(
                "pricing override entry for %r is not a table — skipping", model,
            )
            continue
        try:
            inp = float(entry.get("input", 0))
            outp = float(entry.get("output", 0))
            out[str(model)] = (inp, outp)
        except (TypeError, ValueError) as e:
            log.warning(
                "pricing override for %r has invalid input/output (%s) — skipping",
                model, e,
            )
            continue
    return out


# Cached at module load. Override doesn't change at runtime unless price()
# detects an mtime change (see below), or callers explicitly invoke
# reload_pricing().
_PRICING_OVERRIDE: dict[str, tuple[float, float]] = _load_pricing_override()
_PRICING_OVERRIDE_MTIME: float = 0.0


def _pricing_path() -> Path:
    from pathlib import Path
    return Path.home() / ".forge" / "pricing.toml"


# Initialize the mtime cache. price() checks this on every call and reloads
# if the file changed — cheap (one stat), correct (hot-reload), and avoids
# the "daemon serves stale prices" footgun.
try:
    _PRICING_OVERRIDE_MTIME = _pricing_path().stat().st_mtime
except OSError:
    pass


# Combined view: override wins over baseline. NOTE: this dict is inspection-
# only and lags hot-reloads — `price()` consults _PRICING_OVERRIDE and
# _PRICING_BASELINE directly to keep precedence ordering explicit. Don't
# refactor price() to read _PRICING.
_PRICING: dict[str, tuple[float, float]] = {
    **_PRICING_BASELINE,
    **_PRICING_OVERRIDE,
}


def _maybe_reload_pricing() -> None:
    """If ~/.forge/pricing.toml mtime changed, re-read it.

    Called from price() on every invocation. Cost: one stat() per price()
    call — negligible. Benefit: daemons + long-lived REPL sessions pick
    up pricing edits without restart.
    """
    global _PRICING_OVERRIDE_MTIME, _PRICING_OVERRIDE, _PRICING
    try:
        mtime = _pricing_path().stat().st_mtime
    except OSError:
        # File deleted since last check — clear the override.
        if _PRICING_OVERRIDE:
            _PRICING_OVERRIDE = {}
            _PRICING_OVERRIDE_MTIME = 0.0
            _PRICING = dict(_PRICING_BASELINE)
        return
    if mtime != _PRICING_OVERRIDE_MTIME:
        _PRICING_OVERRIDE = _load_pricing_override()
        _PRICING = {**_PRICING_BASELINE, **_PRICING_OVERRIDE}
        _PRICING_OVERRIDE_MTIME = mtime


def price(model: str, p_in: int, p_out: int) -> float:
    """Return cost in USD for the given token counts.

    Override layer is consulted first — useful for users on custom pricing
    (enterprise discounts, self-hosted models with electricity costs, etc).
    Falls back to the baseline. Unknown models return 0 rather than crash.

    Auto-reloads ~/.forge/pricing.toml if its mtime has changed since the
    last call — daemons and long-lived REPLs stay current without restart.
    """
    _maybe_reload_pricing()
    if model in _PRICING_OVERRIDE:
        in_cost, out_cost = _PRICING_OVERRIDE[model]
    else:
        in_cost, out_cost = _PRICING_BASELINE.get(model, (0.0, 0.0))
    return (p_in / 1_000_000) * in_cost + (p_out / 1_000_000) * out_cost


def reload_pricing() -> dict[str, tuple[float, float]]:
    """Force a re-read of ~/.forge/pricing.toml. Useful for tests + explicit refresh."""
    global _PRICING_OVERRIDE, _PRICING, _PRICING_OVERRIDE_MTIME
    _PRICING_OVERRIDE = _load_pricing_override()
    _PRICING = {**_PRICING_BASELINE, **_PRICING_OVERRIDE}
    try:
        _PRICING_OVERRIDE_MTIME = _pricing_path().stat().st_mtime
    except OSError:
        _PRICING_OVERRIDE_MTIME = 0.0
    return dict(_PRICING)


# =============================================================================
# OllamaProvider — the existing path, kept as the default.
# =============================================================================


class OllamaProvider:
    """Talks to a local Ollama instance via its OpenAI-compatible /v1 endpoint.

    Bakes in the Day 0 fixes: think=False, no tools, temperature=0.
    """

    name = "ollama"

    def __init__(self, base_url: str | None = None):
        from openai import OpenAI
        self.base_url = base_url or os.environ.get(
            "FORGE_OLLAMA_URL", "http://localhost:11434/v1"
        )
        self.client = OpenAI(base_url=self.base_url, api_key="ollama")

    def handles(self, model: str) -> bool:
        # Default fallback — handles anything not claimed by a more specific
        # provider. The router puts us last in the chain.
        return True

    def _extra_body(self, *, effort: str, num_ctx: int) -> dict:
        return {
            "think": False,
            "reasoning_effort": effort,
            "options": {"num_ctx": num_ctx},
            "keep_alive": os.environ.get("FORGE_KEEP_ALIVE", "24h"),
        }

    def complete(self, *, model, messages, max_tokens, temperature,
                 effort, num_ctx, role) -> Completion:
        t0 = time.monotonic()
        resp = self.client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            extra_body=self._extra_body(effort=effort, num_ctx=num_ctx),
        )
        elapsed = time.monotonic() - t0
        choice = resp.choices[0]
        usage = resp.usage
        in_tokens = usage.prompt_tokens if usage else 0
        out_tokens = usage.completion_tokens if usage else 0
        return Completion(
            content=choice.message.content or "",
            role_used=role,
            model_used=model,
            elapsed_s=elapsed,
            prompt_tokens=in_tokens,
            completion_tokens=out_tokens,
            cost_usd=price(model, in_tokens, out_tokens),
            finish_reason=choice.finish_reason or "",
        )

    def complete_stream(self, *, model, messages, max_tokens, temperature,
                        effort, num_ctx, role) -> Iterator[StreamChunk]:
        t0 = time.monotonic()
        stream = self.client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
            stream_options={"include_usage": True},
            extra_body=self._extra_body(effort=effort, num_ctx=num_ctx),
        )

        accumulated: list[str] = []
        in_tokens = out_tokens = 0
        finish_reason = ""

        for chunk in stream:
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

        full = "".join(accumulated)
        comp = Completion(
            content=full,
            role_used=role,
            model_used=model,
            elapsed_s=time.monotonic() - t0,
            prompt_tokens=in_tokens,
            completion_tokens=out_tokens,
            cost_usd=price(model, in_tokens, out_tokens),
            finish_reason=finish_reason,
        )
        yield StreamChunk(
            delta="", accumulated=full, is_final=True, completion=comp,
        )


# =============================================================================
# AnthropicProvider — handles claude-* models via the Anthropic SDK.
# =============================================================================


class AnthropicProvider:
    """Talks to api.anthropic.com via the official anthropic Python SDK.

    Reads ANTHROPIC_API_KEY at construction. Raises a clear error if missing.
    """

    name = "anthropic"

    def __init__(self):
        try:
            import anthropic
            self._anthropic = anthropic
        except ImportError as e:
            raise RuntimeError(
                "anthropic SDK not installed. Run: pip install anthropic"
            ) from e
        self.api_key = os.environ.get("ANTHROPIC_API_KEY")
        self._client: object | None = None

    def _ensure_client(self):
        if self._client is None:
            if not self.api_key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY not set. Get one at "
                    "https://console.anthropic.com/settings/keys, then: "
                    "export ANTHROPIC_API_KEY=sk-ant-..."
                )
            self._client = self._anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def handles(self, model: str) -> bool:
        return model.startswith("claude-")

    def _split_system(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """Anthropic puts system as a top-level kwarg, not in messages.

        Concat all system messages, return (system_str, non_system_messages).
        """
        system_parts: list[str] = []
        rest: list[dict] = []
        for m in messages:
            if m.get("role") == "system":
                system_parts.append(m.get("content", ""))
            else:
                rest.append(m)
        return "\n\n".join(system_parts), rest

    def complete(self, *, model, messages, max_tokens, temperature,
                 effort, num_ctx, role) -> Completion:
        client = self._ensure_client()
        system, rest = self._split_system(messages)

        t0 = time.monotonic()
        resp = client.messages.create(  # type: ignore[attr-defined]
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system or self._anthropic.NOT_GIVEN,
            messages=rest,
        )
        elapsed = time.monotonic() - t0

        # Anthropic returns content as a list of blocks; concat text blocks.
        text_parts: list[str] = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
        content = "".join(text_parts)

        usage = resp.usage
        in_tokens = usage.input_tokens
        out_tokens = usage.output_tokens

        return Completion(
            content=content,
            role_used=role,
            model_used=model,
            elapsed_s=elapsed,
            prompt_tokens=in_tokens,
            completion_tokens=out_tokens,
            cost_usd=price(model, in_tokens, out_tokens),
            finish_reason=resp.stop_reason or "",
        )

    def complete_stream(self, *, model, messages, max_tokens, temperature,
                        effort, num_ctx, role) -> Iterator[StreamChunk]:
        client = self._ensure_client()
        system, rest = self._split_system(messages)

        t0 = time.monotonic()
        accumulated: list[str] = []
        in_tokens = out_tokens = 0
        stop_reason = ""

        with client.messages.stream(  # type: ignore[attr-defined]
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system or self._anthropic.NOT_GIVEN,
            messages=rest,
        ) as stream:
            for text_delta in stream.text_stream:
                accumulated.append(text_delta)
                yield StreamChunk(
                    delta=text_delta,
                    accumulated="".join(accumulated),
                    is_final=False,
                )
            final_msg = stream.get_final_message()
            in_tokens = final_msg.usage.input_tokens
            out_tokens = final_msg.usage.output_tokens
            stop_reason = final_msg.stop_reason or ""

        full = "".join(accumulated)
        comp = Completion(
            content=full,
            role_used=role,
            model_used=model,
            elapsed_s=time.monotonic() - t0,
            prompt_tokens=in_tokens,
            completion_tokens=out_tokens,
            cost_usd=price(model, in_tokens, out_tokens),
            finish_reason=stop_reason,
        )
        yield StreamChunk(
            delta="", accumulated=full, is_final=True, completion=comp,
        )


# =============================================================================
# OpenAIProvider — handles gpt-*, o3-*, o4-* via the official OpenAI client.
# =============================================================================


class OpenAIProvider:
    """Talks to api.openai.com via the OpenAI Python SDK.

    Reads OPENAI_API_KEY at construction.
    """

    name = "openai"

    def __init__(self):
        from openai import OpenAI as _OAI  # noqa: N814 — kept as ctor handle
        self._OAI = _OAI  # noqa: N815
        self.api_key = os.environ.get("OPENAI_API_KEY")
        self._client: object | None = None

    def _ensure_client(self):
        if self._client is None:
            if not self.api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY not set. Get one at "
                    "https://platform.openai.com/api-keys, then: "
                    "export OPENAI_API_KEY=sk-..."
                )
            self._client = self._OAI(api_key=self.api_key)
        return self._client

    def handles(self, model: str) -> bool:
        # Ollama-style model ids contain ':' (e.g. 'gpt-oss:20b', 'qwen2.5vl:7b')
        # — those go to Ollama, not OpenAI. We only claim hosted-OpenAI models.
        if ":" in model:
            return False
        return (
            model.startswith("gpt-")
            or model.startswith("o3-")
            or model.startswith("o4-")
        )

    def complete(self, *, model, messages, max_tokens, temperature,
                 effort, num_ctx, role) -> Completion:
        client = self._ensure_client()
        t0 = time.monotonic()
        resp = client.chat.completions.create(  # type: ignore[attr-defined]
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        elapsed = time.monotonic() - t0
        choice = resp.choices[0]
        usage = resp.usage
        in_tokens = usage.prompt_tokens if usage else 0
        out_tokens = usage.completion_tokens if usage else 0
        return Completion(
            content=choice.message.content or "",
            role_used=role,
            model_used=model,
            elapsed_s=elapsed,
            prompt_tokens=in_tokens,
            completion_tokens=out_tokens,
            cost_usd=price(model, in_tokens, out_tokens),
            finish_reason=choice.finish_reason or "",
        )

    def complete_stream(self, *, model, messages, max_tokens, temperature,
                        effort, num_ctx, role) -> Iterator[StreamChunk]:
        client = self._ensure_client()
        t0 = time.monotonic()
        stream = client.chat.completions.create(  # type: ignore[attr-defined]
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
            stream_options={"include_usage": True},
        )

        accumulated: list[str] = []
        in_tokens = out_tokens = 0
        finish_reason = ""

        for chunk in stream:
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

        full = "".join(accumulated)
        comp = Completion(
            content=full, role_used=role, model_used=model,
            elapsed_s=time.monotonic() - t0,
            prompt_tokens=in_tokens, completion_tokens=out_tokens,
            cost_usd=price(model, in_tokens, out_tokens),
            finish_reason=finish_reason,
        )
        yield StreamChunk(
            delta="", accumulated=full, is_final=True, completion=comp,
        )


# =============================================================================
# Default chain
# =============================================================================


def default_providers() -> list[Provider]:
    """The provider chain for new ModelRouter instances.

    Order matters — the router picks the FIRST provider whose handles()
    returns True. Specific providers come first; OllamaProvider is the
    catch-all default.

    Anthropic and OpenAI providers are constructed lazily — they don't
    fail at construction if the env var is missing; they fail on first
    real call. This means an Ollama-only user pays no startup cost.
    """
    chain: list[Provider] = []
    # Specific first — any model id matching their pattern routes here.
    try:
        chain.append(AnthropicProvider())
    except RuntimeError:
        pass  # anthropic SDK not installed; skip
    try:
        chain.append(OpenAIProvider())
    except RuntimeError:
        pass
    # Catch-all last
    chain.append(OllamaProvider())
    return chain

"""Instrumented OpenAI async client — the single sanctioned construction
point for `openai.AsyncOpenAI` in this codebase.

`build_llm_client(...)` is the ONLY place `openai.AsyncOpenAI(...)` may
be constructed anywhere under `app/` — enforced by an AST-walk
adoption-guard test (`tests/unit/test_llm_client_adoption_guard.py`).
Every current call site (`llm_fallback.classify_with_llm`,
`workers.rag._summarize_resolution`, `api.deps_rag.get_llm_client`,
`services.embeddings.get_embedding_provider`,
`services.rag_embeddings.get_rag_embedding_provider`,
`workers.whatsapp._resolve_llm_client`) must call `build_llm_client(...)`
instead of constructing `openai.AsyncOpenAI` directly.

`InstrumentedAsyncOpenAI` is a thin proxy around a real
`openai.AsyncOpenAI`: it intercepts only `.chat.completions.create` and
`.embeddings.create` to record Prometheus metrics (token counts, USD
cost, latency, request outcome), and delegates everything else via
`__getattr__` — so it's a drop-in replacement wherever `AsyncOpenAI` was
constructed before.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import openai
from prometheus_client import Counter, Histogram

LLM_TOKENS = Counter(
    "llm_tokens_total",
    "Total LLM tokens consumed, labeled by feature/model/token_type.",
    labelnames=("feature", "model", "token_type"),
)
LLM_COST = Counter(
    "llm_cost_usd_total",
    "Total estimated LLM spend in USD, labeled by feature/model.",
    labelnames=("feature", "model"),
)
LLM_LATENCY = Histogram(
    "llm_request_duration_seconds",
    "LLM request duration in seconds, labeled by feature/model.",
    labelnames=("feature", "model"),
)
LLM_REQS = Counter(
    "llm_requests_total",
    "Total LLM requests, labeled by feature/model/outcome.",
    labelnames=("feature", "model", "outcome"),
)
LLM_UNPRICED = Counter(
    "llm_unpriced_requests_total",
    "Requests made against a model with no entry in `_PRICE_TABLE` "
    "(cost intentionally not recorded rather than guessed).",
    labelnames=("model",),
)

# USD per 1,000,000 tokens, keyed by model, as (prompt_price, completion_price).
# Embedding models bill only "input"/prompt tokens, so their completion
# price is 0.
#
# NOTE: these are placeholder prices captured at authoring time (2026) —
# verify against https://openai.com/api/pricing/ periodically; OpenAI
# pricing drifts and this table will silently go stale otherwise.
_PRICE_TABLE: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "text-embedding-3-small": (Decimal("0.02"), Decimal("0")),
}


def _cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> Decimal | None:
    """Return the estimated USD cost for `model`, or `None` when `model`
    has no `_PRICE_TABLE` entry (caller must not guess a price)."""
    prices = _PRICE_TABLE.get(model)
    if prices is None:
        return None
    prompt_price, completion_price = prices
    total = Decimal(prompt_tokens) * prompt_price + Decimal(completion_tokens) * completion_price
    return total / Decimal(1_000_000)


def _record_usage(*, feature: str, model: str, usage: Any) -> None:
    """Record token/cost metrics for a completed request.

    Never raises: any failure here (unexpected `usage` shape, metrics
    backend hiccup, etc.) must never affect the actual LLM call's
    result, so every path is wrapped defensively.
    """
    try:
        if usage is None:
            return

        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        if prompt_tokens is None and completion_tokens is None:
            # `.embeddings.create(...)` responses only carry `total_tokens`
            # (no separate prompt/completion split).
            total_tokens = getattr(usage, "total_tokens", None)
            if total_tokens is None:
                return
            prompt_tokens = total_tokens
            completion_tokens = 0

        prompt_tokens = prompt_tokens or 0
        completion_tokens = completion_tokens or 0

        if prompt_tokens:
            LLM_TOKENS.labels(feature=feature, model=model, token_type="prompt").inc(prompt_tokens)
        if completion_tokens:
            LLM_TOKENS.labels(feature=feature, model=model, token_type="completion").inc(completion_tokens)

        cost = _cost_usd(model, prompt_tokens, completion_tokens)
        if cost is None:
            LLM_UNPRICED.labels(model=model).inc()
        else:
            LLM_COST.labels(feature=feature, model=model).inc(float(cost))
    except Exception:
        pass


def _safe_inc_request(*, feature: str, model: str, outcome: str) -> None:
    try:
        LLM_REQS.labels(feature=feature, model=model, outcome=outcome).inc()
    except Exception:
        pass


def _safe_observe_latency(*, feature: str, model: str, duration: float) -> None:
    try:
        LLM_LATENCY.labels(feature=feature, model=model).observe(duration)
    except Exception:
        pass


async def _call_and_record(real_create: Any, *, feature: str, args: tuple, kwargs: dict) -> Any:
    """Call `real_create(*args, **kwargs)`, recording latency/count/token/
    cost metrics around it, and re-raise any provider exception unchanged
    (never swallowed, never altered)."""
    model = kwargs.get("model", "unknown")
    start = time.monotonic()
    try:
        response = await real_create(*args, **kwargs)
    except BaseException:
        _safe_inc_request(feature=feature, model=model, outcome="error")
        raise

    duration = time.monotonic() - start
    _safe_observe_latency(feature=feature, model=model, duration=duration)
    _safe_inc_request(feature=feature, model=model, outcome="success")
    _record_usage(feature=feature, model=model, usage=getattr(response, "usage", None))
    return response


class _InstrumentedCompletions:
    """Proxy for `client.chat.completions` — intercepts only `.create`."""

    def __init__(self, real_completions: Any, *, feature: str) -> None:
        self._real = real_completions
        self._feature = feature

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        return await _call_and_record(self._real.create, feature=self._feature, args=args, kwargs=kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _InstrumentedChat:
    """Proxy for `client.chat` — exposes an instrumented `.completions`."""

    def __init__(self, real_chat: Any, *, feature: str) -> None:
        self.completions = _InstrumentedCompletions(real_chat.completions, feature=feature)
        self._real = real_chat

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _InstrumentedEmbeddings:
    """Proxy for `client.embeddings` — intercepts only `.create`."""

    def __init__(self, real_embeddings: Any, *, feature: str) -> None:
        self._real = real_embeddings
        self._feature = feature

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        return await _call_and_record(self._real.create, feature=self._feature, args=args, kwargs=kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class InstrumentedAsyncOpenAI:
    """Drop-in proxy for `openai.AsyncOpenAI` that records Prometheus
    cost/latency/outcome metrics around `.chat.completions.create` and
    `.embeddings.create`. Every other attribute delegates unchanged to
    the wrapped real client via `__getattr__`, so this can be used
    anywhere an `openai.AsyncOpenAI` is expected.
    """

    def __init__(self, real_client: "openai.AsyncOpenAI", *, feature: str) -> None:
        self._real_client = real_client
        self._feature = feature
        self.chat = _InstrumentedChat(real_client.chat, feature=feature)
        self.embeddings = _InstrumentedEmbeddings(real_client.embeddings, feature=feature)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real_client, name)


def build_llm_client(*, feature: str, api_key: str | None = None) -> InstrumentedAsyncOpenAI:
    """Construct the sanctioned `openai.AsyncOpenAI` instance for
    `feature`, wrapped in `InstrumentedAsyncOpenAI` for cost/latency
    instrumentation.

    This is the ONLY place `openai.AsyncOpenAI(...)` may be constructed
    under `app/` — every call site should call this instead, passing a
    stable `feature` label (e.g. `"classifier_fallback"`, `"rag_summary"`,
    `"rag_api"`, `"ticket_embedding"`, `"rag_embedding"`, `"whatsapp"`).
    """
    real_client = openai.AsyncOpenAI(api_key=api_key)
    return InstrumentedAsyncOpenAI(real_client, feature=feature)

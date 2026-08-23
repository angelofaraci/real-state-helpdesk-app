"""Unit tests for `app.services.llm_client.InstrumentedAsyncOpenAI` —
the Prometheus cost/latency instrumentation wrapping every OpenAI call
in this codebase.

`build_llm_client` always wraps a real `openai.AsyncOpenAI`, so these
tests reach in and swap the wrapper's `.chat.completions`/`.embeddings`
proxies for fakes, exactly the way the rest of the suite injects a fake
`client` into `classify_with_llm`/`_summarize_resolution`/etc.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from prometheus_client import REGISTRY

from app.services.llm_client import (
    LLM_LATENCY,
    LLM_REQS,
    LLM_UNPRICED,
    build_llm_client,
)


def _sample(name: str, labels: dict[str, str]) -> float | None:
    return REGISTRY.get_sample_value(name, labels)


def _usage(prompt_tokens: int, completion_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)


@pytest.mark.asyncio
async def test_unknown_model_records_zero_cost_and_increments_unpriced() -> None:
    client = build_llm_client(feature="classifier_fallback", api_key="sk-test")
    fake_create = AsyncMock(
        return_value=SimpleNamespace(usage=_usage(10, 5))
    )
    client.chat.completions._real.create = fake_create

    before_unpriced = _sample("llm_unpriced_requests_total", {"model": "totally-unknown-model"}) or 0.0
    before_cost = _sample("llm_cost_usd_total", {"feature": "classifier_fallback", "model": "totally-unknown-model"})

    await client.chat.completions.create(model="totally-unknown-model", messages=[])

    after_unpriced = _sample("llm_unpriced_requests_total", {"model": "totally-unknown-model"})
    after_cost = _sample("llm_cost_usd_total", {"feature": "classifier_fallback", "model": "totally-unknown-model"})

    assert after_unpriced == before_unpriced + 1
    # cost metric must never appear/increment for an unpriced model
    assert after_cost == before_cost


@pytest.mark.asyncio
async def test_missing_usage_records_latency_and_count_but_no_cost() -> None:
    client = build_llm_client(feature="rag_embedding", api_key="sk-test")
    fake_create = AsyncMock(return_value=SimpleNamespace(usage=None))
    client.embeddings._real.create = fake_create

    before_reqs = _sample(
        "llm_requests_total", {"feature": "rag_embedding", "model": "text-embedding-3-small", "outcome": "success"}
    ) or 0.0
    before_latency_count = _sample(
        "llm_request_duration_seconds_count", {"feature": "rag_embedding", "model": "text-embedding-3-small"}
    ) or 0.0
    before_cost = _sample(
        "llm_cost_usd_total", {"feature": "rag_embedding", "model": "text-embedding-3-small"}
    )

    await client.embeddings.create(model="text-embedding-3-small", input=["hello"])

    after_reqs = _sample(
        "llm_requests_total", {"feature": "rag_embedding", "model": "text-embedding-3-small", "outcome": "success"}
    )
    after_latency_count = _sample(
        "llm_request_duration_seconds_count", {"feature": "rag_embedding", "model": "text-embedding-3-small"}
    )
    after_cost = _sample(
        "llm_cost_usd_total", {"feature": "rag_embedding", "model": "text-embedding-3-small"}
    )

    assert after_reqs == before_reqs + 1
    assert after_latency_count == before_latency_count + 1
    # no usage payload means no cost recording at all, priced model or not
    assert after_cost == before_cost


@pytest.mark.asyncio
async def test_provider_exception_records_error_outcome_and_reraises() -> None:
    client = build_llm_client(feature="ticket_embedding", api_key="sk-test")
    boom = RuntimeError("provider exploded")
    fake_create = AsyncMock(side_effect=boom)
    client.chat.completions._real.create = fake_create

    before_errors = _sample(
        "llm_requests_total", {"feature": "ticket_embedding", "model": "gpt-4o-mini", "outcome": "error"}
    ) or 0.0

    with pytest.raises(RuntimeError) as exc_info:
        await client.chat.completions.create(model="gpt-4o-mini", messages=[])

    assert exc_info.value is boom

    after_errors = _sample(
        "llm_requests_total", {"feature": "ticket_embedding", "model": "gpt-4o-mini", "outcome": "error"}
    )
    assert after_errors == before_errors + 1

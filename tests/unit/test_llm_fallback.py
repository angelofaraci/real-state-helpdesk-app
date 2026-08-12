"""Unit tests for `app.services.llm_fallback.classify_with_llm` — the
OpenAI structured-output fallback classifier.

The OpenAI client is always injected, so no real network call happens.
Structured outputs/enum constraints should normally prevent the model
from returning an id outside the given choices, but the fallback must
NOT blindly trust that — a post-hoc membership check against the
caller-supplied `categories`/`urgency_levels` ids is mandatory
(guards against prompt injection / model hallucination).
"""

import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import openai
import pytest

from app.services.classifier import (
    ClassificationTransientError,
    ClassificationUnavailableError,
    TaxonomyOption,
)
from app.services.llm_fallback import classify_with_llm


def _fake_completion(payload: dict) -> MagicMock:
    completion = MagicMock()
    message = MagicMock()
    message.content = json.dumps(payload)
    choice = MagicMock()
    choice.message = message
    completion.choices = [choice]
    return completion


@pytest.mark.asyncio
async def test_llm_fallback_rejects_id_not_in_org_taxonomy() -> None:
    category = TaxonomyOption(id=uuid4(), name="Plumbing", description="Pipes")
    urgency = TaxonomyOption(id=uuid4(), name="High", description="Urgent")
    hallucinated_category_id = str(uuid4())  # not among `categories`

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_fake_completion(
            {
                "category_id": hallucinated_category_id,
                "urgency_id": str(urgency.id),
                "confidence": 0.9,
                "urgency_confidence": 0.9,
            }
        )
    )

    with pytest.raises(ClassificationUnavailableError):
        await classify_with_llm(
            text="My sink is leaking",
            categories=[category],
            urgency_levels=[urgency],
            client=fake_client,
        )


@pytest.mark.asyncio
async def test_llm_fallback_rejects_urgency_id_not_in_org_taxonomy() -> None:
    category = TaxonomyOption(id=uuid4(), name="Plumbing", description="Pipes")
    urgency = TaxonomyOption(id=uuid4(), name="High", description="Urgent")
    hallucinated_urgency_id = str(uuid4())

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_fake_completion(
            {
                "category_id": str(category.id),
                "urgency_id": hallucinated_urgency_id,
                "confidence": 0.9,
                "urgency_confidence": 0.9,
            }
        )
    )

    with pytest.raises(ClassificationUnavailableError):
        await classify_with_llm(
            text="My sink is leaking",
            categories=[category],
            urgency_levels=[urgency],
            client=fake_client,
        )


@pytest.mark.asyncio
async def test_llm_fallback_returns_classification_result_on_valid_response() -> None:
    category = TaxonomyOption(id=uuid4(), name="Plumbing", description="Pipes")
    urgency = TaxonomyOption(id=uuid4(), name="High", description="Urgent")

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_fake_completion(
            {
                "category_id": str(category.id),
                "urgency_id": str(urgency.id),
                "confidence": 0.87,
                "urgency_confidence": 0.81,
            }
        )
    )

    result = await classify_with_llm(
        text="My sink is leaking",
        categories=[category],
        urgency_levels=[urgency],
        client=fake_client,
    )

    assert result.category_id == category.id
    assert result.urgency_id == urgency.id
    assert result.predicted_category == "Plumbing"
    assert result.predicted_urgency == "High"
    assert result.model_used == "llm_fallback"


@pytest.mark.asyncio
async def test_llm_fallback_raises_transient_error_on_rate_limit() -> None:
    category = TaxonomyOption(id=uuid4(), name="Plumbing", description=None)
    urgency = TaxonomyOption(id=uuid4(), name="High", description=None)

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=openai.RateLimitError(
            "rate limited", response=MagicMock(status_code=429), body=None
        )
    )

    with pytest.raises(ClassificationTransientError):
        await classify_with_llm(
            text="My sink is leaking",
            categories=[category],
            urgency_levels=[urgency],
            client=fake_client,
        )


@pytest.mark.asyncio
async def test_llm_fallback_raises_unavailable_error_on_malformed_response() -> None:
    category = TaxonomyOption(id=uuid4(), name="Plumbing", description=None)
    urgency = TaxonomyOption(id=uuid4(), name="High", description=None)

    fake_client = MagicMock()
    completion = MagicMock()
    message = MagicMock()
    message.content = "not json at all"
    choice = MagicMock()
    choice.message = message
    completion.choices = [choice]
    fake_client.chat.completions.create = AsyncMock(return_value=completion)

    with pytest.raises(ClassificationUnavailableError):
        await classify_with_llm(
            text="My sink is leaking",
            categories=[category],
            urgency_levels=[urgency],
            client=fake_client,
        )

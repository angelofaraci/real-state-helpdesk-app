"""Unit tests for `app.services.classifier.classify` — the stage-2 ticket
classification orchestrator.

The sklearn prediction step and the embedding provider are both injected,
so these tests never touch joblib, real `.joblib` model files, or a real
sentence-transformers/OpenAI embedding call. The LLM fallback path itself
is exercised in `tests/unit/test_llm_fallback.py`; here we only assert that
`classify()` routes to it under the documented conditions (by injecting a
fake `llm_fallback` callable).
"""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.classifier import (
    ClassificationResult,
    TaxonomyOption,
    classify,
)


class _FakeEmbedder:
    """Stand-in for `EmbeddingProvider` — returns a fixed embedding."""

    async def embed(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


def _predictor(name: str, confidence: float):
    """Build a fake sklearn-predictor callable: `(embedding) -> (name, confidence)`."""

    def _predict(embedding):
        return name, confidence

    return _predict


@pytest.mark.asyncio
async def test_high_confidence_sklearn_prediction_matching_active_taxonomy() -> None:
    category = TaxonomyOption(id=uuid4(), name="Plumbing", description="Pipes")
    urgency = TaxonomyOption(id=uuid4(), name="High", description="Urgent")

    result = await classify(
        text="My sink is leaking badly",
        categories=[category],
        urgency_levels=[urgency],
        embedder=_FakeEmbedder(),
        predict_category=_predictor("Plumbing", 0.92),
        predict_urgency=_predictor("High", 0.88),
    )

    assert isinstance(result, ClassificationResult)
    assert result.category_id == category.id
    assert result.urgency_id == urgency.id
    assert result.predicted_category == "Plumbing"
    assert result.predicted_urgency == "High"
    assert result.confidence == 0.92
    assert result.urgency_confidence == 0.88
    assert result.model_used == "sklearn_v1"


@pytest.mark.asyncio
async def test_case_insensitive_taxonomy_name_lookup() -> None:
    category = TaxonomyOption(id=uuid4(), name="Plumbing", description=None)
    urgency = TaxonomyOption(id=uuid4(), name="High", description=None)

    result = await classify(
        text="My sink is leaking badly",
        categories=[category],
        urgency_levels=[urgency],
        embedder=_FakeEmbedder(),
        predict_category=_predictor("PLUMBING", 0.95),
        predict_urgency=_predictor("high", 0.9),
    )

    assert result.category_id == category.id
    assert result.urgency_id == urgency.id
    assert result.model_used == "sklearn_v1"


@pytest.mark.asyncio
async def test_low_category_confidence_falls_back_to_llm() -> None:
    category = TaxonomyOption(id=uuid4(), name="Plumbing", description=None)
    urgency = TaxonomyOption(id=uuid4(), name="High", description=None)
    fallback_result = ClassificationResult(
        category_id=category.id,
        urgency_id=urgency.id,
        predicted_category="Plumbing",
        predicted_urgency="High",
        confidence=0.5,
        urgency_confidence=0.7,
        model_used="llm_fallback",
    )
    llm_fallback = AsyncMock(return_value=fallback_result)

    result = await classify(
        text="Something is wrong",
        categories=[category],
        urgency_levels=[urgency],
        embedder=_FakeEmbedder(),
        predict_category=_predictor("Plumbing", 0.4),
        predict_urgency=_predictor("High", 0.9),
        llm_fallback=llm_fallback,
    )

    assert result.model_used == "llm_fallback"
    llm_fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_sklearn_category_not_in_active_taxonomy_falls_back_to_llm_regardless_of_confidence() -> None:
    category = TaxonomyOption(id=uuid4(), name="Plumbing", description=None)
    urgency = TaxonomyOption(id=uuid4(), name="High", description=None)
    fallback_result = ClassificationResult(
        category_id=category.id,
        urgency_id=urgency.id,
        predicted_category="Plumbing",
        predicted_urgency="High",
        confidence=0.5,
        urgency_confidence=0.9,
        model_used="llm_fallback",
    )
    llm_fallback = AsyncMock(return_value=fallback_result)

    result = await classify(
        text="Something is wrong",
        categories=[category],
        urgency_levels=[urgency],
        embedder=_FakeEmbedder(),
        # "Electrical" no longer exists among the active taxonomy (renamed
        # or deactivated) — even with very high confidence, this must not
        # be trusted.
        predict_category=_predictor("Electrical", 0.99),
        predict_urgency=_predictor("High", 0.9),
        llm_fallback=llm_fallback,
    )

    assert result.model_used == "llm_fallback"
    llm_fallback.assert_awaited_once()

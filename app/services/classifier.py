"""Stage-2 ticket classification orchestrator.

`classify()` embeds the ticket text, runs it through the org's locally
trained sklearn classifiers (`category_clf.joblib` / `urgency_clf.joblib`,
loaded from `settings.ml_artifacts_dir`), and only trusts that sklearn
prediction when BOTH of the following hold:

1. the category confidence is at or above `settings.classifier_confidence_threshold`;
2. the predicted category *name* case-insensitively matches one of the
   caller-supplied active taxonomy rows for the org (`categories`).

If either check fails — including the case where the sklearn model
predicts a category name that no longer exists in the org's active
taxonomy (renamed or deactivated since the model was trained) — the
ticket is routed to the OpenAI LLM fallback (`app.services.llm_fallback`)
instead, regardless of how confident the sklearn model was.

The urgency prediction is looked up the same way once a category has been
selected, but does not independently trigger the fallback: only the
category confidence/membership decides whether we trust sklearn at all
(see `app/services/llm_fallback.py` for the corresponding urgency
membership guard on the fallback path).

Sklearn model loading is deliberately guarded against path traversal: an
artifact filename is resolved and checked for containment within the
resolved `ml_artifacts_dir` *before* `joblib.load` is ever called.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Literal, Protocol, Sequence
from uuid import UUID

from app.core.config import get_settings


@dataclass(frozen=True, slots=True)
class TaxonomyOption:
    """A single active category or urgency-level row, as supplied by the
    caller (already scoped/filtered to the organization and `active=True`).
    """

    id: UUID
    name: str
    description: str | None


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    category_id: UUID
    urgency_id: UUID
    predicted_category: str
    predicted_urgency: str
    confidence: float
    urgency_confidence: float
    model_used: Literal["sklearn_v1", "llm_fallback"]


class ClassificationUnavailableError(Exception):
    """Permanent classification failure. The caller should leave the
    ticket's classification pending (e.g. `classification_status="failed"`)
    rather than retry automatically."""


class ClassificationTransientError(Exception):
    """Transient classification failure (network, timeout, rate limit).
    The caller should retry (e.g. via the arq job queue's retry policy)."""


class EmbeddingProvider(Protocol):
    """Implemented by `app.services.embeddings` providers."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


# `(embedding) -> (predicted_name, confidence)`
SklearnPredictFn = Callable[[list[float]], tuple[str, float]]

# `async (*, text, categories, urgency_levels) -> ClassificationResult`
LlmFallbackFn = Callable[..., Awaitable[ClassificationResult]]


def _resolve_artifact_path(artifact_name: str) -> Path:
    """Resolve `artifact_name` relative to `settings.ml_artifacts_dir` and
    verify the resolved path is actually contained within that directory.

    Raises `ClassificationUnavailableError` if the resolved path escapes
    `ml_artifacts_dir` (e.g. via a `..`-containing artifact name) — this
    check happens *before* any file I/O, so `joblib.load` is never called
    on an unresolved/uncontained path.
    """
    settings = get_settings()
    base_dir = Path(settings.ml_artifacts_dir).resolve()
    candidate = (base_dir / artifact_name).resolve()

    if candidate != base_dir and base_dir not in candidate.parents:
        raise ClassificationUnavailableError(
            f"refusing to load ML artifact outside ml_artifacts_dir: {artifact_name!r}"
        )

    return candidate


def _load_joblib_artifact(artifact_name: str):
    """Load a joblib artifact from `settings.ml_artifacts_dir`, guarded
    against path traversal via `_resolve_artifact_path`."""
    import joblib  # lazy import: only needed when an artifact is actually loaded

    path = _resolve_artifact_path(artifact_name)
    return joblib.load(path)


def _default_predict_category(embedding: list[float]) -> tuple[str, float]:
    model = _load_joblib_artifact("category_clf.joblib")
    name, confidence = model.predict(embedding)
    return name, confidence


def _default_predict_urgency(embedding: list[float]) -> tuple[str, float]:
    model = _load_joblib_artifact("urgency_clf.joblib")
    name, confidence = model.predict(embedding)
    return name, confidence


def _find_by_name(options: Sequence[TaxonomyOption], name: str) -> TaxonomyOption | None:
    lowered = name.lower()
    for option in options:
        if option.name.lower() == lowered:
            return option
    return None


async def classify(
    *,
    text: str,
    categories: Sequence[TaxonomyOption],
    urgency_levels: Sequence[TaxonomyOption],
    embedder: EmbeddingProvider,
    predict_category: SklearnPredictFn | None = None,
    predict_urgency: SklearnPredictFn | None = None,
    llm_fallback: LlmFallbackFn | None = None,
) -> ClassificationResult:
    """Classify `text` into one of `categories` and one of `urgency_levels`.

    `predict_category`/`predict_urgency` and `llm_fallback` are injectable
    for tests; in production they default to the real sklearn artifacts
    (see `_default_predict_category`/`_default_predict_urgency`) and
    `app.services.llm_fallback.classify_with_llm` respectively.
    """
    settings = get_settings()
    predict_category = predict_category or _default_predict_category
    predict_urgency = predict_urgency or _default_predict_urgency

    [embedding] = await embedder.embed([text])

    category_name, category_confidence = predict_category(embedding)
    urgency_name, urgency_confidence = predict_urgency(embedding)

    category_match = _find_by_name(categories, category_name)
    urgency_match = _find_by_name(urgency_levels, urgency_name)

    trust_sklearn = (
        category_confidence >= settings.classifier_confidence_threshold
        and category_match is not None
        and urgency_match is not None
    )

    if trust_sklearn:
        assert category_match is not None
        assert urgency_match is not None
        return ClassificationResult(
            category_id=category_match.id,
            urgency_id=urgency_match.id,
            predicted_category=category_match.name,
            predicted_urgency=urgency_match.name,
            confidence=category_confidence,
            urgency_confidence=urgency_confidence,
            model_used="sklearn_v1",
        )

    if llm_fallback is None:
        from app.services.llm_fallback import classify_with_llm as llm_fallback

    return await llm_fallback(
        text=text,
        categories=categories,
        urgency_levels=urgency_levels,
    )

"""OpenAI LLM fallback classifier — used by `app.services.classifier.classify`
when the local sklearn model's prediction is not trusted (low confidence,
or a predicted category name no longer present in the org's active
taxonomy).

The model is constrained to choose only among the org's real active
category/urgency ids via a JSON-schema `enum` (structured outputs). Even
so, the returned ids are re-validated against `categories`/`urgency_levels`
after parsing (`_require_membership`) — a defense-in-depth guard against
prompt injection or model hallucination, since structured-output
constraints are a strong guarantee from the API but not one this code
should blindly trust.
"""

from __future__ import annotations

import json
from typing import Sequence
from uuid import UUID

import openai

from app.core.config import get_settings
from app.services.classifier import (
    ClassificationResult,
    ClassificationTransientError,
    ClassificationUnavailableError,
    TaxonomyOption,
)

# OpenAI SDK exceptions that represent a transient/retryable failure
# (network hiccup, timeout, rate limit, or a 5xx on OpenAI's side).
_TRANSIENT_OPENAI_ERRORS = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
)


def _build_response_schema(
    categories: Sequence[TaxonomyOption], urgency_levels: Sequence[TaxonomyOption]
) -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "ticket_classification",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "category_id": {
                        "type": "string",
                        "enum": [str(option.id) for option in categories],
                    },
                    "urgency_id": {
                        "type": "string",
                        "enum": [str(option.id) for option in urgency_levels],
                    },
                    "confidence": {"type": "number"},
                    "urgency_confidence": {"type": "number"},
                },
                "required": ["category_id", "urgency_id", "confidence", "urgency_confidence"],
                "additionalProperties": False,
            },
        },
    }


def _build_prompt(
    text: str, categories: Sequence[TaxonomyOption], urgency_levels: Sequence[TaxonomyOption]
) -> str:
    category_lines = "\n".join(
        f"- id={option.id} name={option.name!r} description={option.description!r}"
        for option in categories
    )
    urgency_lines = "\n".join(
        f"- id={option.id} name={option.name!r} description={option.description!r}"
        for option in urgency_levels
    )
    return (
        "Classify the following support ticket into exactly one of the given "
        "categories and exactly one of the given urgency levels. Only choose "
        "from the ids listed below; do not invent new ids.\n\n"
        f"Ticket text:\n{text}\n\n"
        f"Categories:\n{category_lines}\n\n"
        f"Urgency levels:\n{urgency_lines}\n"
    )


def _find_by_id(options: Sequence[TaxonomyOption], id_: UUID) -> TaxonomyOption | None:
    for option in options:
        if option.id == id_:
            return option
    return None


def _require_membership(
    *,
    category_id: UUID,
    urgency_id: UUID,
    categories: Sequence[TaxonomyOption],
    urgency_levels: Sequence[TaxonomyOption],
) -> tuple[TaxonomyOption, TaxonomyOption]:
    """Re-validate that `category_id`/`urgency_id` are actually among the
    ids we offered the model, regardless of the (supposedly enum-
    constrained) structured-output response. Never trust the model's
    returned id at face value."""
    category = _find_by_id(categories, category_id)
    urgency = _find_by_id(urgency_levels, urgency_id)
    if category is None or urgency is None:
        raise ClassificationUnavailableError(
            "LLM fallback returned an id outside the org's active taxonomy"
        )
    return category, urgency


async def classify_with_llm(
    *,
    text: str,
    categories: Sequence[TaxonomyOption],
    urgency_levels: Sequence[TaxonomyOption],
    client=None,
) -> ClassificationResult:
    """Classify `text` via the OpenAI chat-completions structured-output
    API, constrained to the org's active `categories`/`urgency_levels`.

    `client` must be (or behave like) an `openai.AsyncOpenAI` instance; it
    is injected so tests never make a real network call. When omitted, a
    real client is constructed from `settings.openai_api_key`.

    Raises `ClassificationTransientError` on a transient OpenAI SDK error
    (network/timeout/rate-limit/5xx) and `ClassificationUnavailableError`
    on anything else: parse failure, a returned id outside the org's
    taxonomy, or a missing API key.
    """
    settings = get_settings()

    if client is None:
        if not settings.openai_api_key:
            raise ClassificationUnavailableError(
                "no OpenAI API key configured for the LLM fallback classifier"
            )
        client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

    try:
        completion = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {
                    "role": "user",
                    "content": _build_prompt(text, categories, urgency_levels),
                }
            ],
            response_format=_build_response_schema(categories, urgency_levels),
        )
    except _TRANSIENT_OPENAI_ERRORS as exc:
        raise ClassificationTransientError(str(exc)) from exc
    except openai.OpenAIError as exc:
        raise ClassificationUnavailableError(str(exc)) from exc

    try:
        raw_content = completion.choices[0].message.content
        payload = json.loads(raw_content)
        category_id = UUID(payload["category_id"])
        urgency_id = UUID(payload["urgency_id"])
        confidence = float(payload["confidence"])
        urgency_confidence = float(payload["urgency_confidence"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError, IndexError, AttributeError) as exc:
        raise ClassificationUnavailableError(
            f"failed to parse LLM fallback response: {exc}"
        ) from exc

    category, urgency = _require_membership(
        category_id=category_id,
        urgency_id=urgency_id,
        categories=categories,
        urgency_levels=urgency_levels,
    )

    return ClassificationResult(
        category_id=category.id,
        urgency_id=urgency.id,
        predicted_category=category.name,
        predicted_urgency=urgency.name,
        confidence=confidence,
        urgency_confidence=urgency_confidence,
        model_used="llm_fallback",
    )

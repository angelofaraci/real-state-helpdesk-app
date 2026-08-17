"""Pydantic response schema for `GET /tickets/{ticket_id}/suggested-response`
(Stage 3, PR7).

On success, `suggestion` carries the grounded AI-drafted reply
(`app.services.rag.SuggestedResponse.message`/`.top_similarity`).
`top_similarity` is `None` when the response is a still-fresh cache hit
(see `SuggestedResponse`'s own docstring) — the caller already saw that
score when the suggestion was first generated.

On an empty-retrieval `None` result from `app.services.rag.suggest_response`
(design's resolved Q4), `suggestion` is `null` and `reason` explains why —
this is a 200, not an error: the caller (a human agent) can simply reply
without an AI-drafted starting point.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel


class SuggestionOut(BaseModel):
    message_id: UUID
    content: str
    top_similarity: float | None

    model_config = {"from_attributes": True}


class SuggestedResponseOut(BaseModel):
    suggestion: SuggestionOut | None
    reason: str | None = None

"""`TicketEmbeddingRepository` — direct-scoped (`TicketEmbedding` owns its
own `organization_id` column, so `scope_path` stays empty).

Same `search()` shape as `KnowledgeChunkRepository` (cosine distance,
scoped, ordered by distance ascending for index-friendliness, `limit *
overfetch` headroom), plus `upsert`: a ticket has at most one embedding row
(`ix_ticket_embeddings_ticket_id_unique`), so writing a new summary/
embedding for a ticket that's already been embedded needs `INSERT ... ON
CONFLICT (ticket_id) DO UPDATE` rather than a fetch-then-branch round trip
— same pattern as `ClassificationRepository.upsert`.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.ticket_embedding import TicketEmbedding
from app.repositories.base import ScopedRepository


class TicketEmbeddingRepository(ScopedRepository[TicketEmbedding]):
    model = TicketEmbedding

    def search(
        self,
        embedding: Sequence[float],
        *,
        limit: int,
        min_similarity: float,
        overfetch: int = 4,
    ) -> Select:
        """A scoped `SELECT` of the `limit * overfetch` ticket embeddings
        nearest `embedding` by cosine distance, restricted to rows whose
        cosine similarity is at least `min_similarity`. See
        `KnowledgeChunkRepository.search` for the distance/similarity
        translation and ordering rationale — identical here."""
        distance = TicketEmbedding.embedding.cosine_distance(embedding)
        return (
            select(TicketEmbedding, (1 - distance).label("similarity"))
            .where(self.scope_clause())
            .where(distance <= 1 - min_similarity)
            .order_by(distance)
            .limit(limit * overfetch)
        )

    async def upsert(
        self,
        ticket_id: UUID,
        organization_id: UUID,
        summary: str,
        embedding: Sequence[float],
        *,
        model_used: str | None = None,
    ) -> None:
        """Insert a new `ticket_embeddings` row for `ticket_id`, or update
        the existing one in place (`ticket_id` is unique) — the shape a
        re-embed call needs without a fetch-then-branch round trip."""
        stmt = pg_insert(TicketEmbedding).values(
            ticket_id=ticket_id,
            organization_id=organization_id,
            summary=summary,
            embedding=list(embedding),
            model_used=model_used,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["ticket_id"],
            set_={
                "summary": stmt.excluded.summary,
                "embedding": stmt.excluded.embedding,
                "model_used": stmt.excluded.model_used,
            },
        )
        await self._session.execute(stmt)

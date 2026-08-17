"""`KnowledgeChunkRepository` — direct-scoped (`KnowledgeChunk` owns its own
`organization_id` column — D4's denormalized copy off `knowledge_base`, so
a similarity search never needs to join back to `knowledge_base` just to
scope itself — so `scope_path` stays empty).

`search()` is the vector-similarity query the retrieval pipeline (a later
PR in this change) will call: cosine distance against a query embedding,
scoped to the current organization, ordered by distance ascending (closest
first) so the HNSW index on `embedding` can actually be used for the scan —
ordering by the computed `similarity` label instead would force a full
scan since Postgres can't push a descending sort on a derived expression
down to the index the same way.

`limit * overfetch` gives the caller headroom to post-filter ANN results
(approximate nearest-neighbor search can return slightly different
neighbors than an exact scan) before truncating to the caller's requested
`limit`.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import Select, delete, select

from app.models.knowledge_chunk import KnowledgeChunk
from app.repositories.base import ScopedRepository


class KnowledgeChunkRepository(ScopedRepository[KnowledgeChunk]):
    model = KnowledgeChunk

    def search(
        self,
        embedding: Sequence[float],
        *,
        limit: int,
        min_similarity: float,
        overfetch: int = 4,
    ) -> Select:
        """A scoped `SELECT` of the `limit * overfetch` chunks nearest
        `embedding` by cosine distance, restricted to chunks whose cosine
        similarity is at least `min_similarity`.

        `similarity = 1 - distance`, so `similarity >= min_similarity` is
        equivalent to `distance <= 1 - min_similarity` — filtering on the
        distance expression (rather than the `similarity` label) keeps the
        `WHERE` clause index-friendly, same reasoning as the `ORDER BY`
        below.
        """
        distance = KnowledgeChunk.embedding.cosine_distance(embedding)
        return (
            select(KnowledgeChunk, (1 - distance).label("similarity"))
            .where(self.scope_clause())
            .where(distance <= 1 - min_similarity)
            .order_by(distance)
            .limit(limit * overfetch)
        )

    async def delete_for_knowledge_base(self, knowledge_base_id: UUID) -> None:
        """Bulk-delete every `knowledge_chunks` row for `knowledge_base_id`
        (scoped), without loading them into the session first — used by
        `app.services.knowledge_base_service.delete_knowledge_base`'s
        explicit app-level cascade ahead of the parent `knowledge_base`
        row's own delete."""
        stmt = delete(KnowledgeChunk).where(
            self.scope_clause(), KnowledgeChunk.knowledge_base_id == knowledge_base_id
        )
        await self._session.execute(stmt)

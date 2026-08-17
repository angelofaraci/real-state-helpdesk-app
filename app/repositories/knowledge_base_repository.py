"""`KnowledgeBaseRepository` — direct-scoped (`KnowledgeBase` owns its own
`organization_id` column, so `scope_path` stays empty).

Plain `ScopedRepository` CRUD — `select`/`get_or_404`/`add`/`update`/
`delete` already cover everything the ingestion pipeline (later PRs in
this change) needs to create, list, and update knowledge base articles. No
`search()` here: retrieval happens over `knowledge_chunks`
(`KnowledgeChunkRepository.search`), not over `knowledge_base` rows
directly.
"""

from __future__ import annotations

from app.models.knowledge_base import KnowledgeBase
from app.repositories.base import ScopedRepository


class KnowledgeBaseRepository(ScopedRepository[KnowledgeBase]):
    model = KnowledgeBase

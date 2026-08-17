"""Unit tests for `KnowledgeChunkRepository`.

`KnowledgeChunk` owns its own `organization_id` column (D4's denormalized
copy off `knowledge_base`), so this exercises the direct-scoping branch of
`ScopedRepository`.

No live Postgres is available in this sandbox, so `search()` is verified by
compiling the statement and inspecting the resulting SQL/structure, same
approach `test_classification_repository.py` uses for its `upsert`.
"""

import uuid
from unittest.mock import AsyncMock

from app.core.scope import OrgScope
from app.models.enums import UserRole
from app.repositories.knowledge_chunk_repository import KnowledgeChunkRepository


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_is_direct_scoped_with_no_scope_path() -> None:
    assert KnowledgeChunkRepository.scope_path == ()


def test_search_scopes_by_organization_id() -> None:
    scope = _scope()
    repo = KnowledgeChunkRepository(AsyncMock(), scope)

    sql = _compiled(repo.search([0.1] * 768, limit=10, min_similarity=0.7))

    assert "knowledge_chunks" in sql
    assert "knowledge_chunks.organization_id" in sql
    assert scope.organization_id.hex in sql


def test_search_filters_by_cosine_distance_threshold() -> None:
    repo = KnowledgeChunkRepository(AsyncMock(), _scope())

    sql = _compiled(repo.search([0.1] * 768, limit=10, min_similarity=0.7))

    assert "<=>" in sql
    # cos_sim >= 0.7  <=>  distance <= 1 - 0.7 == 0.3
    assert "<= 0.3" in sql


def test_search_orders_by_the_distance_expression_not_the_similarity_label() -> None:
    repo = KnowledgeChunkRepository(AsyncMock(), _scope())

    sql = _compiled(repo.search([0.1] * 768, limit=10, min_similarity=0.7))

    assert "ORDER BY knowledge_chunks.embedding <=>" in sql
    # Ordering by the computed `similarity` label (descending) would forfeit
    # the HNSW index scan — must never appear.
    assert "ORDER BY similarity" not in sql
    assert "DESC" not in sql


def test_search_limits_to_limit_times_overfetch() -> None:
    repo = KnowledgeChunkRepository(AsyncMock(), _scope())

    sql = _compiled(repo.search([0.1] * 768, limit=10, min_similarity=0.7, overfetch=4))

    assert "LIMIT 40" in sql


def test_search_overfetch_defaults_to_four() -> None:
    repo = KnowledgeChunkRepository(AsyncMock(), _scope())

    sql = _compiled(repo.search([0.1] * 768, limit=5, min_similarity=0.7))

    assert "LIMIT 20" in sql

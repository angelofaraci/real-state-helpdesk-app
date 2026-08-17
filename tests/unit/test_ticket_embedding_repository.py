"""Unit tests for `TicketEmbeddingRepository`.

`TicketEmbedding` owns its own `organization_id` column, so this exercises
the direct-scoping branch of `ScopedRepository`. Same `search()` shape as
`KnowledgeChunkRepository` (see that module's tests for the
distance/similarity/index reasoning), plus `upsert`.

No live Postgres is available in this sandbox: `upsert` idempotency is
verified by asserting the statement compiles to `INSERT ... ON CONFLICT
(ticket_id) DO UPDATE` (a real DB would then enforce the unique index on
`ticket_id` at the row level — the SQL shape is what guarantees a second
call updates in place rather than raising or duplicating), same approach
`test_classification_repository.py` uses for its own `upsert`.
"""

import uuid
from unittest.mock import AsyncMock

from sqlalchemy.dialects import postgresql

from app.core.scope import OrgScope
from app.models.enums import UserRole
from app.repositories.ticket_embedding_repository import TicketEmbeddingRepository


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _pg_compiled(stmt) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_is_direct_scoped_with_no_scope_path() -> None:
    assert TicketEmbeddingRepository.scope_path == ()


# ---------------------------------------------------------------------------
# search — identical shape/rationale to KnowledgeChunkRepository.search
# ---------------------------------------------------------------------------


def test_search_scopes_by_organization_id() -> None:
    scope = _scope()
    repo = TicketEmbeddingRepository(AsyncMock(), scope)

    sql = _compiled(repo.search([0.1] * 768, limit=10, min_similarity=0.7))

    assert "ticket_embeddings" in sql
    assert "ticket_embeddings.organization_id" in sql
    assert scope.organization_id.hex in sql


def test_search_orders_by_the_distance_expression_not_the_similarity_label() -> None:
    repo = TicketEmbeddingRepository(AsyncMock(), _scope())

    sql = _compiled(repo.search([0.1] * 768, limit=10, min_similarity=0.7))

    assert "ORDER BY ticket_embeddings.embedding <=>" in sql
    assert "ORDER BY similarity" not in sql
    assert "DESC" not in sql


def test_search_limits_to_limit_times_overfetch() -> None:
    repo = TicketEmbeddingRepository(AsyncMock(), _scope())

    sql = _compiled(repo.search([0.1] * 768, limit=10, min_similarity=0.7, overfetch=4))

    assert "LIMIT 40" in sql


# ---------------------------------------------------------------------------
# upsert: INSERT ... ON CONFLICT (ticket_id) DO UPDATE
# ---------------------------------------------------------------------------


async def test_upsert_compiles_to_insert_on_conflict_do_update() -> None:
    session = AsyncMock()
    repo = TicketEmbeddingRepository(session, _scope())
    ticket_id = uuid.uuid4()
    org_id = uuid.uuid4()

    await repo.upsert(
        ticket_id,
        org_id,
        "Tenant reports a leaking faucet in the kitchen.",
        [0.1] * 768,
        model_used="text-embedding-3-small",
    )

    session.execute.assert_awaited_once()
    (stmt,), _kwargs = session.execute.call_args
    sql = _pg_compiled(stmt)

    assert "INSERT INTO ticket_embeddings" in sql
    assert "ON CONFLICT (ticket_id) DO UPDATE" in sql
    assert str(ticket_id) in sql
    assert "'text-embedding-3-small'" in sql


async def test_upsert_called_twice_for_the_same_ticket_issues_two_on_conflict_statements() -> None:
    """Idempotency check: a second `upsert` for the same `ticket_id` must
    compile to the same `ON CONFLICT (ticket_id) DO UPDATE` shape (which the
    DB's unique index on `ticket_id` then turns into an update-in-place,
    never a duplicate row), not a plain `INSERT` that would violate the
    unique index or duplicate data."""
    session = AsyncMock()
    repo = TicketEmbeddingRepository(session, _scope())
    ticket_id = uuid.uuid4()
    org_id = uuid.uuid4()

    await repo.upsert(ticket_id, org_id, "First summary.", [0.1] * 768)
    await repo.upsert(ticket_id, org_id, "Updated summary.", [0.2] * 768)

    assert session.execute.await_count == 2
    for call in session.execute.await_args_list:
        (stmt,), _kwargs = call
        sql = _pg_compiled(stmt)
        assert "INSERT INTO ticket_embeddings" in sql
        assert "ON CONFLICT (ticket_id) DO UPDATE" in sql
        assert str(ticket_id) in sql

    first_sql = _pg_compiled(session.execute.await_args_list[0].args[0])
    second_sql = _pg_compiled(session.execute.await_args_list[1].args[0])
    assert "'First summary.'" in first_sql
    assert "'Updated summary.'" in second_sql

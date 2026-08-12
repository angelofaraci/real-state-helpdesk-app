"""Unit tests for `ClassificationRepository`.

`Classification` has no `organization_id` column of its own — scoping goes
through `tickets.organization_id`, so this exercises the join-path branch
of `ScopedRepository`, same as `ContractRepository` (through `properties`).

Beyond the generic scoping machinery, `ClassificationRepository` adds two
worker-facing operations that don't fit the generic CRUD shape of
`ScopedRepository`:

- `upsert`: the classification worker writes a prediction for a ticket that
  may or may not already have a `classifications` row (a retry/reclassify
  case), so this needs `INSERT ... ON CONFLICT (ticket_id) DO UPDATE`
  rather than a plain `INSERT` or a fetch-then-update round trip.
- `mark_human_corrected`: flips `human_corrected` to `true` when an agent
  overrides the AI's prediction.

No live Postgres is available in this sandbox, so both are verified by
inspecting the compiled SQL passed to a mocked `AsyncSession.execute`.
"""

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.core.scope import OrgScope
from app.models.enums import UserRole
from app.models.ticket import Ticket
from app.repositories.classification_repository import ClassificationRepository


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


# ---------------------------------------------------------------------------
# scope_path: join through Ticket
# ---------------------------------------------------------------------------


def test_scope_path_joins_through_ticket() -> None:
    assert ClassificationRepository.scope_path == ((Ticket, "ticket_id"),)


def test_scope_clause_uses_exists_through_tickets() -> None:
    scope = _scope()
    repo = ClassificationRepository(AsyncMock(), scope)

    sql = _compiled(repo.select())

    assert "classifications" in sql
    assert "EXISTS" in sql
    assert "tickets" in sql
    assert "tickets.organization_id" in sql
    assert scope.organization_id.hex in sql
    assert "classifications.organization_id" not in sql
    assert (
        "tickets.id = classifications.ticket_id" in sql
        or "classifications.ticket_id = tickets.id" in sql
    )


# ---------------------------------------------------------------------------
# upsert: INSERT ... ON CONFLICT (ticket_id) DO UPDATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_compiles_to_insert_on_conflict_do_update() -> None:
    session = AsyncMock()
    repo = ClassificationRepository(session, _scope())
    ticket_id = uuid.uuid4()

    await repo.upsert(
        ticket_id,
        predicted_category="plumbing",
        confidence=0.92,
        predicted_urgency="high",
        urgency_confidence=0.81,
        model_used="sklearn_v1",
    )

    session.execute.assert_awaited_once()
    (stmt,), _kwargs = session.execute.call_args
    sql = _pg_compiled(stmt)

    assert "INSERT INTO classifications" in sql
    assert "ON CONFLICT (ticket_id) DO UPDATE" in sql
    assert str(ticket_id) in sql
    assert "'plumbing'" in sql
    assert "'sklearn_v1'" in sql


@pytest.mark.asyncio
async def test_upsert_defaults_optional_fields_to_none() -> None:
    session = AsyncMock()
    repo = ClassificationRepository(session, _scope())
    ticket_id = uuid.uuid4()

    await repo.upsert(ticket_id)

    session.execute.assert_awaited_once()
    (stmt,), _kwargs = session.execute.call_args
    sql = _pg_compiled(stmt)

    assert "INSERT INTO classifications" in sql
    assert str(ticket_id) in sql


# ---------------------------------------------------------------------------
# mark_human_corrected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_human_corrected_updates_the_row_for_the_ticket() -> None:
    session = AsyncMock()
    repo = ClassificationRepository(session, _scope())
    ticket_id = uuid.uuid4()

    await repo.mark_human_corrected(ticket_id)

    session.execute.assert_awaited_once()
    (stmt,), _kwargs = session.execute.call_args
    sql = _compiled(stmt)

    assert "UPDATE classifications" in sql
    assert "human_corrected=true" in sql or "human_corrected = true" in sql
    assert "classifications.ticket_id" in sql
    assert ticket_id.hex in sql

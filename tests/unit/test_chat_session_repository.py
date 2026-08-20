"""Unit tests for `ChatSessionRepository` — direct-scoped (`ChatSession`
owns its own `organization_id` column, so `scope_path` stays empty), same
pattern as `TicketRepository`/`PropertyRepository`.

No live Postgres is available in this sandbox, so the scope clause shape
is verified by inspecting the compiled SQL of the returned `Select`.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from app.core.scope import OrgScope
from app.models.enums import UserRole
from app.repositories.chat_session_repository import ChatSessionRepository


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _compiled(clause) -> str:
    return str(clause.compile(compile_kwargs={"literal_binds": True}))


def test_scope_clause_is_a_direct_organization_id_predicate() -> None:
    scope = _scope()
    repo = ChatSessionRepository(AsyncMock(), scope)

    sql = _compiled(repo.scope_clause())

    assert "chat_sessions.organization_id" in sql
    assert scope.organization_id.hex in sql
    # Direct scoping never produces an EXISTS()-through-a-join predicate,
    # unlike join-scoped repositories (e.g. ContractRepository).
    assert "EXISTS" not in sql


def test_select_is_scoped_to_the_chat_sessions_table() -> None:
    scope = _scope()
    repo = ChatSessionRepository(AsyncMock(), scope)

    sql = _compiled(repo.select())

    assert "chat_sessions" in sql
    assert "chat_sessions.organization_id" in sql

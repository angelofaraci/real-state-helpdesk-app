"""Unit tests for `ChatMessageRepository` — join-scoped through
`ChatSession` (`ChatMessage` has no `organization_id` column of its own;
scoping goes through `chat_sessions.organization_id`), same join-path
pattern as `ContractRepository` (join through `properties.organization_id`).

No live Postgres is available in this sandbox, so the join is verified by
inspecting the compiled SQL of the returned `Select`.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from app.core.scope import OrgScope
from app.models.enums import UserRole
from app.repositories.chat_message_repository import ChatMessageRepository


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_scope_clause_uses_exists_through_chat_sessions() -> None:
    scope = _scope()
    repo = ChatMessageRepository(AsyncMock(), scope)

    sql = _compiled(repo.select())

    assert "chat_messages" in sql
    assert "EXISTS" in sql
    assert "chat_sessions" in sql
    assert "chat_sessions.organization_id" in sql
    assert scope.organization_id.hex in sql


def test_list_for_session_orders_chronologically_with_id_tiebreak() -> None:
    scope = _scope()
    repo = ChatMessageRepository(AsyncMock(), scope)
    chat_session_id = uuid.uuid4()

    sql = _compiled(repo.list_for_session(chat_session_id))

    assert "chat_messages.chat_session_id" in sql
    assert chat_session_id.hex in sql
    order_by_index = sql.index("ORDER BY")
    order_clause = sql[order_by_index:]
    assert "chat_messages.created_at" in order_clause
    assert "chat_messages.id" in order_clause
    # created_at must be ordered before id (primary sort key, id as
    # tiebreak for same-timestamp inserts).
    assert order_clause.index("chat_messages.created_at") < order_clause.index(
        "chat_messages.id"
    )

"""Unit tests for `app.services.knowledge_base_service`.

`AsyncSession` is mocked, following the pattern established by
`tests/unit/test_ticket_service_update.py` / `test_category_service.py`.
The delete tests are the RED->GREEN evidence for the hard-delete cascade
(task 3.6/3.7): deleting a `knowledge_base` row must also remove its
`knowledge_chunks` rows, proven here via the repository call (a bulk
`DELETE FROM knowledge_chunks WHERE knowledge_base_id = ...` statement),
not by trusting the `ondelete="CASCADE"` FK alone — no live Postgres is
reachable in this sandbox to exercise that FK.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.scope import OrgScope
from app.models.enums import UserRole
from app.models.knowledge_base import KnowledgeBase
from app.services import knowledge_base_service


def _scope(organization_id) -> OrgScope:
    return OrgScope(organization_id=organization_id, user_id=uuid4(), role=UserRole.ADMIN)


def _knowledge_base(*, organization_id, **overrides) -> KnowledgeBase:
    defaults = dict(
        id=uuid4(),
        organization_id=organization_id,
        title="Pet policy",
        content="Pets are allowed with a deposit.",
        source_type=None,
        status="ready",
        embedding_error=None,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return KnowledgeBase(**defaults)


def _execute_result(*, scalar: object = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    return result


@pytest.mark.asyncio
async def test_delete_knowledge_base_deletes_its_chunks_before_the_row() -> None:
    organization_id = uuid4()
    scope = _scope(organization_id)
    kb = _knowledge_base(organization_id=organization_id)

    session = AsyncMock()
    session.execute.side_effect = [
        _execute_result(scalar=kb),  # KnowledgeBaseRepository.get_or_404
        _execute_result(),  # KnowledgeChunkRepository.delete_for_knowledge_base bulk DELETE
    ]

    await knowledge_base_service.delete_knowledge_base(
        session, scope=scope, knowledge_base_id=kb.id
    )

    # Second execute() call is the bulk chunk delete — assert it's a real
    # `DELETE FROM knowledge_chunks ... WHERE knowledge_base_id = <kb.id>`
    # statement, not a no-op.
    (stmt,), _ = session.execute.call_args_list[1]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "DELETE FROM knowledge_chunks" in sql
    assert str(kb.id).replace("-", "") in sql.replace("-", "")

    # The parent row is deleted through the ORM session AFTER the chunk
    # bulk-delete, and the transaction is flushed.
    session.delete.assert_awaited_once_with(kb)
    session.flush.assert_awaited_once()

    delete_call_index = session.execute.call_args_list.index(
        session.execute.call_args_list[1]
    )
    assert delete_call_index == 1  # chunks deleted (2nd execute) before kb ORM delete


@pytest.mark.asyncio
async def test_delete_knowledge_base_missing_row_raises_not_found_and_skips_chunk_delete() -> None:
    from app.core.exceptions import NotFoundError

    organization_id = uuid4()
    scope = _scope(organization_id)
    session = AsyncMock()
    session.execute.side_effect = [_execute_result(scalar=None)]

    with pytest.raises(NotFoundError):
        await knowledge_base_service.delete_knowledge_base(
            session, scope=scope, knowledge_base_id=uuid4()
        )

    session.delete.assert_not_awaited()
    assert session.execute.await_count == 1

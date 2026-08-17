"""Unit tests for `KnowledgeBaseRepository`.

`KnowledgeBase` owns its own `organization_id` column, so this exercises
the direct-scoping branch of `ScopedRepository` — plain CRUD, no
`search()` (retrieval happens over `knowledge_chunks`, not
`knowledge_base` rows).
"""

import uuid
from unittest.mock import AsyncMock

from app.core.scope import OrgScope
from app.models.enums import UserRole
from app.models.knowledge_base import KnowledgeBase
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_is_direct_scoped_with_no_scope_path() -> None:
    assert KnowledgeBaseRepository.scope_path == ()


def test_select_scopes_by_organization_id() -> None:
    scope = _scope()
    repo = KnowledgeBaseRepository(AsyncMock(), scope)

    sql = _compiled(repo.select())

    assert "knowledge_base" in sql
    assert "knowledge_base.organization_id" in sql
    assert scope.organization_id.hex in sql


def test_add_forces_current_scope_organization_id() -> None:
    scope = _scope()
    repo = KnowledgeBaseRepository(AsyncMock(), scope)

    instance = repo.add(
        title="Water heater troubleshooting",
        content="Turn off the breaker before inspecting.",
        organization_id=uuid.uuid4(),  # deliberately foreign — must be discarded
    )

    assert isinstance(instance, KnowledgeBase)
    assert instance.organization_id == scope.organization_id

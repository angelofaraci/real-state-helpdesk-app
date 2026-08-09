"""Unit tests for `UserRepository`, the first production `ScopedRepository`
subclass. `get_or_404`/`add`/`update`/`delete` are already covered generically
by `tests/unit/test_scoped_repository.py`; these tests cover only what
`UserRepository` adds on top: the filtered `list()` helper.
"""

import uuid

from app.core.scope import OrgScope
from app.models.enums import UserRole, UserStatus
from app.repositories.user_repository import UserRepository


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_list_scopes_to_the_organization_with_no_filters() -> None:
    scope = _scope()
    repo = UserRepository(None, scope)

    sql = _compiled(repo.list())
    where_clause = sql.split("WHERE", 1)[1]

    assert "users" in sql
    assert scope.organization_id.hex in sql
    assert "users.role" not in where_clause
    assert "users.status" not in where_clause


def test_list_filters_by_role() -> None:
    scope = _scope()
    repo = UserRepository(None, scope)

    sql = _compiled(repo.list(role=UserRole.AGENT))

    assert "role" in sql
    assert "agent" in sql


def test_list_filters_by_status() -> None:
    scope = _scope()
    repo = UserRepository(None, scope)

    sql = _compiled(repo.list(status=UserStatus.DISABLED))

    assert "status" in sql
    assert "disabled" in sql

"""Unit tests for `TicketRepository` — the role-fused visibility repository
for `Ticket`. This is the highest-value test file in Work Unit 7a: it proves
`select()` produces the correct role-scoped SQL for all 4 roles (agent/admin
see everything in-org; tenant sees own-reported OR own-contract tickets;
owner sees tickets on properties they own), and that `get_or_404` therefore
404s uniformly for both cross-org AND same-org-but-role-invisible tickets —
the same code path, by construction, since visibility is fused into
`select()` itself rather than checked after the fact.

No live Postgres is available in this sandbox, so the visibility clause
shape is verified by inspecting the compiled SQL of the returned `Select`.
Session-boundary behavior (`get_or_404`) is verified against a mocked
`AsyncSession`.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.enums import ClassificationStatus, TicketStatus, UserRole
from app.repositories.ticket_repository import TicketRepository


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _session_returning(scalar_result) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    session.execute.return_value = execute_result
    return session


# ---------------------------------------------------------------------------
# select(): compiled-SQL shape per role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.AGENT, UserRole.ADMIN])
def test_select_for_staff_roles_is_org_scoped_only(role) -> None:
    scope = _scope(role=role)
    repo = TicketRepository(AsyncMock(), scope)

    sql = _compiled(repo.select())

    assert "tickets" in sql
    assert "tickets.organization_id" in sql
    assert scope.organization_id.hex in sql
    # No extra visibility narrowing for staff: no reference to the acting
    # user's own id or to contracts/properties EXISTS subqueries.
    assert "contracts" not in sql
    assert "properties" not in sql
    assert scope.user_id.hex not in sql


def test_select_for_tenant_role_is_own_ticket_or_own_contract() -> None:
    scope = _scope(role=UserRole.TENANT)
    repo = TicketRepository(AsyncMock(), scope)

    sql = _compiled(repo.select())

    assert "tickets" in sql
    assert scope.organization_id.hex in sql
    # user_id == scope.user_id branch
    assert "tickets.user_id" in sql
    assert scope.user_id.hex in sql
    # OR EXISTS(... contracts ... tenant_id == scope.user_id) branch
    assert "EXISTS" in sql
    assert "contracts" in sql
    assert "contracts.tenant_id" in sql
    assert "contracts.id = tickets.contract_id" in sql or "tickets.contract_id = contracts.id" in sql
    assert "properties" not in sql


def test_select_for_owner_role_is_exists_through_properties() -> None:
    scope = _scope(role=UserRole.OWNER)
    repo = TicketRepository(AsyncMock(), scope)

    sql = _compiled(repo.select())

    assert "tickets" in sql
    assert scope.organization_id.hex in sql
    assert "EXISTS" in sql
    assert "properties" in sql
    assert "properties.owner_id" in sql
    assert scope.user_id.hex in sql
    assert (
        "properties.id = tickets.property_id" in sql
        or "tickets.property_id = properties.id" in sql
    )
    # Deliberately does NOT filter out soft-deleted properties: an owner
    # keeps seeing ticket history for a property they later deleted.
    assert "deleted_at" not in sql
    assert "contracts" not in sql


# ---------------------------------------------------------------------------
# list(): filters compose on top of select()
# ---------------------------------------------------------------------------


def test_list_with_no_filters_is_equivalent_to_select() -> None:
    scope = _scope(role=UserRole.ADMIN)
    repo = TicketRepository(AsyncMock(), scope)

    sql = _compiled(repo.list())

    assert "tickets" in sql
    assert scope.organization_id.hex in sql


def test_list_filters_by_status() -> None:
    scope = _scope(role=UserRole.ADMIN)
    repo = TicketRepository(AsyncMock(), scope)

    sql = _compiled(repo.list(status=TicketStatus.OPEN))

    assert "tickets.status" in sql
    assert "'open'" in sql


def test_list_filters_by_category_urgency_agent_property() -> None:
    scope = _scope(role=UserRole.ADMIN)
    repo = TicketRepository(AsyncMock(), scope)
    category_id = uuid.uuid4()
    urgency_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    property_id = uuid.uuid4()

    sql = _compiled(
        repo.list(
            category_id=category_id,
            urgency_id=urgency_id,
            agent_id=agent_id,
            property_id=property_id,
        )
    )

    assert "tickets.category_id" in sql
    assert category_id.hex in sql
    assert "tickets.urgency_id" in sql
    assert urgency_id.hex in sql
    assert "tickets.agent_id" in sql
    assert agent_id.hex in sql
    assert "tickets.property_id" in sql
    assert property_id.hex in sql


# ---------------------------------------------------------------------------
# get_or_404: 404 uniformly for cross-org AND role-invisible tickets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_404_raises_not_found_when_not_visible() -> None:
    session = _session_returning(None)
    repo = TicketRepository(session, _scope(role=UserRole.TENANT))

    with pytest.raises(NotFoundError):
        await repo.get_or_404(uuid.uuid4())


@pytest.mark.asyncio
async def test_get_or_404_returns_the_row_when_visible() -> None:
    fake_row = MagicMock()
    session = _session_returning(fake_row)
    repo = TicketRepository(session, _scope(role=UserRole.ADMIN))

    result = await repo.get_or_404(uuid.uuid4())

    assert result is fake_row


# ---------------------------------------------------------------------------
# list_pending(): tickets still awaiting classification, older than a cutoff
# ---------------------------------------------------------------------------


def test_list_pending_filters_by_status_and_created_at() -> None:
    scope = _scope(role=UserRole.ADMIN)
    repo = TicketRepository(AsyncMock(), scope)
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)

    sql = _compiled(repo.list_pending(older_than=cutoff))

    assert "tickets.classification_status" in sql
    assert f"'{ClassificationStatus.PENDING.value}'" in sql
    assert "tickets.created_at" in sql
    assert "2026-01-01" in sql
    assert scope.organization_id.hex in sql


def test_list_pending_is_scoped_like_select() -> None:
    scope = _scope(role=UserRole.TENANT)
    repo = TicketRepository(AsyncMock(), scope)
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)

    sql = _compiled(repo.list_pending(older_than=cutoff))

    # A tenant's list_pending() still goes through the role-fused select(),
    # not a bare org-scoped query.
    assert "EXISTS" in sql
    assert "contracts" in sql

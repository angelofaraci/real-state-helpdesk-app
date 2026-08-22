"""Unit tests for `app.services.notification_service` — `resolve_recipients`
and `fan_out` (stage 6, PR5), plus `list_notifications`/`mark_read` (stage 6,
PR7b — the notifications API's service-layer backing).

RESOLVED decision (encoded exactly): the SLA warning/breach recipient set
is every `ACTIVE` `role=ADMIN` user in the ticket's organization, PLUS the
ticket's assigned agent if `agent_id` is set and that user is `ACTIVE`
(any role) — no `users.supervisor_id` column, no queue table. Recipients
are always resolved fresh via a live query, never cached. Zero active
admins + an unassigned ticket is a normal, valid outcome: `[]`, not an
error.

`AsyncSession` is mocked, following the sequential-`execute`-calls pattern
already established by `tests/unit/test_ticket_service_update.py`.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import NotFoundError as CoreNotFoundError
from app.core.scope import OrgScope
from app.models.enums import NotificationKind, UserRole, UserStatus
from app.models.notification import Notification
from app.models.ticket import Ticket
from app.models.user import User
from app.services import notification_service


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _user(scope: OrgScope, **overrides) -> User:
    defaults = dict(
        id=uuid.uuid4(),
        organization_id=scope.organization_id,
        name="Some User",
        email=f"user-{uuid.uuid4()}@example.com",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    defaults.update(overrides)
    return User(**defaults)


def _ticket(scope: OrgScope, **overrides) -> Ticket:
    defaults = dict(
        id=uuid.uuid4(),
        organization_id=scope.organization_id,
        user_id=uuid.uuid4(),
        agent_id=None,
    )
    defaults.update(overrides)
    return Ticket(**defaults)


def _notification(scope: OrgScope, **overrides) -> Notification:
    defaults = dict(
        id=uuid.uuid4(),
        organization_id=scope.organization_id,
        user_id=scope.user_id,
        ticket_id=None,
        sla_event_id=None,
        kind=NotificationKind.SLA_WARNING,
        title="SLA warning",
        body=None,
        sent_at=datetime(2024, 1, 1, tzinfo=UTC),
        read_at=None,
    )
    defaults.update(overrides)
    return Notification(**defaults)


def _scalars_result(items: list[object]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
    return result


def _scalar_result(item: object | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = item
    return result


def _session(*execute_results: object) -> AsyncMock:
    session = AsyncMock()
    session.execute.side_effect = list(execute_results)
    return session


# ---------------------------------------------------------------------------
# resolve_recipients
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_recipients_returns_active_admins_plus_the_assigned_agent() -> None:
    scope = _scope()
    admin_1 = _user(scope, role=UserRole.ADMIN)
    admin_2 = _user(scope, role=UserRole.ADMIN)
    agent = _user(scope, role=UserRole.AGENT)
    ticket = _ticket(scope, agent_id=agent.id)
    session = _session(
        _scalars_result([admin_1, admin_2]),
        _scalar_result(agent),
    )

    recipients = await notification_service.resolve_recipients(
        session, scope=scope, ticket=ticket
    )

    assert {u.id for u in recipients} == {admin_1.id, admin_2.id, agent.id}
    assert len(recipients) == 3


@pytest.mark.asyncio
async def test_resolve_recipients_dedupes_when_the_assigned_agent_is_also_an_admin() -> None:
    """The assigned agent may happen to also carry `role=ADMIN` (already
    counted by the admin query) — must not be counted twice."""
    scope = _scope()
    admin_who_is_also_agent = _user(scope, role=UserRole.ADMIN)
    other_admin = _user(scope, role=UserRole.ADMIN)
    ticket = _ticket(scope, agent_id=admin_who_is_also_agent.id)
    session = _session(
        _scalars_result([admin_who_is_also_agent, other_admin]),
        _scalar_result(admin_who_is_also_agent),
    )

    recipients = await notification_service.resolve_recipients(
        session, scope=scope, ticket=ticket
    )

    assert {u.id for u in recipients} == {admin_who_is_also_agent.id, other_admin.id}
    assert len(recipients) == 2


@pytest.mark.asyncio
async def test_resolve_recipients_returns_empty_list_for_unassigned_ticket_with_no_admins() -> (
    None
):
    """Zero active admins + an unassigned ticket is a normal, valid
    outcome — `[]`, never an exception."""
    scope = _scope()
    ticket = _ticket(scope, agent_id=None)
    session = _session(_scalars_result([]))

    recipients = await notification_service.resolve_recipients(
        session, scope=scope, ticket=ticket
    )

    assert recipients == []
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_recipients_excludes_a_disabled_admin() -> None:
    scope = _scope()
    active_admin = _user(scope, role=UserRole.ADMIN, status=UserStatus.ACTIVE)
    ticket = _ticket(scope, agent_id=None)
    # The disabled admin is excluded at the query level (status filter), so
    # the mocked `admins` query result never even includes it — this test
    # proves the query is *scoped* to only active admins in the first
    # place, matching the query built by `resolve_recipients`.
    session = _session(_scalars_result([active_admin]))

    recipients = await notification_service.resolve_recipients(
        session, scope=scope, ticket=ticket
    )

    assert recipients == [active_admin]
    (stmt,), _kwargs = session.execute.call_args
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "users.status" in sql
    assert "'active'" in sql
    assert "users.role" in sql
    assert "'admin'" in sql


@pytest.mark.asyncio
async def test_resolve_recipients_skips_agent_lookup_when_unassigned() -> None:
    scope = _scope()
    admin = _user(scope, role=UserRole.ADMIN)
    ticket = _ticket(scope, agent_id=None)
    session = _session(_scalars_result([admin]))

    recipients = await notification_service.resolve_recipients(
        session, scope=scope, ticket=ticket
    )

    assert recipients == [admin]
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_recipients_excludes_an_inactive_assigned_agent() -> None:
    """An assigned agent whose status is not `ACTIVE` (e.g. `disabled`)
    must not receive notifications."""
    scope = _scope()
    ticket = _ticket(scope, agent_id=uuid.uuid4())
    session = _session(
        _scalars_result([]),
        _scalar_result(None),
    )

    recipients = await notification_service.resolve_recipients(
        session, scope=scope, ticket=ticket
    )

    assert recipients == []


# ---------------------------------------------------------------------------
# fan_out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fan_out_creates_one_notification_per_recipient(monkeypatch) -> None:
    scope = _scope()
    recipient_1 = _user(scope)
    recipient_2 = _user(scope)
    ticket = _ticket(scope)
    sla_event_id = uuid.uuid4()
    session = AsyncMock()
    monkeypatch.setattr(
        notification_service,
        "resolve_recipients",
        AsyncMock(return_value=[recipient_1, recipient_2]),
    )

    await notification_service.fan_out(
        session,
        scope=scope,
        ticket=ticket,
        sla_event_id=sla_event_id,
        kind=NotificationKind.SLA_WARNING,
    )

    assert session.add.call_count == 2
    created = [call.args[0] for call in session.add.call_args_list]
    assert {n.user_id for n in created} == {recipient_1.id, recipient_2.id}
    for notification in created:
        assert notification.kind == NotificationKind.SLA_WARNING
        assert notification.ticket_id == ticket.id
        assert notification.sla_event_id == sla_event_id
        assert notification.organization_id == scope.organization_id
        assert str(ticket.id) in notification.title


@pytest.mark.asyncio
async def test_fan_out_creates_zero_notifications_and_does_not_raise_when_no_recipients(
    monkeypatch,
) -> None:
    scope = _scope()
    ticket = _ticket(scope)
    session = AsyncMock()
    monkeypatch.setattr(
        notification_service, "resolve_recipients", AsyncMock(return_value=[])
    )

    await notification_service.fan_out(
        session,
        scope=scope,
        ticket=ticket,
        sla_event_id=uuid.uuid4(),
        kind=NotificationKind.SLA_BREACHED,
    )

    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_fan_out_breached_kind_uses_breached_copy(monkeypatch) -> None:
    scope = _scope()
    recipient = _user(scope)
    ticket = _ticket(scope)
    session = AsyncMock()
    monkeypatch.setattr(
        notification_service, "resolve_recipients", AsyncMock(return_value=[recipient])
    )

    await notification_service.fan_out(
        session,
        scope=scope,
        ticket=ticket,
        sla_event_id=uuid.uuid4(),
        kind=NotificationKind.SLA_BREACHED,
    )

    (notification,), _kwargs = session.add.call_args
    assert notification.kind == NotificationKind.SLA_BREACHED
    assert "breach" in notification.title.lower()


# ---------------------------------------------------------------------------
# list_notifications (stage 6, PR7b)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_notifications_returns_items_and_total() -> None:
    scope = _scope()
    notification_1 = _notification(scope)
    notification_2 = _notification(scope)
    count_result = MagicMock()
    count_result.scalar_one.return_value = 2
    session = _session(count_result, _scalars_result([notification_1, notification_2]))

    items, total = await notification_service.list_notifications(
        session, scope=scope, limit=20, offset=0
    )

    assert items == [notification_1, notification_2]
    assert total == 2


@pytest.mark.asyncio
async def test_list_notifications_applies_limit_and_offset_to_the_page_query() -> None:
    scope = _scope()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 0
    session = _session(count_result, _scalars_result([]))

    await notification_service.list_notifications(session, scope=scope, limit=5, offset=10)

    (page_stmt,), _kwargs = session.execute.call_args_list[1]
    sql = str(page_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "LIMIT 5" in sql
    assert "OFFSET 10" in sql


@pytest.mark.asyncio
async def test_list_notifications_scopes_the_page_query_to_the_recipient() -> None:
    """`list_notifications` must build its page query on top of
    `NotificationRepository.for_recipient` — never a raw scoped query that
    duplicates its cross-user filtering."""
    scope = _scope()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 0
    session = _session(count_result, _scalars_result([]))

    await notification_service.list_notifications(session, scope=scope, limit=20, offset=0)

    (page_stmt,), _kwargs = session.execute.call_args_list[1]
    sql = str(page_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "notifications.user_id" in sql
    assert scope.user_id.hex in sql
    assert scope.organization_id.hex in sql


# ---------------------------------------------------------------------------
# mark_read (stage 6, PR7b)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_read_sets_read_at_when_currently_unread() -> None:
    scope = _scope()
    notification = _notification(scope, read_at=None)
    session = _session(_scalar_result(notification))

    result = await notification_service.mark_read(
        session, scope=scope, notification_id=notification.id
    )

    assert result.read_at is not None


@pytest.mark.asyncio
async def test_mark_read_is_idempotent_and_never_overwrites_an_existing_read_at() -> None:
    scope = _scope()
    already_read_at = datetime(2024, 1, 1, tzinfo=UTC)
    notification = _notification(scope, read_at=already_read_at)
    session = _session(_scalar_result(notification))

    result = await notification_service.mark_read(
        session, scope=scope, notification_id=notification.id
    )

    assert result.read_at == already_read_at


@pytest.mark.asyncio
async def test_mark_read_raises_not_found_when_not_visible_to_the_scope() -> None:
    """Covers both cross-user and cross-org invisibility in one place:
    `for_recipient` structurally excludes both, so the mocked lookup
    returning `None` stands in for either case."""
    scope = _scope()
    session = _session(_scalar_result(None))

    with pytest.raises(CoreNotFoundError):
        await notification_service.mark_read(
            session, scope=scope, notification_id=uuid.uuid4()
        )

"""Integration tests for Stage 6 (queues + SLA) real-Postgres-only
constraint enforcement, following the same skip-stub pattern as
`tests/integration/test_channel_identity_constraints.py` and
`tests/integration/test_classification_constraints.py`.

`tests/unit/test_sla_event_model.py` and `tests/unit/test_notification_model.py`
prove the ORM metadata declares the right shape (columns, FK targets,
enum, indexes, check constraint text) via pure `Base.metadata` inspection.
They do NOT prove that:

- The real `ux_sla_events_ticket_event_once` partial unique index
  (migration 0009) actually rejects a second `(ticket_id, event='warning')`
  row for the same ticket at the database level, while allowing a second
  `event='resolved'` row for that same ticket (the reopen/re-resolve
  case the partial predicate deliberately preserves).
- The real `ck_notifications_title_not_blank` CHECK constraint enforces a
  non-blank title independently of any Python-side validation a later PR
  might add on top.

No live Postgres is reachable in this sandbox. These tests are
intentionally skipped rather than faking Postgres-specific behavior with
SQLite, and should be run manually or in CI against
`docker compose up -d postgres` once a Postgres integration test harness
exists.
"""

import secrets
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import NotificationKind, SlaEventType, UserRole, UserStatus
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.sla_event import SlaEvent
from app.models.ticket import Ticket
from app.models.user import User


async def _make_org(db_session: AsyncSession) -> Organization:
    org = Organization(
        name=f"Test Org {uuid4()}",
        chat_widget_key=secrets.token_urlsafe(16),
        timezone="UTC",
    )
    db_session.add(org)
    await db_session.flush()
    return org


async def _make_ticket(db_session: AsyncSession, org: Organization) -> Ticket:
    user = User(
        organization_id=org.id,
        name="Test User",
        email=f"{uuid4()}@example.com",
        role=UserRole.TENANT,
        password_hash="not-a-real-hash",
        status=UserStatus.ACTIVE,
    )
    db_session.add(user)
    await db_session.flush()

    ticket = Ticket(
        organization_id=org.id,
        user_id=user.id,
        title="Test ticket",
        created_at=datetime.now(UTC),
    )
    db_session.add(ticket)
    await db_session.flush()
    return ticket


async def test_duplicate_sla_warning_event_for_the_same_ticket_is_rejected(
    db_session: AsyncSession,
) -> None:
    """Inserting a second `SlaEvent(ticket_id=X, event='warning')` row for
    a ticket that already has one must raise `IntegrityError` against the
    real `ux_sla_events_ticket_event_once` partial unique index."""
    org = await _make_org(db_session)
    ticket = await _make_ticket(db_session, org)

    db_session.add(
        SlaEvent(
            organization_id=org.id,
            ticket_id=ticket.id,
            event=SlaEventType.WARNING,
            occurred_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    db_session.add(
        SlaEvent(
            organization_id=org.id,
            ticket_id=ticket.id,
            event=SlaEventType.WARNING,
            occurred_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError, match="ux_sla_events_ticket_event_once"):
        await db_session.flush()


async def test_repeated_resolved_events_for_the_same_ticket_are_allowed(
    db_session: AsyncSession,
) -> None:
    """Two `SlaEvent(ticket_id=X, event='resolved')` rows for the same
    ticket must both insert successfully — `resolved` is deliberately
    excluded from the partial unique index's predicate so a ticket's
    reopen/re-resolve cycle can record it repeatedly."""
    org = await _make_org(db_session)
    ticket = await _make_ticket(db_session, org)

    db_session.add(
        SlaEvent(
            organization_id=org.id,
            ticket_id=ticket.id,
            event=SlaEventType.RESOLVED,
            occurred_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    db_session.add(
        SlaEvent(
            organization_id=org.id,
            ticket_id=ticket.id,
            event=SlaEventType.RESOLVED,
            occurred_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    result = await db_session.execute(
        select(SlaEvent).where(SlaEvent.ticket_id == ticket.id)
    )
    assert len(result.scalars().all()) == 2


async def test_blank_notification_title_is_rejected_by_the_check_constraint(
    db_session: AsyncSession,
) -> None:
    """Inserting a `Notification` with a blank/whitespace-only `title`
    must raise `IntegrityError` against the real
    `ck_notifications_title_not_blank` CHECK constraint, independent of
    any Python-side validation.

    This is the CI "proof of life" test for Stage 8 PR1: it is the one
    `_pg`-style real-Postgres test unskipped to prove the CI workflow's
    new `postgres` + `redis` service containers and `alembic upgrade
    head` step actually work end to end. The other skip-marked tests in
    this file remain skipped/stubbed; filling in their bodies is out of
    scope for this PR.
    """
    org = Organization(
        name=f"Test Org {uuid4()}",
        chat_widget_key=secrets.token_urlsafe(16),
        timezone="UTC",
    )
    db_session.add(org)
    await db_session.flush()

    user = User(
        organization_id=org.id,
        name="Test User",
        email=f"{uuid4()}@example.com",
        role=UserRole.AGENT,
        password_hash="not-a-real-hash",
        status=UserStatus.ACTIVE,
    )
    db_session.add(user)
    await db_session.flush()

    notification = Notification(
        organization_id=org.id,
        user_id=user.id,
        kind=NotificationKind.SLA_WARNING,
        title="   ",
    )
    db_session.add(notification)

    with pytest.raises(IntegrityError, match="ck_notifications_title_not_blank"):
        await db_session.flush()

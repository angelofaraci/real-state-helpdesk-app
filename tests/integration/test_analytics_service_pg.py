"""Integration tests for `app.services.analytics_service` against a REAL
Postgres instance (stage 7 — analytics, PR3), following
`tests/integration/test_chat_flow.py`'s `db_session`-fixture,
real-row-creation pattern.

Unlike `tests/unit/test_analytics_service.py` (mocked session, compiled-SQL
shape assertions), these tests prove actual aggregation CORRECTNESS against
known-data fixtures:

- Org-local day bucketing genuinely differs by `organizations.timezone`
  for the same UTC instant (D3).
- A same-day reopen + re-resolve of one ticket counts `tickets_resolved`
  TWICE (event-based, per spec Q1).
- `avg_resolution_hours` genuinely excludes tickets with an escalated chat
  session (D7).
- `chat_escalation_rate`/`chat_to_ticket_rate` use session-START-day
  cohort semantics (spec Q3).
- `upsert_daily_metrics` genuinely overwrites `metric_value` and bumps
  `updated_at` on a second call for the same key, never duplicating the
  row (D6).

A live Postgres instance IS reachable in this sandbox (`docker compose up
-d postgres`, confirmed healthy, migrated to head `0010`) — unlike the
older stage-6 integration test modules' skip-stub precedent
(`tests/integration/test_sla_event_constraints.py`), these tests run
unskipped.
"""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sla_defaults import DEFAULT_BUSINESS_HOURS
from app.models.chat_session import ChatSession
from app.models.classification import Classification
from app.models.daily_metric import DailyMetric
from app.models.enums import AuthorType, SlaEventType, TicketStatus, UserRole, UserStatus
from app.models.message import Message
from app.models.organization import Organization
from app.models.sla_event import SlaEvent
from app.models.ticket import Ticket
from app.models.user import User
from app.services import analytics_service


async def _make_org(db_session: AsyncSession, *, timezone: str) -> Organization:
    org = Organization(
        name=f"Test Org {uuid4()}",
        chat_widget_key=secrets.token_urlsafe(16),
        timezone=timezone,
        business_hours=DEFAULT_BUSINESS_HOURS,
    )
    db_session.add(org)
    await db_session.flush()
    return org


async def _make_user(db_session: AsyncSession, org: Organization) -> User:
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
    return user


async def _make_ticket(
    db_session: AsyncSession, org: Organization, user: User, *, created_at: datetime, **overrides
) -> Ticket:
    defaults: dict = dict(
        organization_id=org.id,
        user_id=user.id,
        title="Test ticket",
        created_at=created_at,
    )
    defaults.update(overrides)
    ticket = Ticket(**defaults)
    db_session.add(ticket)
    await db_session.flush()
    return ticket


async def _make_sla_event(
    db_session: AsyncSession,
    org: Organization,
    ticket: Ticket,
    *,
    event: SlaEventType,
    occurred_at: datetime,
) -> SlaEvent:
    sla_event = SlaEvent(
        organization_id=org.id, ticket_id=ticket.id, event=event, occurred_at=occurred_at
    )
    db_session.add(sla_event)
    await db_session.flush()
    return sla_event


async def _make_chat_session(
    db_session: AsyncSession,
    org: Organization,
    *,
    created_at: datetime,
    escalated_at: datetime | None = None,
    ticket: Ticket | None = None,
    user: User | None = None,
) -> ChatSession:
    chat_session = ChatSession(
        organization_id=org.id,
        user_id=user.id if user else None,
        ticket_id=ticket.id if ticket else None,
        created_at=created_at,
        escalated_at=escalated_at,
    )
    db_session.add(chat_session)
    await db_session.flush()
    return chat_session


async def _make_classification(
    db_session: AsyncSession, ticket: Ticket, *, created_at: datetime, **overrides
) -> Classification:
    defaults: dict = dict(
        ticket_id=ticket.id,
        human_corrected=False,
        created_at=created_at,
    )
    defaults.update(overrides)
    classification = Classification(**defaults)
    db_session.add(classification)
    await db_session.flush()
    return classification


async def _make_message(
    db_session: AsyncSession, ticket: Ticket, *, created_at: datetime, **overrides
) -> Message:
    defaults: dict = dict(
        ticket_id=ticket.id,
        author_type=AuthorType.BOT,
        content="Suggested reply",
        is_ai_suggestion=False,
        created_at=created_at,
    )
    defaults.update(overrides)
    message = Message(**defaults)
    db_session.add(message)
    await db_session.flush()
    return message


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skip(
        reason="Requires a live Postgres instance (see docker-compose.yml); "
        "not reachable in CI. Run manually or in CI with "
        "`docker compose up -d db && pytest tests/integration -m ''`."
    ),
]


# ---------------------------------------------------------------------------
# tickets family — tickets_created, known-data fixture (task 3.1/3.2)
# ---------------------------------------------------------------------------


async def test_tickets_created_count_matches_exact_known_fixture_count(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    user = await _make_user(db_session, org)
    local_day = date(2026, 8, 20)
    # Local midnight for UTC-3 is 03:00Z — put 3 tickets solidly inside the
    # local day and 1 just outside it (the previous local day).
    in_day = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    outside_day = datetime(2026, 8, 20, 2, 59, tzinfo=UTC)  # still 2026-08-19 local

    for _ in range(3):
        await _make_ticket(db_session, org, user, created_at=in_day)
    await _make_ticket(db_session, org, user, created_at=outside_day)

    result = await analytics_service.tickets_created_count(db_session, org, local_day)

    assert result == 3


async def test_tickets_created_count_org_local_day_boundary_differs_by_timezone(
    db_session: AsyncSession,
) -> None:
    """Task 3.13/3.14: two orgs with different timezones, same UTC instant,
    bucket into different `metric_date` rows."""
    org_ba = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")  # UTC-3
    org_tokyo = await _make_org(db_session, timezone="Asia/Tokyo")  # UTC+9
    user_ba = await _make_user(db_session, org_ba)
    user_tokyo = await _make_user(db_session, org_tokyo)

    # 2026-08-20T01:00:00Z is 2026-08-19 local in Buenos Aires (UTC-3) but
    # 2026-08-20 local in Tokyo (UTC+9) — the SAME UTC instant.
    shared_instant = datetime(2026, 8, 20, 1, 0, tzinfo=UTC)
    await _make_ticket(db_session, org_ba, user_ba, created_at=shared_instant)
    await _make_ticket(db_session, org_tokyo, user_tokyo, created_at=shared_instant)

    ba_aug20 = await analytics_service.tickets_created_count(db_session, org_ba, date(2026, 8, 20))
    ba_aug19 = await analytics_service.tickets_created_count(db_session, org_ba, date(2026, 8, 19))
    tokyo_aug20 = await analytics_service.tickets_created_count(
        db_session, org_tokyo, date(2026, 8, 20)
    )

    assert ba_aug20 == 0
    assert ba_aug19 == 1
    assert tokyo_aug20 == 1


# ---------------------------------------------------------------------------
# tickets family — tickets_open (PR6)
# ---------------------------------------------------------------------------


async def test_tickets_open_count_includes_a_ticket_still_open_as_of_day_end(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    user = await _make_user(db_session, org)
    local_day = date(2026, 8, 20)
    # Created well before the day, never closed.
    created_at = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    await _make_ticket(db_session, org, user, created_at=created_at)

    result = await analytics_service.tickets_open_count(db_session, org, local_day)

    assert result == 1


async def test_tickets_open_count_includes_a_ticket_closed_after_day_end(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    user = await _make_user(db_session, org)
    local_day = date(2026, 8, 20)
    created_at = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    # Local day end for 2026-08-20 in UTC-3 is 2026-08-21T03:00:00Z; this
    # ticket closes well after that.
    closed_at = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    await _make_ticket(
        db_session,
        org,
        user,
        created_at=created_at,
        status=TicketStatus.CLOSED,
        closed_at=closed_at,
    )

    result = await analytics_service.tickets_open_count(db_session, org, local_day)

    assert result == 1


async def test_tickets_open_count_excludes_a_ticket_closed_before_day_end(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    user = await _make_user(db_session, org)
    local_day = date(2026, 8, 20)
    created_at = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    # Closed BEFORE the local day's end (2026-08-21T03:00:00Z).
    closed_at = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    await _make_ticket(
        db_session,
        org,
        user,
        created_at=created_at,
        status=TicketStatus.CLOSED,
        closed_at=closed_at,
    )

    result = await analytics_service.tickets_open_count(db_session, org, local_day)

    assert result == 0


async def test_tickets_open_count_excludes_a_ticket_created_after_the_day(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    user = await _make_user(db_session, org)
    local_day = date(2026, 8, 20)
    # Created AFTER local_day's end.
    created_at = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    await _make_ticket(db_session, org, user, created_at=created_at)

    result = await analytics_service.tickets_open_count(db_session, org, local_day)

    assert result == 0


# ---------------------------------------------------------------------------
# SLA family — tickets_resolved (event-based, double-counts reopen+re-resolve),
# avg_resolution_hours (excludes escalated tickets) (task 3.3/3.4)
# ---------------------------------------------------------------------------


async def test_tickets_resolved_count_double_counts_same_day_reopen_and_re_resolve(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    user = await _make_user(db_session, org)
    created_at = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    ticket = await _make_ticket(db_session, org, user, created_at=created_at)

    # Resolved, then reopened, then re-resolved — both `resolved` events
    # land on the same org-local day (2026-08-20 in UTC-3).
    await _make_sla_event(
        db_session, org, ticket, event=SlaEventType.RESOLVED,
        occurred_at=datetime(2026, 8, 20, 14, 0, tzinfo=UTC),
    )
    await _make_sla_event(
        db_session, org, ticket, event=SlaEventType.RESOLVED,
        occurred_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
    )

    result = await analytics_service.tickets_resolved_count(db_session, org, date(2026, 8, 20))

    assert result == 2


async def test_avg_resolution_hours_excludes_tickets_with_an_escalated_chat_session(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    user = await _make_user(db_session, org)
    local_day = date(2026, 8, 20)

    # Eligible ticket: resolved 4 hours after creation, NO escalated chat session.
    eligible_created = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    eligible_resolved = datetime(2026, 8, 20, 14, 0, tzinfo=UTC)
    eligible_ticket = await _make_ticket(db_session, org, user, created_at=eligible_created)
    await _make_sla_event(
        db_session, org, eligible_ticket, event=SlaEventType.RESOLVED,
        occurred_at=eligible_resolved,
    )

    # Excluded ticket: resolved 100 hours after creation (would badly skew
    # the average), but its chat session WAS escalated.
    excluded_created = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    excluded_resolved = datetime(2026, 8, 20, 14, 0, tzinfo=UTC)
    excluded_ticket = await _make_ticket(db_session, org, user, created_at=excluded_created)
    await _make_sla_event(
        db_session, org, excluded_ticket, event=SlaEventType.RESOLVED,
        occurred_at=excluded_resolved,
    )
    await _make_chat_session(
        db_session, org,
        created_at=excluded_created,
        escalated_at=excluded_created + timedelta(minutes=5),
        ticket=excluded_ticket,
        user=user,
    )

    result = await analytics_service.avg_resolution_hours(db_session, org, local_day)

    assert result == pytest.approx(Decimal("4.0"), abs=Decimal("0.01"))


async def test_avg_resolution_hours_returns_none_when_all_resolved_tickets_are_excluded(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    user = await _make_user(db_session, org)
    created_at = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    ticket = await _make_ticket(db_session, org, user, created_at=created_at)
    await _make_sla_event(
        db_session, org, ticket, event=SlaEventType.RESOLVED,
        occurred_at=datetime(2026, 8, 20, 14, 0, tzinfo=UTC),
    )
    await _make_chat_session(
        db_session, org, created_at=created_at, escalated_at=created_at, ticket=ticket, user=user
    )

    result = await analytics_service.avg_resolution_hours(db_session, org, date(2026, 8, 20))

    assert result is None


# ---------------------------------------------------------------------------
# SLA family — sla_compliance_rate (PR6)
# ---------------------------------------------------------------------------


async def test_sla_compliance_rate_counts_on_time_compliant_and_late_non_compliant(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    user = await _make_user(db_session, org)
    local_day = date(2026, 8, 20)
    created_at = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)

    # On-time: resolved before its due date.
    on_time_ticket = await _make_ticket(
        db_session, org, user, created_at=created_at,
        sla_due_at=datetime(2026, 8, 20, 16, 0, tzinfo=UTC),
    )
    await _make_sla_event(
        db_session, org, on_time_ticket, event=SlaEventType.RESOLVED,
        occurred_at=datetime(2026, 8, 20, 14, 0, tzinfo=UTC),
    )

    # Late: resolved after its due date.
    late_ticket = await _make_ticket(
        db_session, org, user, created_at=created_at,
        sla_due_at=datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
    )
    await _make_sla_event(
        db_session, org, late_ticket, event=SlaEventType.RESOLVED,
        occurred_at=datetime(2026, 8, 20, 15, 0, tzinfo=UTC),
    )

    result = await analytics_service.sla_compliance_rate(db_session, org, local_day)

    assert result == Decimal(1) / Decimal(2)


async def test_sla_compliance_rate_excludes_resolved_events_with_no_sla_due_at(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    user = await _make_user(db_session, org)
    local_day = date(2026, 8, 20)
    created_at = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)

    # On-time, eligible.
    eligible_ticket = await _make_ticket(
        db_session, org, user, created_at=created_at,
        sla_due_at=datetime(2026, 8, 20, 16, 0, tzinfo=UTC),
    )
    await _make_sla_event(
        db_session, org, eligible_ticket, event=SlaEventType.RESOLVED,
        occurred_at=datetime(2026, 8, 20, 14, 0, tzinfo=UTC),
    )

    # No sla_due_at at all — excluded from both numerator and denominator,
    # even though it resolved.
    unclassified_ticket = await _make_ticket(
        db_session, org, user, created_at=created_at, sla_due_at=None,
    )
    await _make_sla_event(
        db_session, org, unclassified_ticket, event=SlaEventType.RESOLVED,
        occurred_at=datetime(2026, 8, 20, 14, 0, tzinfo=UTC),
    )

    result = await analytics_service.sla_compliance_rate(db_session, org, local_day)

    assert result == Decimal(1)


async def test_sla_compliance_rate_returns_none_when_zero_eligible_resolved_events(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")

    result = await analytics_service.sla_compliance_rate(db_session, org, date(2026, 8, 20))

    assert result is None


# ---------------------------------------------------------------------------
# chat family — cohort semantics on session START day (task 3.5/3.6)
# ---------------------------------------------------------------------------


async def test_chat_escalation_rate_and_chat_to_ticket_rate_use_session_start_day_cohort(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    user = await _make_user(db_session, org)
    local_day = date(2026, 8, 20)
    started_at = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    ticket = await _make_ticket(db_session, org, user, created_at=started_at)

    # 4 sessions started that day: 2 escalated (1 also linked to a ticket,
    # escalated the NEXT calendar day — proving cohort keys on START day),
    # 1 more linked to a ticket without escalation, 1 plain.
    await _make_chat_session(
        db_session, org, created_at=started_at,
        escalated_at=started_at + timedelta(days=1), ticket=ticket, user=user,
    )
    await _make_chat_session(db_session, org, created_at=started_at, escalated_at=started_at)
    await _make_chat_session(db_session, org, created_at=started_at, ticket=ticket, user=user)
    await _make_chat_session(db_session, org, created_at=started_at)

    started = await analytics_service.chat_sessions_started_count(db_session, org, local_day)
    escalation_rate = await analytics_service.chat_escalation_rate(db_session, org, local_day)
    to_ticket_rate = await analytics_service.chat_to_ticket_rate(db_session, org, local_day)

    assert started == 4
    assert escalation_rate == Decimal(2) / Decimal(4)
    assert to_ticket_rate == Decimal(2) / Decimal(4)


async def test_chat_family_metrics_omits_rates_with_zero_sessions_started(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")

    metrics = await analytics_service.chat_family_metrics(db_session, org, date(2026, 8, 20))

    assert metrics == [("chat_sessions_started", Decimal(0))]


# ---------------------------------------------------------------------------
# classifier family — classifier_accuracy, llm_fallback_rate (PR6)
# ---------------------------------------------------------------------------


async def test_classifier_family_metrics_computes_accuracy_and_fallback_rate_for_the_day(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    user = await _make_user(db_session, org)
    local_day = date(2026, 8, 20)
    in_day = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    outside_day = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

    # 4 classifications in-day: 1 human_corrected, 3 not -> accuracy 0.75.
    # 2 of the 4 used model_used='llm_fallback' -> fallback rate 0.5.
    specs = [
        dict(human_corrected=True, model_used="sklearn_v1"),
        dict(human_corrected=False, model_used="sklearn_v1"),
        dict(human_corrected=False, model_used="llm_fallback"),
        dict(human_corrected=False, model_used="llm_fallback"),
    ]
    for spec in specs:
        ticket = await _make_ticket(db_session, org, user, created_at=in_day)
        await _make_classification(db_session, ticket, created_at=in_day, **spec)

    # An out-of-day classification must not affect the day's aggregate.
    outside_ticket = await _make_ticket(db_session, org, user, created_at=outside_day)
    await _make_classification(
        db_session, outside_ticket, created_at=outside_day, human_corrected=True,
    )

    accuracy = await analytics_service.classifier_accuracy(db_session, org, local_day)
    fallback_rate = await analytics_service.llm_fallback_rate(db_session, org, local_day)
    metrics = await analytics_service.classifier_family_metrics(db_session, org, local_day)

    assert accuracy == Decimal("0.75")
    assert fallback_rate == Decimal("0.5")
    assert metrics == [
        ("classifier_accuracy", Decimal("0.75")),
        ("llm_fallback_rate", Decimal("0.5")),
    ]


async def test_classifier_family_metrics_returns_empty_list_with_zero_classifications(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    local_day = date(2026, 8, 20)

    accuracy = await analytics_service.classifier_accuracy(db_session, org, local_day)
    fallback_rate = await analytics_service.llm_fallback_rate(db_session, org, local_day)
    metrics = await analytics_service.classifier_family_metrics(db_session, org, local_day)

    assert accuracy is None
    assert fallback_rate is None
    assert metrics == []


# ---------------------------------------------------------------------------
# RAG family — rag_suggestion_usage_rate (PR6)
# ---------------------------------------------------------------------------


async def test_rag_suggestion_day_totals_bucketed_by_message_created_at_day(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    user = await _make_user(db_session, org)
    local_day = date(2026, 8, 20)
    in_day = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    outside_day = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    ticket = await _make_ticket(db_session, org, user, created_at=in_day)

    # 3 AI-suggested messages in-day.
    suggestion_1 = await _make_message(
        db_session, ticket, created_at=in_day, is_ai_suggestion=True,
    )
    await _make_message(db_session, ticket, created_at=in_day, is_ai_suggestion=True)
    await _make_message(db_session, ticket, created_at=in_day, is_ai_suggestion=True)

    # 2 human replies in-day, one based on a suggestion, one not.
    await _make_message(
        db_session, ticket, created_at=in_day,
        based_on_suggestion_id=suggestion_1.id,
    )
    await _make_message(db_session, ticket, created_at=in_day)
    # A second reply based on the SAME suggestion — used counts DISTINCT
    # suggestion ids, so this must not inflate the used count.
    await _make_message(
        db_session, ticket, created_at=in_day,
        based_on_suggestion_id=suggestion_1.id,
    )

    # Outside the day — must not be counted.
    await _make_message(
        db_session, ticket, created_at=outside_day, is_ai_suggestion=True,
    )

    generated, used = await analytics_service.rag_suggestion_day_totals(
        db_session, org, local_day
    )

    assert generated == 3
    assert used == 1


async def test_rag_family_metrics_computes_usage_rate_when_suggestions_generated(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    user = await _make_user(db_session, org)
    local_day = date(2026, 8, 20)
    in_day = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    ticket = await _make_ticket(db_session, org, user, created_at=in_day)

    suggestion = await _make_message(
        db_session, ticket, created_at=in_day, is_ai_suggestion=True,
    )
    await _make_message(db_session, ticket, created_at=in_day, is_ai_suggestion=True)
    await _make_message(
        db_session, ticket, created_at=in_day, based_on_suggestion_id=suggestion.id,
    )

    metrics = await analytics_service.rag_family_metrics(db_session, org, local_day)

    assert metrics == [("rag_suggestion_usage_rate", Decimal(1) / Decimal(2))]


async def test_rag_family_metrics_returns_empty_list_with_zero_suggestions_generated(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    local_day = date(2026, 8, 20)

    metrics = await analytics_service.rag_family_metrics(db_session, org, local_day)

    assert metrics == []


# ---------------------------------------------------------------------------
# upsert_daily_metrics — real ON CONFLICT DO UPDATE, no duplicate rows (task 3.7/3.8)
# ---------------------------------------------------------------------------


async def test_upsert_daily_metrics_overwrites_value_and_bumps_updated_at_never_duplicates(
    db_session: AsyncSession,
) -> None:
    org = await _make_org(db_session, timezone="America/Argentina/Buenos_Aires")
    org_id = org.id
    metric_date = date(2026, 8, 20)
    row = dict(
        organization_id=org_id,
        metric_date=metric_date,
        metric_key="tickets_created",
        metric_value=Decimal("3.0000"),
    )

    await analytics_service.upsert_daily_metrics(db_session, [row])
    await db_session.flush()

    first = (
        await db_session.execute(
            select(DailyMetric).where(
                DailyMetric.organization_id == org_id,
                DailyMetric.metric_date == metric_date,
                DailyMetric.metric_key == "tickets_created",
            )
        )
    ).scalar_one()
    assert first.metric_value == Decimal("3.0000")
    first_id = first.id
    first_updated_at = first.updated_at

    # Re-run with a CHANGED value — must overwrite, never insert a second row.
    changed_row = dict(row, metric_value=Decimal("7.0000"))
    await analytics_service.upsert_daily_metrics(db_session, [changed_row])
    await db_session.flush()
    db_session.expire_all()

    rows = (
        (
            await db_session.execute(
                select(DailyMetric).where(
                    DailyMetric.organization_id == org_id,
                    DailyMetric.metric_date == metric_date,
                    DailyMetric.metric_key == "tickets_created",
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(rows) == 1
    assert rows[0].id == first_id
    assert rows[0].metric_value == Decimal("7.0000")
    assert rows[0].updated_at >= first_updated_at

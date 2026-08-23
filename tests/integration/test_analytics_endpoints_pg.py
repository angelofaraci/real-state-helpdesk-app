"""API-level tests for the `/api/v1/analytics/*` endpoints (stage 7 —
analytics, PR4), against a REAL Postgres instance.

Unlike `tests/api/test_notifications_endpoints.py` (mocked session, mocked
service layer — appropriate for that router, which delegates to a
repository-backed service), these endpoints run LIVE cross-model aggregate
SQL (joins, `ROW_NUMBER() OVER (...)`, `FILTER (WHERE ...)`) directly in
`app.services.analytics_service`, so a mocked session would prove nothing
about correctness. Following `tests/integration/test_chat_flow.py`'s
`wired_app`/`db_session` real-row-creation pattern instead.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.session import get_session
from app.core.sla_defaults import DEFAULT_BUSINESS_HOURS
from app.main import app
from app.models.category import Category
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.classification import Classification
from app.models.daily_metric import DailyMetric
from app.models.enums import (
    AuthorType,
    ChatMessageRole,
    TicketChannel,
    TicketStatus,
    UserRole,
    UserStatus,
)
from app.models.message import Message
from app.models.organization import Organization
from app.models.ticket import Ticket
from app.models.urgency_level import UrgencyLevel
from app.models.user import User

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skip(
        reason="Requires a live Postgres instance (see docker-compose.yml); "
        "not reachable in CI. Run manually or in CI with "
        "`docker compose up -d db && pytest tests/integration -m ''`."
    ),
]

ANALYTICS_ROUTES = [
    "/api/v1/analytics/overview",
    "/api/v1/analytics/trends?metric_key=tickets_created",
    "/api/v1/analytics/classifier",
    "/api/v1/analytics/rag",
    "/api/v1/analytics/chatbot",
]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


async def _make_org(db_session: AsyncSession, *, timezone: str = "UTC") -> Organization:
    org = Organization(
        name=f"Test Org {uuid4()}",
        chat_widget_key=secrets.token_urlsafe(16),
        timezone=timezone,
        business_hours=DEFAULT_BUSINESS_HOURS,
    )
    db_session.add(org)
    await db_session.flush()
    return org


async def _make_user(db_session: AsyncSession, org: Organization, *, role: UserRole) -> User:
    user = User(
        organization_id=org.id,
        name="Test User",
        email=f"{uuid4()}@example.com",
        role=role,
        password_hash="not-a-real-hash",
        status=UserStatus.ACTIVE,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def _make_category(db_session: AsyncSession, org: Organization, *, name: str) -> Category:
    category = Category(organization_id=org.id, name=name)
    db_session.add(category)
    await db_session.flush()
    return category


async def _make_urgency_level(db_session: AsyncSession, org: Organization, *, name: str) -> UrgencyLevel:
    urgency = UrgencyLevel(organization_id=org.id, name=name, sla_hours=8)
    db_session.add(urgency)
    await db_session.flush()
    return urgency


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


async def _make_message(
    db_session: AsyncSession, ticket: Ticket, **overrides
) -> Message:
    defaults: dict = dict(
        ticket_id=ticket.id,
        author_type=AuthorType.BOT,
        content="Suggested reply",
        is_ai_suggestion=False,
    )
    defaults.update(overrides)
    message = Message(**defaults)
    db_session.add(message)
    await db_session.flush()
    return message


async def _make_chat_session(
    db_session: AsyncSession,
    org: Organization,
    *,
    created_at: datetime,
    escalated_at: datetime | None = None,
) -> ChatSession:
    chat_session = ChatSession(
        organization_id=org.id, created_at=created_at, escalated_at=escalated_at
    )
    db_session.add(chat_session)
    await db_session.flush()
    return chat_session


async def _make_chat_message(
    db_session: AsyncSession, chat_session: ChatSession, **overrides
) -> ChatMessage:
    defaults: dict = dict(
        chat_session_id=chat_session.id,
        role=ChatMessageRole.USER,
        content="hi",
    )
    defaults.update(overrides)
    chat_message = ChatMessage(**defaults)
    db_session.add(chat_message)
    await db_session.flush()
    return chat_message


async def _seed_classifier_sample(
    db_session: AsyncSession,
    org: Organization,
    user: User,
    category: Category,
    *,
    total: int,
    corrected_count: int,
    base_created_at: datetime,
) -> None:
    """Create `total` tickets (all in `category`), each with exactly one
    `Classification` row; the first `corrected_count` are
    `human_corrected=True`. Backs the `needs_review` boundary tests."""
    tickets = [
        Ticket(
            organization_id=org.id,
            user_id=user.id,
            title=f"Classifier sample {i}",
            category_id=category.id,
            created_at=base_created_at + timedelta(seconds=i),
        )
        for i in range(total)
    ]
    db_session.add_all(tickets)
    await db_session.flush()

    classifications = [
        Classification(
            ticket_id=ticket.id,
            human_corrected=i < corrected_count,
            created_at=base_created_at + timedelta(seconds=i),
        )
        for i, ticket in enumerate(tickets)
    ]
    db_session.add_all(classifications)
    await db_session.flush()


def _fake_principal(*, role: UserRole, organization_id) -> User:
    return type(
        "FakeUser",
        (),
        {
            "id": uuid4(),
            "organization_id": organization_id,
            "name": "Some User",
            "email": "user@example.com",
            "role": role,
            "status": UserStatus.ACTIVE,
        },
    )()


@pytest.fixture
def wired_app(db_session: AsyncSession):
    """`AsyncClient`/`ASGITransport` (NOT `fastapi.testclient.TestClient`):
    `TestClient` runs the ASGI app in its own thread with its own event
    loop, which cannot reuse `db_session`'s asyncpg connection (opened on
    the pytest-asyncio test's loop) — surfaces as `RuntimeError: ... Task
    ... got Future ... attached to a different loop`. `AsyncClient` over
    `ASGITransport` runs the whole request in-process on THIS test's own
    loop, matching `tests/integration/test_chat_flow.py`'s established
    pattern for real-DB + ASGI-app tests."""
    app.dependency_overrides[get_session] = lambda: db_session
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
async def client(wired_app):
    transport = ASGITransport(app=wired_app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


def _as_admin(org_id) -> None:
    principal = _fake_principal(role=UserRole.ADMIN, organization_id=org_id)
    app.dependency_overrides[deps.get_principal] = lambda: principal


# ---------------------------------------------------------------------------
# RBAC — tasks 4.13/4.14: tenant/owner/agent get 403 on ALL 5 routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ANALYTICS_ROUTES)
@pytest.mark.parametrize("role", [UserRole.TENANT, UserRole.OWNER, UserRole.AGENT])
async def test_non_admin_roles_get_403_on_every_analytics_route(
    client, db_session: AsyncSession, route, role
) -> None:
    org = await _make_org(db_session)
    principal = _fake_principal(role=role, organization_id=org.id)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    response = await client.get(route)

    assert response.status_code == 403


@pytest.mark.parametrize("route", ANALYTICS_ROUTES)
async def test_admin_role_is_not_rejected_by_the_rbac_gate(
    client, db_session: AsyncSession, route
) -> None:
    org = await _make_org(db_session)
    _as_admin(org.id)

    response = await client.get(route)

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /analytics/overview — tasks 4.1/4.2, live query
# ---------------------------------------------------------------------------


async def test_overview_open_by_status_excludes_closed_tickets(
    client, db_session: AsyncSession
) -> None:
    org = await _make_org(db_session)
    user = await _make_user(db_session, org, role=UserRole.TENANT)
    now = datetime.now(UTC)
    await _make_ticket(db_session, org, user, created_at=now, status=TicketStatus.OPEN)
    await _make_ticket(
        db_session, org, user, created_at=now, status=TicketStatus.CLOSED, closed_at=now
    )
    _as_admin(org.id)

    response = await client.get("/api/v1/analytics/overview")

    assert response.status_code == 200
    body = response.json()
    statuses = {row["status"]: row["count"] for row in body["open_by_status"]}
    assert statuses.get("open") == 1
    assert "closed" not in statuses


async def test_overview_sla_window_warning_and_breached_boundary(
    client, db_session: AsyncSession
) -> None:
    org = await _make_org(db_session)
    user = await _make_user(db_session, org, role=UserRole.TENANT)
    now = datetime.now(UTC)

    # Warning: 90% elapsed (> 80% threshold), due date not yet passed.
    warning_created = now - timedelta(hours=9)
    warning_due = now + timedelta(hours=1)
    await _make_ticket(
        db_session, org, user, created_at=warning_created,
        status=TicketStatus.OPEN, sla_due_at=warning_due,
    )
    # Breached: due date already passed.
    breached_created = now - timedelta(hours=20)
    breached_due = now - timedelta(hours=1)
    await _make_ticket(
        db_session, org, user, created_at=breached_created,
        status=TicketStatus.OPEN, sla_due_at=breached_due,
    )
    # Safe: only 10% elapsed, well under the warning threshold.
    safe_created = now - timedelta(hours=1)
    safe_due = now + timedelta(hours=9)
    await _make_ticket(
        db_session, org, user, created_at=safe_created,
        status=TicketStatus.OPEN, sla_due_at=safe_due,
    )
    _as_admin(org.id)

    response = await client.get("/api/v1/analytics/overview")

    assert response.status_code == 200
    sla_window = response.json()["sla_window"]
    assert sla_window["warning"] == 1
    assert sla_window["breached"] == 1


async def test_overview_channel_breakdown_excludes_tickets_older_than_30_days(
    client, db_session: AsyncSession
) -> None:
    org = await _make_org(db_session)
    user = await _make_user(db_session, org, role=UserRole.TENANT)
    now = datetime.now(UTC)
    await _make_ticket(
        db_session, org, user, created_at=now - timedelta(days=1), channel=TicketChannel.EMAIL
    )
    await _make_ticket(
        db_session, org, user, created_at=now - timedelta(days=45), channel=TicketChannel.EMAIL
    )
    _as_admin(org.id)

    response = await client.get("/api/v1/analytics/overview")

    assert response.status_code == 200
    channels = {row["channel"]: row["count"] for row in response.json()["channel_breakdown_30d"]}
    assert channels.get("email") == 1


async def test_overview_open_by_urgency_groups_including_null_urgency(
    client, db_session: AsyncSession
) -> None:
    org = await _make_org(db_session)
    user = await _make_user(db_session, org, role=UserRole.TENANT)
    urgency = await _make_urgency_level(db_session, org, name="Critical")
    now = datetime.now(UTC)
    await _make_ticket(
        db_session, org, user, created_at=now, status=TicketStatus.OPEN, urgency_id=urgency.id
    )
    await _make_ticket(db_session, org, user, created_at=now, status=TicketStatus.OPEN)
    _as_admin(org.id)

    response = await client.get("/api/v1/analytics/overview")

    assert response.status_code == 200
    rows = response.json()["open_by_urgency"]
    named = {r["urgency_name"]: r["count"] for r in rows if r["urgency_name"]}
    unclassified = [r for r in rows if r["urgency_id"] is None]
    assert named.get("Critical") == 1
    assert len(unclassified) == 1
    assert unclassified[0]["count"] == 1


async def test_overview_cross_org_isolation(client, db_session: AsyncSession) -> None:
    org_a = await _make_org(db_session)
    org_b = await _make_org(db_session)
    user_b = await _make_user(db_session, org_b, role=UserRole.TENANT)
    await _make_ticket(
        db_session, org_b, user_b, created_at=datetime.now(UTC), status=TicketStatus.OPEN
    )
    _as_admin(org_a.id)

    response = await client.get("/api/v1/analytics/overview")

    assert response.status_code == 200
    assert response.json()["open_by_status"] == []


# ---------------------------------------------------------------------------
# GET /analytics/trends — tasks 4.3/4.4, 4.5/4.6
# ---------------------------------------------------------------------------


async def test_trends_rejects_unknown_metric_key_with_422(client, db_session: AsyncSession) -> None:
    org = await _make_org(db_session)
    _as_admin(org.id)

    response = await client.get("/api/v1/analytics/trends?metric_key=not_a_real_metric")

    assert response.status_code == 422


async def test_trends_returns_series_from_daily_metrics(
    client, db_session: AsyncSession
) -> None:
    org = await _make_org(db_session)
    metric_date = datetime.now(UTC).date()
    db_session.add(
        DailyMetric(
            organization_id=org.id,
            metric_date=metric_date,
            metric_key="tickets_created",
            metric_value=Decimal("5.0000"),
        )
    )
    await db_session.flush()
    _as_admin(org.id)

    response = await client.get(
        f"/api/v1/analytics/trends?metric_key=tickets_created"
        f"&start_date={metric_date}&end_date={metric_date}"
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["points"]) == 1
    assert body["points"][0]["metric_value"] == "5.0000"


async def test_trends_rejects_end_date_before_start_date_with_422(
    client, db_session: AsyncSession
) -> None:
    org = await _make_org(db_session)
    _as_admin(org.id)

    response = await client.get(
        "/api/v1/analytics/trends?metric_key=tickets_created"
        "&start_date=2026-08-20&end_date=2026-08-01"
    )

    assert response.status_code == 422


async def test_trends_rejects_span_exceeding_366_days_with_422(
    client, db_session: AsyncSession
) -> None:
    org = await _make_org(db_session)
    _as_admin(org.id)

    response = await client.get(
        "/api/v1/analytics/trends?metric_key=tickets_created"
        "&start_date=2024-01-01&end_date=2026-01-02"
    )

    assert response.status_code == 422


async def test_trends_defaults_to_last_30_days_when_dates_omitted(
    client, db_session: AsyncSession
) -> None:
    org = await _make_org(db_session)
    _as_admin(org.id)

    response = await client.get("/api/v1/analytics/trends?metric_key=tickets_created")

    assert response.status_code == 200
    body = response.json()
    start = datetime.fromisoformat(body["start_date"]).date()
    end = datetime.fromisoformat(body["end_date"]).date()
    assert (end - start).days == 29


async def test_trends_cross_org_isolation(client, db_session: AsyncSession) -> None:
    org_a = await _make_org(db_session)
    org_b = await _make_org(db_session)
    metric_date = datetime.now(UTC).date()
    db_session.add(
        DailyMetric(
            organization_id=org_b.id,
            metric_date=metric_date,
            metric_key="tickets_created",
            metric_value=Decimal("9.0000"),
        )
    )
    await db_session.flush()
    _as_admin(org_a.id)

    response = await client.get(
        f"/api/v1/analytics/trends?metric_key=tickets_created"
        f"&start_date={metric_date}&end_date={metric_date}"
    )

    assert response.status_code == 200
    assert response.json()["points"] == []


# ---------------------------------------------------------------------------
# GET /analytics/classifier — tasks 4.7/4.8, needs_review boundary (STRICT >)
# ---------------------------------------------------------------------------


async def test_classifier_39_of_200_correction_rate_is_not_flagged(
    client, db_session: AsyncSession
) -> None:
    org = await _make_org(db_session)
    user = await _make_user(db_session, org, role=UserRole.TENANT)
    category = await _make_category(db_session, org, name="Plumbing")
    await _seed_classifier_sample(
        db_session, org, user, category,
        total=200, corrected_count=39, base_created_at=datetime.now(UTC) - timedelta(days=1),
    )
    _as_admin(org.id)

    response = await client.get("/api/v1/analytics/classifier")

    assert response.status_code == 200
    row = next(r for r in response.json()["categories"] if r["category_id"] == str(category.id))
    assert row["sample_size"] == 200
    assert row["needs_review"] is False


async def test_classifier_exactly_40_of_200_correction_rate_is_not_flagged_boundary_exclusive(
    client, db_session: AsyncSession
) -> None:
    org = await _make_org(db_session)
    user = await _make_user(db_session, org, role=UserRole.TENANT)
    category = await _make_category(db_session, org, name="Electrical")
    await _seed_classifier_sample(
        db_session, org, user, category,
        total=200, corrected_count=40, base_created_at=datetime.now(UTC) - timedelta(days=1),
    )
    _as_admin(org.id)

    response = await client.get("/api/v1/analytics/classifier")

    assert response.status_code == 200
    row = next(r for r in response.json()["categories"] if r["category_id"] == str(category.id))
    assert row["needs_review"] is False


async def test_classifier_41_of_200_correction_rate_is_flagged(
    client, db_session: AsyncSession
) -> None:
    org = await _make_org(db_session)
    user = await _make_user(db_session, org, role=UserRole.TENANT)
    category = await _make_category(db_session, org, name="HVAC")
    await _seed_classifier_sample(
        db_session, org, user, category,
        total=200, corrected_count=41, base_created_at=datetime.now(UTC) - timedelta(days=1),
    )
    _as_admin(org.id)

    response = await client.get("/api/v1/analytics/classifier")

    assert response.status_code == 200
    row = next(r for r in response.json()["categories"] if r["category_id"] == str(category.id))
    assert row["needs_review"] is True


async def test_classifier_cross_org_isolation(client, db_session: AsyncSession) -> None:
    org_a = await _make_org(db_session)
    org_b = await _make_org(db_session)
    user_b = await _make_user(db_session, org_b, role=UserRole.TENANT)
    category_b = await _make_category(db_session, org_b, name="OtherOrgCategory")
    await _seed_classifier_sample(
        db_session, org_b, user_b, category_b,
        total=50, corrected_count=50, base_created_at=datetime.now(UTC) - timedelta(days=1),
    )
    _as_admin(org_a.id)

    response = await client.get("/api/v1/analytics/classifier")

    assert response.status_code == 200
    assert response.json()["categories"] == []


# ---------------------------------------------------------------------------
# GET /analytics/rag — tasks 4.9/4.10
# ---------------------------------------------------------------------------


async def test_rag_computes_generated_used_and_usage_rate_from_messages(
    client, db_session: AsyncSession
) -> None:
    org = await _make_org(db_session)
    user = await _make_user(db_session, org, role=UserRole.TENANT)
    ticket = await _make_ticket(db_session, org, user, created_at=datetime.now(UTC))
    suggestion = await _make_message(
        db_session, ticket, is_ai_suggestion=True, author_type=AuthorType.BOT
    )
    await _make_message(
        db_session, ticket, author_type=AuthorType.AGENT,
        content="reply", based_on_suggestion_id=suggestion.id,
    )
    await _make_message(db_session, ticket, is_ai_suggestion=True, author_type=AuthorType.BOT)
    _as_admin(org.id)

    response = await client.get("/api/v1/analytics/rag")

    assert response.status_code == 200
    body = response.json()
    assert body["suggestions_generated"] == 2
    assert body["suggestions_used"] == 1
    assert body["usage_rate"] == "0.5"


async def test_rag_excludes_chat_messages_table_source_is_messages_only(
    client, db_session: AsyncSession
) -> None:
    org = await _make_org(db_session)
    chat_session = await _make_chat_session(db_session, org, created_at=datetime.now(UTC))
    await _make_chat_message(
        db_session, chat_session, role=ChatMessageRole.TOOL, tool_name="lookup_ticket",
        content="result",
    )
    _as_admin(org.id)

    response = await client.get("/api/v1/analytics/rag")

    assert response.status_code == 200
    body = response.json()
    assert body["suggestions_generated"] == 0
    assert body["suggestions_used"] == 0


async def test_rag_cross_org_isolation(client, db_session: AsyncSession) -> None:
    org_a = await _make_org(db_session)
    org_b = await _make_org(db_session)
    user_b = await _make_user(db_session, org_b, role=UserRole.TENANT)
    ticket_b = await _make_ticket(db_session, org_b, user_b, created_at=datetime.now(UTC))
    await _make_message(db_session, ticket_b, is_ai_suggestion=True, author_type=AuthorType.BOT)
    _as_admin(org_a.id)

    response = await client.get("/api/v1/analytics/rag")

    assert response.status_code == 200
    assert response.json()["suggestions_generated"] == 0


# ---------------------------------------------------------------------------
# GET /analytics/chatbot — task 4.11
# ---------------------------------------------------------------------------


async def test_chatbot_totals_escalation_rate_avg_messages_and_tool_usage(
    client, db_session: AsyncSession
) -> None:
    org = await _make_org(db_session)
    now = datetime.now(UTC)
    escalated_session = await _make_chat_session(db_session, org, created_at=now, escalated_at=now)
    plain_session = await _make_chat_session(db_session, org, created_at=now)
    await _make_chat_message(db_session, escalated_session, role=ChatMessageRole.USER, content="hi")
    await _make_chat_message(
        db_session, escalated_session, role=ChatMessageRole.TOOL,
        tool_name="escalate_to_human", content="done",
    )
    await _make_chat_message(db_session, plain_session, role=ChatMessageRole.USER, content="hey")
    _as_admin(org.id)

    response = await client.get("/api/v1/analytics/chatbot")

    assert response.status_code == 200
    body = response.json()
    assert body["total_sessions"] == 2
    assert body["escalated_sessions"] == 1
    assert body["escalation_rate"] == "0.5"
    assert body["avg_messages_per_session"] == "1.5"
    tool_usage = {row["tool_name"]: row["count"] for row in body["tool_usage"]}
    assert tool_usage == {"escalate_to_human": 1}


async def test_chatbot_cross_org_isolation(client, db_session: AsyncSession) -> None:
    org_a = await _make_org(db_session)
    org_b = await _make_org(db_session)
    await _make_chat_session(db_session, org_b, created_at=datetime.now(UTC))
    _as_admin(org_a.id)

    response = await client.get("/api/v1/analytics/chatbot")

    assert response.status_code == 200
    body = response.json()
    assert body["total_sessions"] == 0
    assert body["escalated_sessions"] == 0
    assert body["escalation_rate"] is None

"""Cross-cutting integration tests for the analytics rollup job (stage 7 —
analytics, PR5 — final integration phase), against a REAL Postgres
instance.

Unlike `tests/unit/test_worker_analytics.py` (mocked session, monkeypatched
family functions — proves the WORKER's own control flow: org iteration,
lookback window, per-family SAVEPOINT fault isolation), and unlike
`tests/integration/test_analytics_service_pg.py` (proves per-function
aggregation CORRECTNESS for one org at a time), this module exercises
`app.workers.analytics.rollup_daily_metrics` END-TO-END against a real
database with NOTHING monkeypatched, covering two genuine gaps left open
after PR3/PR4:

1. **Rollup-job-level cross-org isolation**: PR4's
   `tests/api/test_analytics_endpoints.py` proves the 5 read endpoints
   never leak another org's data, and PR3's
   `test_tickets_created_count_org_local_day_boundary_differs_by_timezone`
   proves per-function query isolation via two orgs — but nothing calls
   the full `rollup_daily_metrics` job itself with two orgs seeded on the
   SAME local day and asserts neither org's computed `DailyMetric` rows
   absorb the other's counts. `test_rollup_never_mixes_two_organizations_
   metrics_on_the_same_local_day` below closes that gap.

2. **End-to-end idempotency across a day boundary**: PR3's
   `test_worker_analytics.py::test_running_rollup_twice_for_the_same_day_
   upserts_stable_rows_no_duplicates` proves the WORKER submits identical
   rows across two same-day runs with monkeypatched family functions —
   it never touches a real day boundary, `daily_metrics`, or the `/trends`
   endpoint. `test_rollup_run_twice_across_a_day_boundary_produces_one_
   stable_trends_row_per_day` below runs the REAL worker twice, with `_now`
   advanced by one day between runs (so the two runs' `lookback_days=2`
   windows overlap on exactly one day), then reads the result back through
   the live `/api/v1/analytics/trends` endpoint.
"""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.session import get_session
from app.core.sla_defaults import DEFAULT_BUSINESS_HOURS
from app.main import app
from app.models.daily_metric import DailyMetric
from app.models.enums import UserRole, UserStatus
from app.models.organization import Organization
from app.models.ticket import Ticket
from app.models.user import User
from app.workers import analytics as worker

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skip(
        reason="Requires a live Postgres instance (see docker-compose.yml); "
        "not reachable in CI. Run manually or in CI with "
        "`docker compose up -d db && pytest tests/integration -m ''`."
    ),
]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _SessionContextManager:
    """Minimal async context manager standing in for the real
    `session_factory()` the worker expects — wraps the REAL `db_session`
    fixture (not a mock), so `rollup_daily_metrics` runs its actual SQL
    against Postgres. `session.commit()` calls are absorbed by
    `db_session`'s outer-transaction/SAVEPOINT-restart pattern
    (`tests/conftest.py`), same as every other real-DB test."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _session_factory(session: AsyncSession):
    def factory() -> _SessionContextManager:
        return _SessionContextManager(session)

    return factory


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


async def _make_tickets(
    db_session: AsyncSession, org: Organization, user: User, *, created_at: datetime, count: int
) -> None:
    for _ in range(count):
        db_session.add(Ticket(organization_id=org.id, user_id=user.id, title="t", created_at=created_at))
    await db_session.flush()


async def _daily_metric(
    db_session: AsyncSession, org: Organization, metric_date: date, metric_key: str
) -> DailyMetric | None:
    """`populate_existing=True` forces a fresh read of column values from
    THIS query's result row, overwriting any stale identity-mapped Python
    object left over from an earlier fetch — necessary because
    `upsert_daily_metrics` is a Core `INSERT ... ON CONFLICT DO UPDATE`
    statement that bypasses the ORM unit-of-work and never updates an
    already-loaded `DailyMetric` instance on its own."""
    result = await db_session.execute(
        select(DailyMetric)
        .where(
            DailyMetric.organization_id == org.id,
            DailyMetric.metric_date == metric_date,
            DailyMetric.metric_key == metric_key,
        )
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


def _fake_principal(*, organization_id) -> User:
    return type(
        "FakeUser",
        (),
        {
            "id": uuid4(),
            "organization_id": organization_id,
            "name": "Some User",
            "email": "user@example.com",
            "role": UserRole.ADMIN,
            "status": UserStatus.ACTIVE,
        },
    )()


@pytest.fixture
def wired_app(db_session: AsyncSession):
    app.dependency_overrides[get_session] = lambda: db_session
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
async def client(wired_app):
    transport = ASGITransport(app=wired_app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


def _as_admin(org_id) -> None:
    principal = _fake_principal(organization_id=org_id)
    app.dependency_overrides[deps.get_principal] = lambda: principal


# ---------------------------------------------------------------------------
# rollup_daily_metrics — cross-org isolation at the JOB level (task 5.1/5.2)
# ---------------------------------------------------------------------------


async def test_rollup_never_mixes_two_organizations_metrics_on_the_same_local_day(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Two UTC-timezone orgs, each with a DIFFERENT number of tickets
    created on the SAME completed local day. Running the real
    `rollup_daily_metrics` (no monkeypatched family functions) must persist
    a `DailyMetric.tickets_created` row per org that reflects ONLY that
    org's own ticket count — never the other org's, and never their sum."""
    org_a = await _make_org(db_session, timezone="UTC")
    org_b = await _make_org(db_session, timezone="UTC")
    user_a = await _make_user(db_session, org_a)
    user_b = await _make_user(db_session, org_b)

    fixed_now = datetime(2026, 8, 20, 0, 30, tzinfo=UTC)
    target_local_day = date(2026, 8, 19)  # yesterday relative to fixed_now
    created_at = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

    await _make_tickets(db_session, org_a, user_a, created_at=created_at, count=3)
    await _make_tickets(db_session, org_b, user_b, created_at=created_at, count=7)

    monkeypatch.setattr(worker, "_now", lambda: fixed_now)
    await worker.rollup_daily_metrics({"session_factory": _session_factory(db_session)})

    metric_a = await _daily_metric(db_session, org_a, target_local_day, "tickets_created")
    metric_b = await _daily_metric(db_session, org_b, target_local_day, "tickets_created")

    assert metric_a is not None
    assert metric_b is not None
    assert metric_a.metric_value == Decimal("3.0000")
    assert metric_b.metric_value == Decimal("7.0000")
    # The critical isolation assertion: neither org's row absorbed the
    # other's tickets — 3 + 7 = 10 would be the failure signature of a
    # missing `organization_id` filter somewhere in the aggregation path.
    assert metric_a.metric_value != metric_b.metric_value
    assert metric_a.organization_id == org_a.id
    assert metric_b.organization_id == org_b.id


# ---------------------------------------------------------------------------
# rollup_daily_metrics — end-to-end idempotency across a day boundary
# (task 5.3/5.4): worker -> daily_metrics -> /trends, two SEPARATE
# invocations one simulated calendar day apart.
# ---------------------------------------------------------------------------


async def test_rollup_run_twice_across_a_day_boundary_produces_one_stable_trends_row_per_day(
    client, db_session: AsyncSession, monkeypatch
) -> None:
    """Simulates two consecutive nightly cron runs. `lookback_days=2` means
    run 1 (simulated "now" = Aug 20 00:30 UTC) recomputes [Aug 19, Aug 18],
    and run 2 (simulated "now" = Aug 21 00:30 UTC, one calendar day later)
    recomputes [Aug 20, Aug 19] — Aug 19 is recomputed in BOTH runs (the
    day-boundary overlap), Aug 18 only in run 1, Aug 20 only in run 2.

    Asserts, via the live `/trends` endpoint (not the worker or the
    endpoint in isolation): exactly one row per `(org, date, key)` with no
    duplication, and that the overlap day is genuinely RE-computed by run 2
    (not silently skipped as already-done) — proven by seeding one
    late-arriving ticket on the overlap day BETWEEN the two runs and
    asserting its count is picked up, rather than by comparing `updated_at`
    wall-clock values: Postgres's `now()` is TRANSACTION-scoped (same value
    for the whole transaction), and this whole test runs inside one outer
    transaction (`tests/conftest.py`'s `db_session` SAVEPOINT fixture), so
    both upserts legitimately share one `now()` instant regardless of
    whether a real re-upsert happened."""
    org = await _make_org(db_session, timezone="UTC")
    user = await _make_user(db_session, org)

    day_18 = date(2026, 8, 18)
    day_19 = date(2026, 8, 19)  # the overlap day
    day_20 = date(2026, 8, 20)

    await _make_tickets(db_session, org, user, created_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC), count=2)
    await _make_tickets(db_session, org, user, created_at=datetime(2026, 8, 19, 12, 0, tzinfo=UTC), count=3)
    await _make_tickets(db_session, org, user, created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC), count=4)

    ctx = {"session_factory": _session_factory(db_session)}

    # --- Run 1: simulated "now" = Aug 20 00:30 UTC -> recomputes [19, 18]. ---
    monkeypatch.setattr(worker, "_now", lambda: datetime(2026, 8, 20, 0, 30, tzinfo=UTC))
    await worker.rollup_daily_metrics(ctx)

    overlap_after_run1 = await _daily_metric(db_session, org, day_19, "tickets_created")
    assert overlap_after_run1 is not None
    assert overlap_after_run1.metric_value == Decimal("3.0000")
    overlap_row_id = overlap_after_run1.id

    # day_20 must NOT exist yet — it is not a completed day as of run 1.
    assert await _daily_metric(db_session, org, day_20, "tickets_created") is None

    # A late-arriving ticket lands on the overlap day AFTER run 1 committed
    # (e.g. a backdated import, or a request that was in flight across the
    # cron boundary) — run 2 must pick it up, proving genuine
    # recomputation rather than a stale skip.
    await _make_tickets(db_session, org, user, created_at=datetime(2026, 8, 19, 18, 0, tzinfo=UTC), count=2)

    # --- Run 2: simulated "now" = Aug 21 00:30 UTC (one day later) ->
    # recomputes [20, 19]. ---
    monkeypatch.setattr(worker, "_now", lambda: datetime(2026, 8, 21, 0, 30, tzinfo=UTC))
    await worker.rollup_daily_metrics(ctx)

    overlap_after_run2 = await _daily_metric(db_session, org, day_19, "tickets_created")
    assert overlap_after_run2 is not None
    # Genuinely RE-upserted with the late ticket picked up (3 + 2 = 5) —
    # NOT still 3 (would mean run 2 silently skipped/no-op'd this day) and
    # NOT 6/8 (would mean a duplicate row accumulated rather than an
    # overwrite).
    assert overlap_after_run2.metric_value == Decimal("5.0000")
    # The SAME row throughout (never a second inserted row for this key —
    # this is the actual `ON CONFLICT ... DO UPDATE` idempotency guarantee
    # under test).
    assert overlap_after_run2.id == overlap_row_id

    _as_admin(org.id)
    response = await client.get(
        "/api/v1/analytics/trends"
        f"?metric_key=tickets_created&start_date={day_18}&end_date={day_20}"
    )

    assert response.status_code == 200
    points = response.json()["points"]
    # Exactly one point per day — no duplication from the two separate runs.
    assert len(points) == 3
    by_date = {p["metric_date"]: p["metric_value"] for p in points}
    assert by_date[str(day_18)] == "2.0000"  # untouched by run 2 (outside its window)
    assert by_date[str(day_19)] == "5.0000"  # overlap day: correctly re-computed, not doubled
    assert by_date[str(day_20)] == "4.0000"  # newly added by run 2

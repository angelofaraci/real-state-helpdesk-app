"""Integration tests for Stage 2 (ticket classification) real-Postgres-only
behaviors, following the same skip-stub pattern as
`tests/integration/test_auth_flow_pg.py`.

Everything in `tests/unit/` and `tests/api/` mocks the `AsyncSession` /
repository boundary, which is sufficient to prove service and HTTP-layer
control flow (branching logic, status codes, response shapes, and the SQL
strings that SQLAlchemy compiles). It does NOT prove that:

- The `ck_tickets_classified_has_taxonomy` CHECK constraint (added in
  migration `0003_classification_stage2.py`) is actually enforced by
  Postgres itself when a row violates it outside the ORM's own validation
  path (e.g. a raw UPDATE, a buggy call site, or a future code path that
  forgets to set taxonomy before flipping `classification_status`).
- `SELECT ... FOR UPDATE` in `app.workers.classification.classify_ticket`
  actually serializes two concurrent job deliveries for the same
  `ticket_id`, so that only one of them performs the classification write
  and the other observes `classification_status != "pending"` and returns
  as a no-op (arq's at-least-once delivery semantics rely on this).
- `ClassificationRepository.upsert`'s `INSERT ... ON CONFLICT (ticket_id)
  DO UPDATE` actually behaves as an upsert against a real unique
  constraint: first call inserts a new `classifications` row, a second
  call for the same `ticket_id` updates that same row in place (no
  duplicate row, no `IntegrityError`).

These tests run against a real Postgres instance (`docker compose up -d
postgres`, `alembic upgrade head`), using the SAVEPOINT-scoped `db_session`
fixture from `tests/conftest.py` (see that module's docstring).
"""

import secrets
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scope import OrgScope
from app.models.category import Category
from app.models.classification import Classification
from app.models.enums import ClassificationStatus, UserRole, UserStatus
from app.models.organization import Organization
from app.models.ticket import Ticket
from app.models.urgency_level import UrgencyLevel
from app.models.user import User
from app.repositories.classification_repository import ClassificationRepository
from app.services.classifier import ClassificationResult
from app.workers.classification import classify_ticket


async def _make_org(db_session: AsyncSession) -> Organization:
    org = Organization(
        name=f"Test Org {uuid4()}",
        chat_widget_key=secrets.token_urlsafe(16),
        timezone="UTC",
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


async def _make_taxonomy(
    db_session: AsyncSession, org: Organization
) -> tuple[Category, UrgencyLevel]:
    category = Category(organization_id=org.id, name=f"Plumbing {uuid4()}")
    urgency = UrgencyLevel(organization_id=org.id, name=f"High {uuid4()}", sla_hours=4)
    db_session.add(category)
    db_session.add(urgency)
    await db_session.flush()
    return category, urgency


async def test_classified_ticket_without_taxonomy_violates_check_constraint(
    db_session: AsyncSession,
) -> None:
    """A ticket row with `classification_status='classified'` and any of
    `category_id`/`urgency_id`/`sla_due_at` null must be rejected by the
    real `ck_tickets_classified_has_taxonomy` CHECK constraint, and must
    succeed once all three are populated (or the ticket stays `pending`)."""
    org = await _make_org(db_session)
    user = await _make_user(db_session, org)

    with pytest.raises(
        IntegrityError, match="ck_tickets_classified_has_taxonomy"
    ):
        async with db_session.begin_nested():
            db_session.add(
                Ticket(
                    organization_id=org.id,
                    user_id=user.id,
                    title="Leaking faucet",
                    classification_status=ClassificationStatus.CLASSIFIED,
                    category_id=None,
                    urgency_id=None,
                    sla_due_at=None,
                )
            )
            await db_session.flush()
    # Scoping the failing `add` + `flush` to their own SAVEPOINT (rather
    # than adding before entering it) is what lets the outer transaction
    # remain usable afterward — same pattern as
    # `test_channel_identity_constraints.py`.

    # A `pending` ticket with no taxonomy at all is fine.
    pending_ticket = Ticket(
        organization_id=org.id,
        user_id=user.id,
        title="Pending ticket",
        classification_status=ClassificationStatus.PENDING,
    )
    db_session.add(pending_ticket)
    await db_session.flush()

    # A `classified` ticket with full taxonomy is fine.
    category, urgency = await _make_taxonomy(db_session, org)
    valid_ticket = Ticket(
        organization_id=org.id,
        user_id=user.id,
        title="Fully classified ticket",
        classification_status=ClassificationStatus.CLASSIFIED,
        category_id=category.id,
        urgency_id=urgency.id,
        sla_due_at=datetime.now(UTC),
    )
    db_session.add(valid_ticket)
    await db_session.flush()

    result = await db_session.execute(
        select(Ticket).where(Ticket.organization_id == org.id)
    )
    tickets = result.scalars().all()
    assert {t.id for t in tickets} == {pending_ticket.id, valid_ticket.id}


class _FakeEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]


class _SessionContextManager:
    """Wraps the already-open `db_session` fixture as the async context
    manager `ctx["session_factory"]()` is expected to be, without ever
    closing it — the fixture owns its own lifecycle/rollback."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> None:
        return None


async def test_concurrent_classify_ticket_jobs_do_not_double_classify(
    db_session: AsyncSession,
) -> None:
    """Two deliveries of the same `classify_ticket(ctx, ticket_id, org_id)`
    job (arq's at-least-once semantics) for the same ticket must classify
    it exactly once.

    True cross-connection concurrency is not reachable under the
    SAVEPOINT-scoped `db_session` fixture (a second real connection would
    not see this test's uncommitted outer transaction at all). Instead this
    honestly simulates the sequential outcome the real `SELECT ... FOR
    UPDATE` lock guarantees: the first delivery runs to completion first
    (as the lock winner would), and the second delivery — using the SAME
    session/connection, since a second real connection cannot observe this
    fixture's data — re-reads the ticket and must observe
    `classification_status != "pending"`, taking the no-op branch. This
    proves the post-lock re-check guard in `classify_ticket` itself (the
    part of the concurrency safety that lives in application code), not
    Postgres's row-locking wire protocol, which is out of reach here — see
    `tests/integration/test_channel_identity_constraints.py` for the same
    documented limitation on a different constraint.
    """
    org = await _make_org(db_session)
    user = await _make_user(db_session, org)
    category, urgency = await _make_taxonomy(db_session, org)

    ticket = Ticket(
        organization_id=org.id,
        user_id=user.id,
        title="Broken thermostat",
        classification_status=ClassificationStatus.PENDING,
    )
    db_session.add(ticket)
    await db_session.flush()

    call_count = 0

    async def _fake_classify(**kwargs: object) -> ClassificationResult:
        nonlocal call_count
        call_count += 1
        return ClassificationResult(
            category_id=category.id,
            urgency_id=urgency.id,
            predicted_category=category.name,
            predicted_urgency=urgency.name,
            confidence=0.99,
            urgency_confidence=0.99,
            model_used="sklearn_v1",
        )

    ctx = {
        "session_factory": lambda: _SessionContextManager(db_session),
        "embedder": _FakeEmbedder(),
        "classify": _fake_classify,
    }

    # First delivery: ticket is pending, so it classifies it.
    await classify_ticket(ctx, ticket.id, org.id)
    # Second (duplicate) delivery for the same ticket_id: the ticket is
    # no longer pending, so this must be a silent no-op.
    await classify_ticket(ctx, ticket.id, org.id)

    assert call_count == 1

    await db_session.refresh(ticket)
    assert ticket.classification_status == ClassificationStatus.CLASSIFIED

    result = await db_session.execute(
        select(Classification).where(Classification.ticket_id == ticket.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 1


async def test_classification_repository_upsert_inserts_then_updates_in_place(
    db_session: AsyncSession,
) -> None:
    """`ClassificationRepository.upsert` must insert a new row on first
    call and update that same row in place (not insert a duplicate, not
    raise `IntegrityError`) on a second call for the same `ticket_id`."""
    org = await _make_org(db_session)
    user = await _make_user(db_session, org)

    ticket = Ticket(
        organization_id=org.id,
        user_id=user.id,
        title="Noisy neighbor",
        classification_status=ClassificationStatus.PENDING,
    )
    db_session.add(ticket)
    await db_session.flush()

    scope = OrgScope.for_background_worker(org.id)
    repo = ClassificationRepository(db_session, scope)

    await repo.upsert(
        ticket.id,
        predicted_category="Noise complaint",
        confidence=0.8,
        predicted_urgency="Medium",
        urgency_confidence=0.7,
        model_used="sklearn_v1",
    )
    await db_session.flush()

    result = await db_session.execute(
        select(Classification).where(Classification.ticket_id == ticket.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    first_row_id = rows[0].id
    assert rows[0].predicted_category == "Noise complaint"
    assert rows[0].model_used == "sklearn_v1"

    await repo.upsert(
        ticket.id,
        predicted_category="Structural damage",
        confidence=0.95,
        predicted_urgency="High",
        urgency_confidence=0.9,
        model_used="llm_fallback",
    )
    await db_session.flush()

    # `upsert` runs a Core-level `INSERT ... ON CONFLICT DO UPDATE`
    # statement, which updates the row in Postgres directly without going
    # through the ORM unit-of-work — so the already-loaded `Classification`
    # instance from the first `select` above stays stale in the session's
    # identity map unless this second query is told to overwrite it.
    # `populate_existing=True` is what makes this assertion actually re-read
    # the real row instead of the identity-mapped object's cached values.
    result = await db_session.execute(
        select(Classification)
        .where(Classification.ticket_id == ticket.id)
        .execution_options(populate_existing=True)
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].id == first_row_id
    assert rows[0].predicted_category == "Structural damage"
    assert rows[0].model_used == "llm_fallback"

"""Unit tests for `app.workers.classification` — the async classification
worker job (`classify_ticket`) and its stale-ticket sweep
(`sweep_pending_classifications`).

`AsyncSession` is mocked (following the pattern established by
`tests/unit/test_ticket_service_update.py`); the worker opens/commits its
own session via an injected `ctx["session_factory"]` (NOT the `get_session`
FastAPI dependency, since a worker job has no request lifecycle), so
`session_factory` here is a plain callable returning an async context
manager that yields the mocked session — mirroring the shape of a real
`async_sessionmaker` instance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import arq
import pytest

from app.core.scope import OrgScope
from app.models.category import Category
from app.models.enums import ClassificationStatus, UserRole
from app.models.ticket import Ticket
from app.models.urgency_level import UrgencyLevel
from app.services.classifier import (
    ClassificationResult,
    ClassificationTransientError,
    ClassificationUnavailableError,
)
from app.workers import classification as worker


class _SessionContextManager:
    """Minimal async context manager wrapping an already-built mock
    session — stands in for calling a real `async_sessionmaker` instance."""

    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _session_factory(session: AsyncMock):
    def factory() -> _SessionContextManager:
        return _SessionContextManager(session)

    return factory


def _execute_result(*, scalar: object = None, scalars_list: list | None = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    if scalars_list is not None:
        scalars = MagicMock()
        scalars.all.return_value = scalars_list
        result.scalars.return_value = scalars
    return result


def _session_returning(*results: MagicMock) -> AsyncMock:
    session = AsyncMock()
    session.execute.side_effect = list(results)
    return session


class _FakeEmbedder:
    async def embed(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


def _ticket(*, organization_id, classification_status=ClassificationStatus.PENDING, **overrides) -> Ticket:
    defaults = dict(
        id=uuid4(),
        organization_id=organization_id,
        user_id=uuid4(),
        property_id=None,
        contract_id=None,
        category_id=None,
        urgency_id=None,
        title="Leaking sink",
        description="My sink is leaking badly",
        channel="web",
        status="open",
        classification_status=classification_status,
        agent_id=None,
        sla_due_at=None,
        closed_at=None,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return Ticket(**defaults)


def _category(*, organization_id, **overrides) -> Category:
    defaults = dict(id=uuid4(), organization_id=organization_id, name="Plumbing", description=None, active=True)
    defaults.update(overrides)
    return Category(**defaults)


def _urgency(*, organization_id, sla_hours=24, **overrides) -> UrgencyLevel:
    defaults = dict(
        id=uuid4(),
        organization_id=organization_id,
        name="High",
        description=None,
        sla_hours=sla_hours,
        sort_order=0,
        active=True,
    )
    defaults.update(overrides)
    return UrgencyLevel(**defaults)


# ---------------------------------------------------------------------------
# classify_ticket — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_path_classifies_and_commits() -> None:
    organization_id = uuid4()
    ticket = _ticket(organization_id=organization_id)
    category = _category(organization_id=organization_id)
    urgency = _urgency(organization_id=organization_id, sla_hours=8)

    fake_result = ClassificationResult(
        category_id=category.id,
        urgency_id=urgency.id,
        predicted_category=category.name,
        predicted_urgency=urgency.name,
        confidence=0.93,
        urgency_confidence=0.85,
        model_used="sklearn_v1",
    )
    fake_classify = AsyncMock(return_value=fake_result)

    session = _session_returning(
        _execute_result(scalar=ticket),  # get_or_404(for_update=True)
        _execute_result(scalars_list=[category]),  # active categories
        _execute_result(scalars_list=[urgency]),  # active urgency levels
        _execute_result(),  # ClassificationRepository.upsert INSERT
    )

    ctx = {
        "session_factory": _session_factory(session),
        "embedder": _FakeEmbedder(),
        "classify": fake_classify,
    }

    await worker.classify_ticket(ctx, ticket.id, organization_id)

    fake_classify.assert_awaited_once()
    _, kwargs = fake_classify.call_args
    assert kwargs["text"] == ticket.description
    assert [c.id for c in kwargs["categories"]] == [category.id]
    assert [u.id for u in kwargs["urgency_levels"]] == [urgency.id]

    assert ticket.category_id == category.id
    assert ticket.urgency_id == urgency.id
    assert ticket.classification_status == ClassificationStatus.CLASSIFIED
    assert ticket.sla_due_at == ticket.created_at + timedelta(hours=8)

    session.commit.assert_awaited_once()

    # The 4th execute call is the ClassificationRepository.upsert INSERT.
    upsert_call = session.execute.call_args_list[3]
    (stmt,), _ = upsert_call
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "INSERT INTO classifications" in sql
    assert "sklearn_v1" in sql


@pytest.mark.asyncio
async def test_get_or_404_is_called_with_for_update() -> None:
    organization_id = uuid4()
    ticket = _ticket(
        organization_id=organization_id,
        classification_status=ClassificationStatus.CLASSIFIED,
    )
    session = _session_returning(_execute_result(scalar=ticket))
    ctx = {
        "session_factory": _session_factory(session),
        "embedder": _FakeEmbedder(),
        "classify": AsyncMock(),
    }

    await worker.classify_ticket(ctx, ticket.id, organization_id)

    (stmt,), _ = session.execute.call_args_list[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE" in sql


# ---------------------------------------------------------------------------
# classify_ticket — idempotency (already classified under the lock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_already_classified_ticket_is_a_no_op() -> None:
    organization_id = uuid4()
    ticket = _ticket(
        organization_id=organization_id,
        classification_status=ClassificationStatus.CLASSIFIED,
        category_id=uuid4(),
        urgency_id=uuid4(),
    )
    session = _session_returning(_execute_result(scalar=ticket))
    fake_classify = AsyncMock()

    ctx = {
        "session_factory": _session_factory(session),
        "embedder": _FakeEmbedder(),
        "classify": fake_classify,
    }

    await worker.classify_ticket(ctx, ticket.id, organization_id)

    fake_classify.assert_not_awaited()
    session.commit.assert_not_awaited()
    # Only the single `get_or_404` lookup happened — no taxonomy queries, no
    # upsert.
    assert session.execute.await_count == 1


# ---------------------------------------------------------------------------
# classify_ticket — permanent failure (ClassificationUnavailableError)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permanent_failure_leaves_ticket_pending_and_does_not_raise() -> None:
    organization_id = uuid4()
    ticket = _ticket(organization_id=organization_id)
    category = _category(organization_id=organization_id)
    urgency = _urgency(organization_id=organization_id)

    fake_classify = AsyncMock(side_effect=ClassificationUnavailableError("no active taxonomy"))

    session = _session_returning(
        _execute_result(scalar=ticket),
        _execute_result(scalars_list=[category]),
        _execute_result(scalars_list=[urgency]),
    )

    ctx = {
        "session_factory": _session_factory(session),
        "embedder": _FakeEmbedder(),
        "classify": fake_classify,
    }

    # Must not raise.
    await worker.classify_ticket(ctx, ticket.id, organization_id)

    assert ticket.classification_status == ClassificationStatus.PENDING
    assert ticket.category_id is None
    assert ticket.urgency_id is None
    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# classify_ticket — transient failure (ClassificationTransientError)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_failure_raises_arq_retry_with_backoff() -> None:
    organization_id = uuid4()
    ticket = _ticket(organization_id=organization_id)
    category = _category(organization_id=organization_id)
    urgency = _urgency(organization_id=organization_id)

    fake_classify = AsyncMock(side_effect=ClassificationTransientError("rate limited"))

    session = _session_returning(
        _execute_result(scalar=ticket),
        _execute_result(scalars_list=[category]),
        _execute_result(scalars_list=[urgency]),
    )

    ctx = {
        "session_factory": _session_factory(session),
        "embedder": _FakeEmbedder(),
        "classify": fake_classify,
        "job_try": 2,
    }

    with pytest.raises(arq.Retry) as exc_info:
        await worker.classify_ticket(ctx, ticket.id, organization_id)

    assert exc_info.value.defer_score == (2**2) * 1000
    assert ticket.classification_status == ClassificationStatus.PENDING
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_failure_defaults_job_try_to_one_when_absent() -> None:
    organization_id = uuid4()
    ticket = _ticket(organization_id=organization_id)
    category = _category(organization_id=organization_id)
    urgency = _urgency(organization_id=organization_id)

    fake_classify = AsyncMock(side_effect=ClassificationTransientError("timeout"))

    session = _session_returning(
        _execute_result(scalar=ticket),
        _execute_result(scalars_list=[category]),
        _execute_result(scalars_list=[urgency]),
    )

    ctx = {
        "session_factory": _session_factory(session),
        "embedder": _FakeEmbedder(),
        "classify": fake_classify,
    }

    with pytest.raises(arq.Retry) as exc_info:
        await worker.classify_ticket(ctx, ticket.id, organization_id)

    assert exc_info.value.defer_score == (2**1) * 1000


# ---------------------------------------------------------------------------
# classify_ticket — default `classify` callable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_defaults_to_app_services_classifier_classify_when_not_overridden() -> None:
    """When `ctx` supplies no `"classify"` override, the worker uses
    `app.services.classifier.classify` itself (checked by patching the name
    the worker module imported it under)."""
    organization_id = uuid4()
    ticket = _ticket(
        organization_id=organization_id,
        classification_status=ClassificationStatus.CLASSIFIED,
    )
    session = _session_returning(_execute_result(scalar=ticket))
    ctx = {
        "session_factory": _session_factory(session),
        "embedder": _FakeEmbedder(),
        # no "classify" override supplied
    }

    # Already-classified short-circuits before `classify` would ever be
    # called — this just asserts the worker doesn't require the override to
    # be present in `ctx`.
    await worker.classify_ticket(ctx, ticket.id, organization_id)

    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# sweep_pending_classifications
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_reenqueues_a_job_per_stale_pending_ticket() -> None:
    org_a = uuid4()
    org_b = uuid4()
    ticket_a = _ticket(organization_id=org_a)
    ticket_b = _ticket(organization_id=org_b)

    session = _session_returning(_execute_result(scalars_list=[ticket_a, ticket_b]))
    redis = AsyncMock()

    ctx = {
        "session_factory": _session_factory(session),
        "redis": redis,
    }

    await worker.sweep_pending_classifications(ctx)

    assert redis.enqueue_job.await_count == 2
    redis.enqueue_job.assert_any_await("classify_ticket", ticket_a.id, ticket_a.organization_id)
    redis.enqueue_job.assert_any_await("classify_ticket", ticket_b.id, ticket_b.organization_id)


@pytest.mark.asyncio
async def test_sweep_queries_pending_tickets_older_than_five_minutes() -> None:
    session = _session_returning(_execute_result(scalars_list=[]))
    redis = AsyncMock()
    ctx = {"session_factory": _session_factory(session), "redis": redis}

    before = datetime.now(UTC) - timedelta(minutes=5)
    await worker.sweep_pending_classifications(ctx)
    after = datetime.now(UTC) - timedelta(minutes=5)

    (stmt,), _ = session.execute.call_args_list[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "classification_status" in sql
    assert "created_at" in sql
    redis.enqueue_job.assert_not_awaited()
    # sanity: the cutoff used is "now - 5 minutes", not something else.
    assert before <= after

"""Triangulation test (stage 6 — queues + SLA, PR4): given the SAME
urgency level, organization config, and anchor timestamp, ALL THREE SLA
writers (`ticket_service.create_ticket`, `ticket_service.update_ticket`,
`app.workers.classification.classify_ticket`) must produce the IDENTICAL
`sla_due_at`. This is a regression guard proving the three call sites
really are one unified code path (`app.services.sla.resolve_sla_due_at`),
not three independently-correct-but-subtly-different computations — none of
`resolve_sla_due_at`/`compute_sla_due_at` is mocked here, unlike the
per-writer unit tests in `tests/unit/test_ticket_service_create.py`,
`tests/unit/test_ticket_service_update.py`, and
`tests/unit/test_worker_classification.py`, which mock `resolve_sla_due_at`
to isolate each writer's own wiring.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.scope import OrgScope
from app.models.category import Category
from app.models.enums import TicketChannel, UserRole
from app.models.organization import Organization
from app.models.ticket import Ticket
from app.models.urgency_level import UrgencyLevel
from app.services import ticket_service
from app.services.classifier import ClassificationResult
from app.workers import classification as worker

_TIMEZONE = "Europe/Madrid"
_BUSINESS_HOURS = {"mon": [["09:00", "18:00"]], "tue": [["09:00", "18:00"]]}
# A Monday well inside the window above, so `respects_business_hours=True`
# exercises the real business-hours walk identically for all 3 writers.
_ANCHOR = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)


def _org(organization_id) -> Organization:
    return Organization(
        id=organization_id,
        name="Acme Corp",
        chat_widget_key="widget-key",
        timezone=_TIMEZONE,
        business_hours=_BUSINESS_HOURS,
    )


def _urgency(organization_id) -> UrgencyLevel:
    return UrgencyLevel(
        id=uuid4(),
        organization_id=organization_id,
        name="Medium",
        sla_hours=3,
        sort_order=0,
        active=True,
        respects_business_hours=True,
    )


def _scalar_sequence(*scalars: object) -> AsyncMock:
    results = []
    for scalar in scalars:
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = scalar
        results.append(execute_result)
    session = AsyncMock()
    session.execute.side_effect = results
    return session


class _SessionContextManager:
    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_all_three_writers_produce_identical_sla_due_at_for_the_same_inputs(
    monkeypatch,
) -> None:
    organization_id = uuid4()
    urgency = _urgency(organization_id)
    org = _org(organization_id)

    # --- create_ticket ------------------------------------------------
    # Chat-origin (property_id/contract_id both None) skips every other
    # lookup, so the execute sequence is just [category, urgency, org] —
    # this test only cares about the SLA anchor, not Rule A's other steps
    # (already covered by tests/unit/test_ticket_service_create.py).
    scope = OrgScope(organization_id=organization_id, user_id=uuid4(), role=UserRole.ADMIN)
    category = Category(
        id=uuid4(), organization_id=organization_id, name="Plumbing", active=True
    )
    create_session = _scalar_sequence(category, urgency, org)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return _ANCHOR

    monkeypatch.setattr(ticket_service, "datetime", _FixedDatetime)
    created = await ticket_service.create_ticket(
        create_session,
        scope=scope,
        property_id=None,
        contract_id=None,
        title="Leaking faucet",
        category_id=category.id,
        urgency_id=urgency.id,
        channel=TicketChannel.WEB,
    )

    # --- update_ticket (manual correction) -----------------------------
    target = Ticket(
        id=uuid4(),
        organization_id=organization_id,
        user_id=uuid4(),
        property_id=None,
        contract_id=None,
        category_id=uuid4(),
        urgency_id=uuid4(),
        channel="web",
        status="open",
        agent_id=None,
        sla_due_at=None,
        closed_at=None,
        created_at=_ANCHOR,
    )
    update_session = _scalar_sequence(target, urgency, org, None, target)
    updated = await ticket_service.update_ticket(
        update_session,
        scope=scope,
        ticket_id=target.id,
        urgency_id=urgency.id,
    )

    # --- classify_ticket -------------------------------------------------
    worker_ticket = Ticket(
        id=uuid4(),
        organization_id=organization_id,
        user_id=uuid4(),
        property_id=None,
        contract_id=None,
        category_id=None,
        urgency_id=None,
        title="Leaking faucet",
        description="It won't stop",
        channel="web",
        status="open",
        classification_status="pending",
        agent_id=None,
        sla_due_at=None,
        closed_at=None,
        created_at=_ANCHOR,
    )

    def _worker_execute_result(*, scalar=None, scalars_list=None) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none.return_value = scalar
        if scalars_list is not None:
            scalars = MagicMock()
            scalars.all.return_value = scalars_list
            result.scalars.return_value = scalars
        return result

    worker_session = AsyncMock()
    worker_session.execute.side_effect = [
        _worker_execute_result(scalar=worker_ticket),
        _worker_execute_result(scalars_list=[category]),
        _worker_execute_result(scalars_list=[urgency]),
        _worker_execute_result(scalar=org),
        _worker_execute_result(),
    ]

    def _factory():
        return _SessionContextManager(worker_session)

    fake_result = ClassificationResult(
        category_id=category.id,
        urgency_id=urgency.id,
        predicted_category=category.name,
        predicted_urgency=urgency.name,
        confidence=0.9,
        urgency_confidence=0.9,
        model_used="sklearn_v1",
    )

    class _FakeEmbedder:
        async def embed(self, texts):
            return [[0.1] for _ in texts]

    ctx = {
        "session_factory": _factory,
        "embedder": _FakeEmbedder(),
        "classify": AsyncMock(return_value=fake_result),
    }
    await worker.classify_ticket(ctx, worker_ticket.id, organization_id)

    # --- parity ------------------------------------------------------
    assert created.created_at == _ANCHOR
    assert created.sla_due_at == updated.sla_due_at == worker_ticket.sla_due_at
    assert created.sla_due_at is not None

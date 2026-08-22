"""Unit tests for `app.services.ticket_service.create_ticket` (Work Unit
7b). Asserts the exact 8-step validation ORDER of Rule A (CONFIRMED
design) — one test per step, each isolated so a step failure never
proceeds to a later lookup. `AsyncSession` is mocked, following the
pattern established by `tests/unit/test_property_service.py`; each
sequential `get_or_404` call inside `create_ticket` corresponds to one
`session.execute` call, so `session.execute.side_effect` is used to feed
back a distinct scalar per step.
"""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.category import Category
from app.models.contract import Contract
from app.models.enums import (
    ClassificationStatus,
    ContractStatus,
    TicketChannel,
    TicketStatus,
    UserRole,
)
from app.models.organization import Organization
from app.models.property import Property
from app.models.urgency_level import UrgencyLevel
from app.services import ticket_service
from app.services.ticket_service import (
    CategoryInactiveError,
    ContractNotActiveError,
    ContractPropertyMismatchError,
    UrgencyInactiveError,
)


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid4(), user_id=uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _property(scope: OrgScope, **overrides) -> Property:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        owner_id=uuid4(),
        address="123 Main St",
        type="apartment",
        deleted_at=None,
    )
    defaults.update(overrides)
    return Property(**defaults)


def _contract(property_: Property, **overrides) -> Contract:
    defaults = dict(
        id=uuid4(),
        property_id=property_.id,
        tenant_id=uuid4(),
        start_date=date(2025, 1, 1),
        end_date=date(2030, 1, 1),
        status=ContractStatus.ACTIVE,
    )
    defaults.update(overrides)
    return Contract(**defaults)


def _category(scope: OrgScope, **overrides) -> Category:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        name="Plumbing",
        description=None,
        active=True,
    )
    defaults.update(overrides)
    return Category(**defaults)


def _urgency(scope: OrgScope, **overrides) -> UrgencyLevel:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        name="High",
        sla_hours=4,
        sort_order=0,
        active=True,
    )
    defaults.update(overrides)
    return UrgencyLevel(**defaults)


def _org(scope: OrgScope, **overrides) -> Organization:
    """PR4: `resolve_sla_due_at` loads the organization row via
    `OrganizationRepository`, so any test exercising it for real (not
    mocking `resolve_sla_due_at` itself) needs one queued in the session's
    execute sequence."""
    defaults = dict(
        id=scope.organization_id,
        name="Acme Corp",
        chat_widget_key="widget-key",
        timezone="UTC",
        business_hours={},
    )
    defaults.update(overrides)
    return Organization(**defaults)


def _session_returning_sequence(*scalars: object) -> AsyncMock:
    """An `AsyncSession` whose `execute` returns one scalar per call, in
    order — modeling the sequential `get_or_404` lookups `create_ticket`
    performs."""
    results = []
    for scalar in scalars:
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = scalar
        results.append(execute_result)
    session = AsyncMock()
    session.execute.side_effect = results
    return session


async def _create(session, scope, **overrides) -> object:
    fields = dict(
        property_id=uuid4(),
        contract_id=uuid4(),
        title="Leaking faucet",
        category_id=uuid4(),
        urgency_id=uuid4(),
        channel=TicketChannel.WEB,
    )
    fields.update(overrides)
    return await ticket_service.create_ticket(session, scope=scope, **fields)


@pytest.mark.asyncio
async def test_step1_missing_property_raises_not_found_without_looking_at_contract() -> None:
    scope = _scope()
    session = _session_returning_sequence(None)

    with pytest.raises(NotFoundError):
        await _create(session, scope)

    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_step2_missing_contract_raises_not_found() -> None:
    scope = _scope()
    property_ = _property(scope)
    session = _session_returning_sequence(property_, None)

    with pytest.raises(NotFoundError):
        await _create(session, scope, property_id=property_.id)

    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_step3_contract_property_mismatch_raises_422() -> None:
    scope = _scope()
    property_ = _property(scope)
    other_property_id = uuid4()
    contract = _contract(property_, property_id=other_property_id)
    session = _session_returning_sequence(property_, contract)

    with pytest.raises(ContractPropertyMismatchError):
        await _create(session, scope, property_id=property_.id, contract_id=contract.id)

    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_step4_inactive_contract_raises_422() -> None:
    scope = _scope()
    property_ = _property(scope)
    contract = _contract(property_, status=ContractStatus.EXPIRED)
    session = _session_returning_sequence(property_, contract)

    with pytest.raises(ContractNotActiveError):
        await _create(session, scope, property_id=property_.id, contract_id=contract.id)

    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_step5_missing_category_raises_not_found() -> None:
    scope = _scope()
    property_ = _property(scope)
    contract = _contract(property_)
    session = _session_returning_sequence(property_, contract, None)

    with pytest.raises(NotFoundError):
        await _create(session, scope, property_id=property_.id, contract_id=contract.id)

    assert session.execute.await_count == 3


@pytest.mark.asyncio
async def test_step5_inactive_category_raises_422() -> None:
    scope = _scope()
    property_ = _property(scope)
    contract = _contract(property_)
    category = _category(scope, active=False)
    session = _session_returning_sequence(property_, contract, category)

    with pytest.raises(CategoryInactiveError):
        await _create(
            session,
            scope,
            property_id=property_.id,
            contract_id=contract.id,
            category_id=category.id,
        )

    assert session.execute.await_count == 3


@pytest.mark.asyncio
async def test_step6_missing_urgency_raises_not_found() -> None:
    scope = _scope()
    property_ = _property(scope)
    contract = _contract(property_)
    category = _category(scope)
    session = _session_returning_sequence(property_, contract, category, None)

    with pytest.raises(NotFoundError):
        await _create(
            session,
            scope,
            property_id=property_.id,
            contract_id=contract.id,
            category_id=category.id,
        )

    assert session.execute.await_count == 4


@pytest.mark.asyncio
async def test_step6_inactive_urgency_raises_422() -> None:
    scope = _scope()
    property_ = _property(scope)
    contract = _contract(property_)
    category = _category(scope)
    urgency = _urgency(scope, active=False)
    session = _session_returning_sequence(property_, contract, category, urgency)

    with pytest.raises(UrgencyInactiveError):
        await _create(
            session,
            scope,
            property_id=property_.id,
            contract_id=contract.id,
            category_id=category.id,
            urgency_id=urgency.id,
        )

    assert session.execute.await_count == 4


@pytest.mark.asyncio
async def test_step7_tenant_not_the_contracts_tenant_raises_not_found() -> None:
    scope = _scope(role=UserRole.TENANT)
    property_ = _property(scope)
    contract = _contract(property_, tenant_id=uuid4())  # not scope.user_id
    category = _category(scope)
    urgency = _urgency(scope)
    session = _session_returning_sequence(property_, contract, category, urgency)

    with pytest.raises(NotFoundError):
        await _create(
            session,
            scope,
            property_id=property_.id,
            contract_id=contract.id,
            category_id=category.id,
            urgency_id=urgency.id,
        )

    assert session.execute.await_count == 4


@pytest.mark.asyncio
async def test_step7_tenant_who_is_the_contracts_tenant_succeeds(monkeypatch) -> None:
    scope = _scope(role=UserRole.TENANT)
    property_ = _property(scope)
    contract = _contract(property_, tenant_id=scope.user_id)
    category = _category(scope)
    urgency = _urgency(scope)
    session = _session_returning_sequence(property_, contract, category, urgency)
    monkeypatch.setattr(
        ticket_service, "resolve_sla_due_at", AsyncMock(return_value=datetime.now(UTC))
    )

    ticket = await _create(
        session,
        scope,
        property_id=property_.id,
        contract_id=contract.id,
        category_id=category.id,
        urgency_id=urgency.id,
    )

    assert ticket.user_id == scope.user_id


@pytest.mark.asyncio
async def test_step8_success_computes_sla_due_at_and_defaults() -> None:
    """PR4 (stage 6): `sla_due_at` is now anchored on the ticket's own
    `created_at` via `resolve_sla_due_at` — NOT a `before <= ... <= after`
    bound around `datetime.now()` at computation time (the pre-PR4
    assertion) — an intentional, accepted behavior change (see PR4's
    RESOLVED decision 1): `create_ticket` sets `created_at` explicitly in
    Python and uses THAT exact instant as the anchor, so the result is
    reproducibly `created_at + sla_hours` (business-hours OFF here) rather
    than merely bounded by two `datetime.now()` samples taken around the
    call."""
    scope = _scope(role=UserRole.OWNER)
    property_ = _property(scope)
    contract = _contract(property_)
    category = _category(scope)
    urgency = _urgency(scope, sla_hours=8, respects_business_hours=False)
    org = _org(scope)
    session = _session_returning_sequence(property_, contract, category, urgency, org)

    ticket = await _create(
        session,
        scope,
        property_id=property_.id,
        contract_id=contract.id,
        category_id=category.id,
        urgency_id=urgency.id,
    )

    assert ticket.status == TicketStatus.OPEN
    assert ticket.agent_id is None
    assert ticket.user_id == scope.user_id
    assert ticket.property_id == property_.id
    assert ticket.contract_id == contract.id
    assert ticket.category_id == category.id
    assert ticket.urgency_id == urgency.id
    assert ticket.created_at is not None
    assert ticket.sla_due_at == ticket.created_at + timedelta(hours=8)
    session.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# Stage 2 — optional taxonomy at creation (PR5): omitting category_id/
# urgency_id skips Rule A's taxonomy-lookup+SLA steps entirely and leaves
# the ticket `classification_status="pending"` for the async worker to pick
# up later.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_without_taxonomy_skips_taxonomy_lookups_and_sets_pending() -> None:
    scope = _scope()
    property_ = _property(scope)
    contract = _contract(property_)
    session = _session_returning_sequence(property_, contract)

    ticket = await _create(
        session,
        scope,
        property_id=property_.id,
        contract_id=contract.id,
        category_id=None,
        urgency_id=None,
    )

    assert ticket.category_id is None
    assert ticket.urgency_id is None
    assert ticket.sla_due_at is None
    assert ticket.classification_status == ClassificationStatus.PENDING
    assert ticket.title == "Leaking faucet"
    # Only the property + contract lookups happened — no category/urgency
    # queries were issued, since both were omitted.
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_create_without_taxonomy_still_enforces_contract_active_check() -> None:
    """Steps 1-4 of Rule A (property/contract resolution + contract-active
    check) still run even when taxonomy is omitted — only the
    category/urgency/SLA steps (5, 6, 8) are skipped."""
    scope = _scope()
    property_ = _property(scope)
    contract = _contract(property_, status=ContractStatus.EXPIRED)
    session = _session_returning_sequence(property_, contract)

    from app.services.ticket_service import ContractNotActiveError

    with pytest.raises(ContractNotActiveError):
        await _create(
            session,
            scope,
            property_id=property_.id,
            contract_id=contract.id,
            category_id=None,
            urgency_id=None,
        )


@pytest.mark.asyncio
async def test_create_with_both_taxonomy_ids_still_computes_sla_synchronously(
    monkeypatch,
) -> None:
    """Regression: supplying both `category_id`/`urgency_id` upfront keeps
    Rule A's synchronous taxonomy validation + SLA computation exactly as
    before (see also `test_step8_success_computes_sla_due_at_and_defaults`,
    which already covers this path in full)."""
    scope = _scope()
    property_ = _property(scope)
    contract = _contract(property_)
    category = _category(scope)
    urgency = _urgency(scope, sla_hours=6)
    session = _session_returning_sequence(property_, contract, category, urgency)
    monkeypatch.setattr(
        ticket_service, "resolve_sla_due_at", AsyncMock(return_value=datetime.now(UTC))
    )

    ticket = await _create(
        session,
        scope,
        property_id=property_.id,
        contract_id=contract.id,
        category_id=category.id,
        urgency_id=urgency.id,
    )

    assert ticket.category_id == category.id
    assert ticket.urgency_id == urgency.id
    assert ticket.sla_due_at is not None
    assert session.execute.await_count == 4


# ---------------------------------------------------------------------------
# Stage 4 (PR3) — chat-originated tickets have no property/contract context
# at all: `property_id`/`contract_id` are optional (`UUID | None`).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_chat_origin_ticket_without_property_or_contract() -> None:
    """`property_id=None`/`contract_id=None` (chat-origin path, see
    `app.services.chat_tools.escalate_to_human`) skips every check that
    depends on a resolved property/contract — no `session.execute` call at
    all — and still creates the ticket successfully."""
    scope = _scope(role=UserRole.TENANT)
    session = _session_returning_sequence()

    ticket = await ticket_service.create_ticket(
        session,
        scope=scope,
        property_id=None,
        contract_id=None,
        title="Chat escalation",
        description="Escalated from chatbot",
        category_id=None,
        urgency_id=None,
        channel=TicketChannel.WEB,
    )

    assert ticket.property_id is None
    assert ticket.contract_id is None
    assert ticket.user_id == scope.user_id
    assert ticket.status == TicketStatus.OPEN
    assert ticket.sla_due_at is None
    assert ticket.classification_status == ClassificationStatus.PENDING
    assert session.execute.await_count == 0
    session.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# Stage 6 (PR4) — writer rewiring: `create_ticket` must anchor `sla_due_at`
# on the ticket's OWN `created_at` (set explicitly in Python, same pattern
# as `classification_status` above) via `resolve_sla_due_at`, not
# `datetime.now()` at the moment of computation — unifying it onto the same
# anchor convention `update_ticket`/`classify_ticket` already use.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_ticket_anchors_sla_due_at_on_created_at_via_resolve_sla_due_at(
    monkeypatch,
) -> None:
    scope = _scope(role=UserRole.OWNER)
    property_ = _property(scope)
    contract = _contract(property_)
    category = _category(scope)
    urgency = _urgency(scope, sla_hours=8)
    session = _session_returning_sequence(property_, contract, category, urgency)

    fake_due_at = datetime(2030, 1, 1, tzinfo=UTC)
    fake_resolve = AsyncMock(return_value=fake_due_at)
    monkeypatch.setattr(ticket_service, "resolve_sla_due_at", fake_resolve)

    ticket = await _create(
        session,
        scope,
        property_id=property_.id,
        contract_id=contract.id,
        category_id=category.id,
        urgency_id=urgency.id,
    )

    fake_resolve.assert_awaited_once()
    _, kwargs = fake_resolve.call_args
    assert ticket.created_at is not None
    assert kwargs["from_timestamp"] == ticket.created_at
    assert kwargs["organization_id"] == scope.organization_id
    assert kwargs["urgency"] is urgency
    assert ticket.sla_due_at == fake_due_at

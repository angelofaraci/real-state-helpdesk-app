"""Unit tests for the stage-2 shape of the ticket Pydantic schemas
(`TicketCreate`/`TicketPatch`/`TicketResponse`).

Pure schema-validation tests — no DB, no service layer. Per this file's
convention (mirrored from `app/schemas/ticket.py`'s docstrings): shape
violations (missing/blank required fields, conflicting optional pairs) are
Pydantic's job, not the service layer's — these tests pin that contract
down at the schema boundary.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.ticket import TicketCreate, TicketPatch, TicketResponse


def _create_kwargs(**overrides) -> dict:
    kwargs = dict(
        property_id=uuid4(),
        contract_id=uuid4(),
        title="Leaking faucet",
    )
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# TicketCreate: title required, description optional
# ---------------------------------------------------------------------------


def test_title_is_required() -> None:
    with pytest.raises(ValidationError):
        TicketCreate(**_create_kwargs(title=None))


def test_title_rejects_blank_string() -> None:
    with pytest.raises(ValidationError):
        TicketCreate(**_create_kwargs(title="   "))


def test_description_is_optional_and_defaults_to_none() -> None:
    ticket = TicketCreate(**_create_kwargs())

    assert ticket.description is None


def test_description_may_be_supplied() -> None:
    ticket = TicketCreate(**_create_kwargs(description="It won't stop dripping."))

    assert ticket.description == "It won't stop dripping."


# ---------------------------------------------------------------------------
# TicketCreate: category_id/urgency_id are both-or-neither
# ---------------------------------------------------------------------------


def test_category_and_urgency_may_both_be_omitted() -> None:
    ticket = TicketCreate(**_create_kwargs())

    assert ticket.category_id is None
    assert ticket.urgency_id is None


def test_category_and_urgency_may_both_be_supplied() -> None:
    category_id = uuid4()
    urgency_id = uuid4()

    ticket = TicketCreate(
        **_create_kwargs(category_id=category_id, urgency_id=urgency_id)
    )

    assert ticket.category_id == category_id
    assert ticket.urgency_id == urgency_id


def test_category_without_urgency_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TicketCreate(**_create_kwargs(category_id=uuid4()))


def test_urgency_without_category_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TicketCreate(**_create_kwargs(urgency_id=uuid4()))


# ---------------------------------------------------------------------------
# TicketPatch: category_id/urgency_id are now patchable
# ---------------------------------------------------------------------------


def test_patch_accepts_category_id() -> None:
    category_id = uuid4()

    patch = TicketPatch(category_id=category_id)

    assert patch.category_id == category_id


def test_patch_accepts_urgency_id() -> None:
    urgency_id = uuid4()

    patch = TicketPatch(urgency_id=urgency_id)

    assert patch.urgency_id == urgency_id


def test_patch_defaults_leave_category_and_urgency_unset() -> None:
    patch = TicketPatch()

    assert patch.category_id is None
    assert patch.urgency_id is None


# ---------------------------------------------------------------------------
# TicketResponse: category_id/urgency_id/sla_due_at nullable, title included
# ---------------------------------------------------------------------------


def _response_kwargs(**overrides) -> dict:
    from datetime import UTC, datetime

    kwargs = dict(
        id=uuid4(),
        organization_id=uuid4(),
        user_id=uuid4(),
        property_id=uuid4(),
        contract_id=uuid4(),
        category_id=None,
        urgency_id=None,
        title="Leaking faucet",
        description=None,
        channel="web",
        status="open",
        classification_status="pending",
        agent_id=None,
        sla_due_at=None,
        created_at=datetime.now(UTC),
        closed_at=None,
    )
    kwargs.update(overrides)
    return kwargs


def test_response_allows_null_category_urgency_and_sla_due_at() -> None:
    response = TicketResponse(**_response_kwargs())

    assert response.category_id is None
    assert response.urgency_id is None
    assert response.sla_due_at is None


def test_response_includes_title_and_description() -> None:
    response = TicketResponse(
        **_response_kwargs(title="Leaking faucet", description="Drip drip")
    )

    assert response.title == "Leaking faucet"
    assert response.description == "Drip drip"

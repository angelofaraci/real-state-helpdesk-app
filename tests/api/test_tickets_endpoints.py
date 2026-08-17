"""API-level tests for `/api/v1/tickets/*` (Work Units 7a + 7b). No live
Postgres is available in this sandbox, so `ticket_service` is monkeypatched
at the module boundary and `get_session`/`get_principal` are overridden
with inert stand-ins, following the pattern established by
`tests/api/test_properties_endpoints.py`.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import tickets as tickets_module
from app.core.exceptions import NotFoundError as CoreNotFoundError
from app.core.session import get_session
from app.main import app
from app.models.enums import (
    ClassificationStatus,
    TicketChannel,
    TicketStatus,
    UserRole,
    UserStatus,
)
from app.models.ticket import Ticket
from app.services import ticket_service
from app.services.ticket_service import (
    CategoryInactiveError,
    ContractNotActiveError,
    ContractPropertyMismatchError,
    ForbiddenError,
    InvalidAgentError,
    UrgencyInactiveError,
)


_UNSET = object()


def _fake_principal(role=UserRole.ADMIN, organization_id=_UNSET):
    return type(
        "FakeUser",
        (),
        {
            "id": uuid4(),
            "organization_id": uuid4() if organization_id is _UNSET else organization_id,
            "name": "Some User",
            "email": "user@example.com",
            "role": role,
            "status": UserStatus.ACTIVE,
        },
    )()


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = lambda: AsyncMock()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _make_ticket(
    principal, classification_status=ClassificationStatus.PENDING
) -> Ticket:
    ticket = Ticket(
        organization_id=principal.organization_id,
        user_id=uuid4(),
        property_id=None,
        contract_id=None,
        title="Leaking faucet",
        description=None,
        category_id=uuid4(),
        urgency_id=uuid4(),
        channel=TicketChannel.WEB,
        status=TicketStatus.OPEN,
        classification_status=classification_status,
        agent_id=None,
        sla_due_at=datetime.now(UTC) + timedelta(hours=4),
        closed_at=None,
    )
    ticket.id = uuid4()
    ticket.created_at = datetime.now(UTC)
    return ticket


@pytest.mark.parametrize(
    "role", [UserRole.TENANT, UserRole.OWNER, UserRole.AGENT, UserRole.ADMIN]
)
def test_list_tickets_returns_200_for_every_org_role(client, monkeypatch, role) -> None:
    principal = _fake_principal(role=role)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    monkeypatch.setattr(
        ticket_service, "list_tickets", AsyncMock(return_value=[_make_ticket(principal)])
    )

    response = client.get("/api/v1/tickets")

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_list_tickets_rejects_a_super_admin_with_403(client) -> None:
    principal = _fake_principal(role=UserRole.ADMIN, organization_id=None)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    response = client.get("/api/v1/tickets")

    assert response.status_code == 403


def test_list_tickets_includes_classification_status(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    ticket = _make_ticket(principal, classification_status=ClassificationStatus.CLASSIFIED)
    monkeypatch.setattr(
        ticket_service, "list_tickets", AsyncMock(return_value=[ticket])
    )

    response = client.get("/api/v1/tickets")

    assert response.status_code == 200
    assert response.json()[0]["classification_status"] == "classified"


def test_list_tickets_forwards_query_filters(client, monkeypatch) -> None:
    principal = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: principal
    mocked = AsyncMock(return_value=[])
    monkeypatch.setattr(ticket_service, "list_tickets", mocked)
    category_id = uuid4()

    response = client.get(
        "/api/v1/tickets", params={"status": "open", "category_id": str(category_id)}
    )

    assert response.status_code == 200
    _, kwargs = mocked.call_args
    assert kwargs["status"] == TicketStatus.OPEN
    assert kwargs["category_id"] == category_id


def test_get_ticket_returns_200(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.TENANT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    target = _make_ticket(principal)
    monkeypatch.setattr(ticket_service, "get_ticket", AsyncMock(return_value=target))

    response = client.get(f"/api/v1/tickets/{target.id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(target.id)


def test_get_ticket_includes_classification_status(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.TENANT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    target = _make_ticket(principal, classification_status=ClassificationStatus.CLASSIFIED)
    monkeypatch.setattr(ticket_service, "get_ticket", AsyncMock(return_value=target))

    response = client.get(f"/api/v1/tickets/{target.id}")

    assert response.status_code == 200
    assert response.json()["classification_status"] == "classified"


def test_get_ticket_maps_not_found_to_404(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.OWNER)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    monkeypatch.setattr(
        ticket_service,
        "get_ticket",
        AsyncMock(side_effect=CoreNotFoundError("Ticket", uuid4())),
    )

    response = client.get(f"/api/v1/tickets/{uuid4()}")

    assert response.status_code == 404


def test_get_ticket_rejects_a_super_admin_with_403(client) -> None:
    principal = _fake_principal(role=UserRole.ADMIN, organization_id=None)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    response = client.get(f"/api/v1/tickets/{uuid4()}")

    assert response.status_code == 403


def _create_payload(**overrides) -> dict:
    payload = dict(
        property_id=str(uuid4()),
        contract_id=str(uuid4()),
        title="Leaking faucet",
        category_id=str(uuid4()),
        urgency_id=str(uuid4()),
    )
    payload.update(overrides)
    return payload


def test_create_ticket_returns_201(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.TENANT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    created = _make_ticket(principal)
    monkeypatch.setattr(ticket_service, "create_ticket", AsyncMock(return_value=created))
    monkeypatch.setattr(tickets_module, "enqueue_classification", AsyncMock())

    response = client.post("/api/v1/tickets", json=_create_payload())

    assert response.status_code == 201
    assert response.json()["id"] == str(created.id)


def test_create_ticket_with_explicit_category_id_returns_pending(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.TENANT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    created = _make_ticket(principal, classification_status=ClassificationStatus.PENDING)
    monkeypatch.setattr(ticket_service, "create_ticket", AsyncMock(return_value=created))
    monkeypatch.setattr(tickets_module, "enqueue_classification", AsyncMock())

    response = client.post("/api/v1/tickets", json=_create_payload())

    assert response.status_code == 201
    assert response.json()["classification_status"] == "pending"


def test_create_ticket_ignores_classification_status_in_payload(client, monkeypatch) -> None:
    """`classification_status` is read-only: it is not a field on
    `TicketCreate`, so even if a caller sends it in the request body it is
    silently dropped and never forwarded to `ticket_service.create_ticket`."""
    principal = _fake_principal(role=UserRole.TENANT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    created = _make_ticket(principal, classification_status=ClassificationStatus.PENDING)
    mocked = AsyncMock(return_value=created)
    monkeypatch.setattr(ticket_service, "create_ticket", mocked)
    monkeypatch.setattr(tickets_module, "enqueue_classification", AsyncMock())

    response = client.post(
        "/api/v1/tickets",
        json=_create_payload(classification_status="classified"),
    )

    assert response.status_code == 201
    assert response.json()["classification_status"] == "pending"
    _, kwargs = mocked.call_args
    assert "classification_status" not in kwargs


def test_update_ticket_ignores_classification_status_in_payload(client, monkeypatch) -> None:
    """`classification_status` is not a field on `TicketPatch` either — a
    PATCH body attempting to set it is silently dropped."""
    principal = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    target = _make_ticket(principal, classification_status=ClassificationStatus.PENDING)
    mocked = AsyncMock(return_value=target)
    monkeypatch.setattr(ticket_service, "update_ticket", mocked)

    response = client.patch(
        f"/api/v1/tickets/{target.id}",
        json={"status": "resolved", "classification_status": "classified"},
    )

    assert response.status_code == 200
    _, kwargs = mocked.call_args
    assert "classification_status" not in kwargs


def test_create_ticket_requires_property_id_and_contract_id(client) -> None:
    principal = _fake_principal(role=UserRole.TENANT)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    payload = _create_payload()
    del payload["property_id"]
    del payload["contract_id"]

    response = client.post("/api/v1/tickets", json=payload)

    assert response.status_code == 422


def test_create_ticket_rejects_a_super_admin_with_403(client) -> None:
    principal = _fake_principal(role=UserRole.ADMIN, organization_id=None)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    response = client.post("/api/v1/tickets", json=_create_payload())

    assert response.status_code == 403


def test_create_ticket_maps_not_found_to_404(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.TENANT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    monkeypatch.setattr(
        ticket_service,
        "create_ticket",
        AsyncMock(side_effect=CoreNotFoundError("Property", uuid4())),
    )

    response = client.post("/api/v1/tickets", json=_create_payload())

    assert response.status_code == 404


@pytest.mark.parametrize(
    "exc",
    [
        ContractPropertyMismatchError("mismatch"),
        ContractNotActiveError("not active"),
        CategoryInactiveError("inactive"),
        UrgencyInactiveError("inactive"),
    ],
)
def test_create_ticket_maps_business_rule_violations_to_422(client, monkeypatch, exc) -> None:
    principal = _fake_principal(role=UserRole.TENANT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    monkeypatch.setattr(ticket_service, "create_ticket", AsyncMock(side_effect=exc))

    response = client.post("/api/v1/tickets", json=_create_payload())

    assert response.status_code == 422


def test_update_ticket_returns_200(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    target = _make_ticket(principal)
    target.status = TicketStatus.RESOLVED
    monkeypatch.setattr(ticket_service, "update_ticket", AsyncMock(return_value=target))

    response = client.patch(
        f"/api/v1/tickets/{target.id}", json={"status": "resolved"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "resolved"


# ---------------------------------------------------------------------------
# Stage 3 (PR5) — `PATCH /tickets/:id` enqueues `embed_resolved_ticket` when
# `ticket_service.update_ticket` flags `entered_resolved_or_closed=True` on
# the returned ticket (the actual transition edge into RESOLVED/CLOSED),
# mirroring `enqueue_classification`'s `BackgroundTasks` pattern on create.
# ---------------------------------------------------------------------------


def test_update_ticket_transitioning_into_resolved_enqueues_embedding_job(
    client, monkeypatch
) -> None:
    principal = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    target = _make_ticket(principal)
    target.status = TicketStatus.RESOLVED
    target.entered_resolved_or_closed = True
    monkeypatch.setattr(ticket_service, "update_ticket", AsyncMock(return_value=target))
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(tickets_module, "enqueue_resolved_ticket_embedding", mocked_enqueue)

    response = client.patch(f"/api/v1/tickets/{target.id}", json={"status": "resolved"})

    assert response.status_code == 200
    mocked_enqueue.assert_awaited_once_with(target.id, target.organization_id)


def test_update_ticket_unrelated_patch_enqueues_nothing(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.ADMIN)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    target = _make_ticket(principal)
    target.status = TicketStatus.RESOLVED
    target.entered_resolved_or_closed = False
    monkeypatch.setattr(ticket_service, "update_ticket", AsyncMock(return_value=target))
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(tickets_module, "enqueue_resolved_ticket_embedding", mocked_enqueue)

    response = client.patch(f"/api/v1/tickets/{target.id}", json={"agent_id": str(uuid4())})

    assert response.status_code == 200
    mocked_enqueue.assert_not_awaited()


def test_update_ticket_missing_flag_attribute_enqueues_nothing(client, monkeypatch) -> None:
    """`ticket_service.update_ticket` is mocked in most other tests without
    setting `entered_resolved_or_closed` at all — the route must treat a
    missing attribute the same as `False`, never raise."""
    principal = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    target = _make_ticket(principal)
    target.status = TicketStatus.RESOLVED
    assert not hasattr(target, "entered_resolved_or_closed")
    monkeypatch.setattr(ticket_service, "update_ticket", AsyncMock(return_value=target))
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(tickets_module, "enqueue_resolved_ticket_embedding", mocked_enqueue)

    response = client.patch(f"/api/v1/tickets/{target.id}", json={"status": "resolved"})

    assert response.status_code == 200
    mocked_enqueue.assert_not_awaited()


def test_update_ticket_maps_forbidden_to_403(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.TENANT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    monkeypatch.setattr(
        ticket_service,
        "update_ticket",
        AsyncMock(side_effect=ForbiddenError("only agents/admins may resolve or close a ticket")),
    )

    response = client.patch(
        f"/api/v1/tickets/{uuid4()}", json={"status": "resolved"}
    )

    assert response.status_code == 403


def test_update_ticket_maps_not_found_to_404(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    monkeypatch.setattr(
        ticket_service,
        "update_ticket",
        AsyncMock(side_effect=CoreNotFoundError("Ticket", uuid4())),
    )

    response = client.patch(
        f"/api/v1/tickets/{uuid4()}", json={"status": "in_progress"}
    )

    assert response.status_code == 404


def test_update_ticket_maps_invalid_agent_to_422(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.ADMIN)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    monkeypatch.setattr(
        ticket_service,
        "update_ticket",
        AsyncMock(side_effect=InvalidAgentError("agent_id must reference an agent or admin")),
    )

    response = client.patch(
        f"/api/v1/tickets/{uuid4()}", json={"agent_id": str(uuid4())}
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Stage 2 (PR5) — background enqueue of the async classification job when
# taxonomy is omitted at creation.
# ---------------------------------------------------------------------------


def test_create_ticket_without_taxonomy_enqueues_classification_job(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.TENANT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    created = _make_ticket(principal, classification_status=ClassificationStatus.PENDING)
    monkeypatch.setattr(ticket_service, "create_ticket", AsyncMock(return_value=created))
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(tickets_module, "enqueue_classification", mocked_enqueue)

    payload = _create_payload()
    del payload["category_id"]
    del payload["urgency_id"]

    response = client.post("/api/v1/tickets", json=payload)

    assert response.status_code == 201
    mocked_enqueue.assert_awaited_once_with(created.id, created.organization_id)


def test_create_ticket_with_taxonomy_enqueues_nothing(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.TENANT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    created = _make_ticket(principal, classification_status=ClassificationStatus.CLASSIFIED)
    monkeypatch.setattr(ticket_service, "create_ticket", AsyncMock(return_value=created))
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(tickets_module, "enqueue_classification", mocked_enqueue)

    response = client.post("/api/v1/tickets", json=_create_payload())

    assert response.status_code == 201
    mocked_enqueue.assert_not_awaited()


# ---------------------------------------------------------------------------
# Stage 2 (PR5) — GET /tickets/:id role-gates classification metadata
# (predicted category/urgency, confidence, model_used) to agent/admin.
# ---------------------------------------------------------------------------


def _attach_classification_metadata(ticket: Ticket) -> Ticket:
    ticket.predicted_category = "Plumbing"
    ticket.confidence = 0.87
    ticket.predicted_urgency = "High"
    ticket.urgency_confidence = 0.75
    ticket.model_used = "sklearn_v1"
    return ticket


@pytest.mark.parametrize("role", [UserRole.AGENT, UserRole.ADMIN])
def test_get_ticket_includes_classification_metadata_for_staff(client, monkeypatch, role) -> None:
    principal = _fake_principal(role=role)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    target = _attach_classification_metadata(_make_ticket(principal))
    monkeypatch.setattr(ticket_service, "get_ticket", AsyncMock(return_value=target))

    response = client.get(f"/api/v1/tickets/{target.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["predicted_category"] == "Plumbing"
    assert body["confidence"] == 0.87
    assert body["predicted_urgency"] == "High"
    assert body["urgency_confidence"] == 0.75
    assert body["model_used"] == "sklearn_v1"


@pytest.mark.parametrize("role", [UserRole.TENANT, UserRole.OWNER])
def test_get_ticket_hides_classification_metadata_for_non_staff(client, monkeypatch, role) -> None:
    principal = _fake_principal(role=role)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    target = _attach_classification_metadata(_make_ticket(principal))
    monkeypatch.setattr(ticket_service, "get_ticket", AsyncMock(return_value=target))

    response = client.get(f"/api/v1/tickets/{target.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["predicted_category"] is None
    assert body["confidence"] is None
    assert body["predicted_urgency"] is None
    assert body["urgency_confidence"] is None
    assert body["model_used"] is None

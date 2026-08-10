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
from app.core.exceptions import NotFoundError as CoreNotFoundError
from app.core.session import get_session
from app.main import app
from app.models.enums import TicketChannel, TicketStatus, UserRole, UserStatus
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


def _make_ticket(principal) -> Ticket:
    ticket = Ticket(
        organization_id=principal.organization_id,
        user_id=uuid4(),
        property_id=None,
        contract_id=None,
        category_id=uuid4(),
        urgency_id=uuid4(),
        channel=TicketChannel.WEB,
        status=TicketStatus.OPEN,
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

    response = client.post("/api/v1/tickets", json=_create_payload())

    assert response.status_code == 201
    assert response.json()["id"] == str(created.id)


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

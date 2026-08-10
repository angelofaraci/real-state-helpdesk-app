"""API-level tests for the read-only `/api/v1/tickets/*` endpoints (Work
Unit 7a). No live Postgres is available in this sandbox, so `ticket_service`
is monkeypatched at the module boundary and `get_session`/`get_principal`
are overridden with inert stand-ins, following the pattern established by
`tests/api/test_properties_endpoints.py`.

Creation/update routes are Work Unit 7b and are not exercised here.
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

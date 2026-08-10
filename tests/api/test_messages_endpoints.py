"""API-level tests for `/api/v1/tickets/{ticket_id}/messages` (Work Unit
8). No live Postgres is available in this sandbox, so `message_service` is
monkeypatched at the module boundary and `get_session`/`get_principal` are
overridden with inert stand-ins, following the pattern established by
`tests/api/test_tickets_endpoints.py`.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.core.exceptions import NotFoundError as CoreNotFoundError
from app.core.session import get_session
from app.main import app
from app.models.enums import AuthorType, UserRole, UserStatus
from app.models.message import Message
from app.services import message_service

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


def _make_message(ticket_id, **overrides) -> Message:
    defaults = dict(
        id=uuid4(),
        ticket_id=ticket_id,
        author_type=AuthorType.USER,
        content="hello",
        is_ai_suggestion=False,
    )
    defaults.update(overrides)
    message = Message(**defaults)
    message.created_at = datetime.now(UTC)
    return message


def test_list_messages_returns_200(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.TENANT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    ticket_id = uuid4()
    monkeypatch.setattr(
        message_service,
        "list_messages",
        AsyncMock(return_value=[_make_message(ticket_id)]),
    )

    response = client.get(f"/api/v1/tickets/{ticket_id}/messages")

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_list_messages_maps_not_found_to_404(client, monkeypatch) -> None:
    """A caller who cannot see the parent ticket (wrong org, or a
    role-invisible ticket, e.g. one tenant's ticket fetched by another
    tenant) gets 404, via the service layer's `TicketRepository.get_or_404`
    pre-check."""
    principal = _fake_principal(role=UserRole.TENANT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    monkeypatch.setattr(
        message_service,
        "list_messages",
        AsyncMock(side_effect=CoreNotFoundError("Ticket", uuid4())),
    )

    response = client.get(f"/api/v1/tickets/{uuid4()}/messages")

    assert response.status_code == 404


def test_list_messages_rejects_a_super_admin_with_403(client) -> None:
    principal = _fake_principal(role=UserRole.ADMIN, organization_id=None)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    response = client.get(f"/api/v1/tickets/{uuid4()}/messages")

    assert response.status_code == 403


def test_create_message_returns_201(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    ticket_id = uuid4()
    created = _make_message(ticket_id, author_type=AuthorType.AGENT, content="on my way")
    monkeypatch.setattr(
        message_service, "create_message", AsyncMock(return_value=created)
    )

    response = client.post(
        f"/api/v1/tickets/{ticket_id}/messages", json={"content": "on my way"}
    )

    assert response.status_code == 201
    assert response.json()["content"] == "on my way"
    assert response.json()["author_type"] == "agent"


def test_create_message_maps_not_found_to_404(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.TENANT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    monkeypatch.setattr(
        message_service,
        "create_message",
        AsyncMock(side_effect=CoreNotFoundError("Ticket", uuid4())),
    )

    response = client.post(
        f"/api/v1/tickets/{uuid4()}/messages", json={"content": "hello"}
    )

    assert response.status_code == 404


def test_create_message_rejects_a_super_admin_with_403(client) -> None:
    principal = _fake_principal(role=UserRole.ADMIN, organization_id=None)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    response = client.post(
        f"/api/v1/tickets/{uuid4()}/messages", json={"content": "hello"}
    )

    assert response.status_code == 403


def test_create_message_ignores_a_client_supplied_author_type(client, monkeypatch) -> None:
    """A client trying to smuggle `author_type=bot` into the request body
    never reaches `message_service.create_message` as an `author_type`
    kwarg — the schema has no such field, so it's dropped before the
    service layer sees the payload."""
    principal = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    ticket_id = uuid4()
    mocked = AsyncMock(
        return_value=_make_message(ticket_id, author_type=AuthorType.AGENT, content="hi")
    )
    monkeypatch.setattr(message_service, "create_message", mocked)

    response = client.post(
        f"/api/v1/tickets/{ticket_id}/messages",
        json={"content": "hi", "author_type": "bot"},
    )

    assert response.status_code == 201
    _, kwargs = mocked.call_args
    assert "author_type" not in kwargs
    assert kwargs["content"] == "hi"


def test_create_message_requires_content(client) -> None:
    principal = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    response = client.post(f"/api/v1/tickets/{uuid4()}/messages", json={})

    assert response.status_code == 422

"""API-level tests for the stage-5 (PR3 — email outbound) `POST
/api/v1/tickets/{ticket_id}/messages` hook: an agent/admin reply flagged
`outbound_email_required` by `app.services.message_service.create_message`
enqueues `app.workers.email.send_ticket_email_reply` via `BackgroundTasks`,
mirroring `create_ticket`'s `enqueue_classification` hook exactly (see
`tests/api/test_tickets_endpoints.py`).

`message_service` is monkeypatched at its module boundary (no live Postgres
in this sandbox), following the same pattern `test_tickets_endpoints.py`
already establishes for `ticket_service`.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import tickets as tickets_module
from app.core.session import get_session
from app.main import app
from app.models.enums import AuthorType, UserRole, UserStatus
from app.models.message import Message
from app.services import message_service

_UNSET = object()


def _fake_principal(role=UserRole.AGENT, organization_id=_UNSET):
    return type(
        "FakeUser",
        (),
        {
            "id": uuid4(),
            "organization_id": uuid4() if organization_id is _UNSET else organization_id,
            "name": "Some Agent",
            "email": "agent@example.com",
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


def _make_message(*, ticket_id, outbound_email_required, **overrides) -> Message:
    message = Message(
        id=uuid4(),
        ticket_id=ticket_id,
        author_type=AuthorType.AGENT,
        content="We're sending someone over.",
        is_ai_suggestion=False,
        based_on_suggestion_id=None,
    )
    message.created_at = datetime.now(UTC)
    message.outbound_email_required = outbound_email_required
    for key, value in overrides.items():
        setattr(message, key, value)
    return message


def test_agent_reply_flagged_outbound_email_required_enqueues_the_reply_job(
    client, monkeypatch
) -> None:
    principal = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    ticket_id = uuid4()
    created = _make_message(ticket_id=ticket_id, outbound_email_required=True)
    monkeypatch.setattr(message_service, "create_message", AsyncMock(return_value=created))
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(tickets_module, "enqueue_ticket_email_reply", mocked_enqueue)

    response = client.post(
        f"/api/v1/tickets/{ticket_id}/messages", json={"content": "We're sending someone over."}
    )

    assert response.status_code == 201
    mocked_enqueue.assert_awaited_once_with(created.id, created.ticket_id, principal.organization_id)


def test_reply_not_flagged_outbound_email_required_enqueues_nothing(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    ticket_id = uuid4()
    created = _make_message(ticket_id=ticket_id, outbound_email_required=False)
    monkeypatch.setattr(message_service, "create_message", AsyncMock(return_value=created))
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(tickets_module, "enqueue_ticket_email_reply", mocked_enqueue)

    response = client.post(
        f"/api/v1/tickets/{ticket_id}/messages", json={"content": "Thanks for the update."}
    )

    assert response.status_code == 201
    mocked_enqueue.assert_not_awaited()


def test_reply_missing_the_flag_entirely_enqueues_nothing(client, monkeypatch) -> None:
    """A `Message` returned without `outbound_email_required` ever having
    been set (e.g. a test double, or a call site that doesn't go through
    `message_service.create_message`) must not crash the route — `getattr`
    defaults to falsy, mirroring `entered_resolved_or_closed`'s idiom."""
    principal = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    ticket_id = uuid4()
    created = Message(
        id=uuid4(),
        ticket_id=ticket_id,
        author_type=AuthorType.AGENT,
        content="hi",
        is_ai_suggestion=False,
    )
    created.created_at = datetime.now(UTC)
    monkeypatch.setattr(message_service, "create_message", AsyncMock(return_value=created))
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(tickets_module, "enqueue_ticket_email_reply", mocked_enqueue)

    response = client.post(f"/api/v1/tickets/{ticket_id}/messages", json={"content": "hi"})

    assert response.status_code == 201
    mocked_enqueue.assert_not_awaited()

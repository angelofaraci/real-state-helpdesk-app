"""API-level tests for `GET /tickets/{ticket_id}/suggested-response`
(Stage 3, PR7 — Work Unit 6.5/6.6).

No live Postgres is available in this sandbox, so `app.services.rag` is
monkeypatched at the module boundary and `get_session`/`get_principal` are
overridden with inert stand-ins, following the pattern established by
`tests/api/test_messages_endpoints.py`. `get_rag_embedder`/`get_llm_client`
(PR7's new FastAPI dependencies) are overridden the same way
`get_session`/`get_principal` are, so no real embedding/LLM call is ever
made from this test module.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps, deps_rag
from app.core.exceptions import NotFoundError as CoreNotFoundError
from app.core.session import get_session
from app.main import app
from app.models.enums import AuthorType, UserRole, UserStatus
from app.models.message import Message
from app.services import rag as rag_service
from app.services.rag import SuggestedResponse

_UNSET = object()


def _fake_principal(role=UserRole.AGENT, organization_id=_UNSET):
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
    app.dependency_overrides[deps_rag.get_rag_embedder] = lambda: object()
    app.dependency_overrides[deps_rag.get_llm_client] = lambda: object()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _make_suggestion_message(ticket_id, content="Try replacing the washer.") -> Message:
    message = Message(
        id=uuid4(),
        ticket_id=ticket_id,
        author_type=AuthorType.BOT,
        content=content,
        is_ai_suggestion=True,
    )
    message.created_at = datetime.now(UTC)
    return message


def test_suggested_response_returns_200_with_suggestion(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    ticket_id = uuid4()
    message = _make_suggestion_message(ticket_id)
    monkeypatch.setattr(
        rag_service,
        "suggest_response",
        AsyncMock(return_value=SuggestedResponse(message=message, top_similarity=0.83)),
    )

    response = client.get(f"/api/v1/tickets/{ticket_id}/suggested-response")

    assert response.status_code == 200
    body = response.json()
    assert body["suggestion"]["content"] == "Try replacing the washer."
    assert body["suggestion"]["top_similarity"] == pytest.approx(0.83)
    assert body["suggestion"]["message_id"] == str(message.id)
    assert body["reason"] is None


def test_suggested_response_returns_200_with_null_suggestion_when_no_grounded_source(
    client, monkeypatch
) -> None:
    principal = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    monkeypatch.setattr(rag_service, "suggest_response", AsyncMock(return_value=None))

    response = client.get(f"/api/v1/tickets/{uuid4()}/suggested-response")

    assert response.status_code == 200
    body = response.json()
    assert body["suggestion"] is None
    assert body["reason"] == "no grounded suggestion available"


def test_suggested_response_maps_not_found_to_404(client, monkeypatch) -> None:
    principal = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: principal
    monkeypatch.setattr(
        rag_service,
        "suggest_response",
        AsyncMock(side_effect=CoreNotFoundError("Ticket", uuid4())),
    )

    response = client.get(f"/api/v1/tickets/{uuid4()}/suggested-response")

    assert response.status_code == 404


@pytest.mark.parametrize("role", [UserRole.TENANT, UserRole.OWNER])
def test_suggested_response_rejects_non_staff_with_403(client, role) -> None:
    principal = _fake_principal(role=role)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    response = client.get(f"/api/v1/tickets/{uuid4()}/suggested-response")

    assert response.status_code == 403


def test_suggested_response_rejects_a_super_admin_with_403(client) -> None:
    principal = _fake_principal(role=UserRole.ADMIN, organization_id=None)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    response = client.get(f"/api/v1/tickets/{uuid4()}/suggested-response")

    assert response.status_code == 403

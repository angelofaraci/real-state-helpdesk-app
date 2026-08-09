"""API-level tests for the `/api/v1/auth/*` endpoints.

No live Postgres is available in this sandbox, so `auth_service` is
monkeypatched at the module boundary and `get_session` is overridden with
an inert stand-in. These tests verify HTTP contract (status codes,
request/response shapes, routing, auth dependency wiring) — not real
database behavior.
"""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.core.session import get_session
from app.main import app
from app.models.enums import UserRole, UserStatus
from app.services import auth_service
from app.services.auth_service import AuthenticationError, InviteAcceptError, TokenPair


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = lambda: AsyncMock()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_login_returns_token_pair_on_success(client, monkeypatch) -> None:
    monkeypatch.setattr(
        auth_service,
        "login",
        AsyncMock(return_value=TokenPair(access_token="a.b.c", refresh_token="raw-refresh")),
    )

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "jane@example.com", "password": "correct-horse-battery"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] == "a.b.c"
    assert body["refresh_token"] == "raw-refresh"
    assert body["token_type"] == "bearer"


def test_login_returns_401_on_authentication_error(client, monkeypatch) -> None:
    monkeypatch.setattr(
        auth_service, "login", AsyncMock(side_effect=AuthenticationError("invalid"))
    )

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "jane@example.com", "password": "wrong-password"},
    )

    assert response.status_code == 401


def test_login_rejects_malformed_request_body(client) -> None:
    response = client.post(
        "/api/v1/auth/login", json={"email": "not-an-email", "password": "x"}
    )
    assert response.status_code == 422


def test_refresh_returns_new_token_pair_on_success(client, monkeypatch) -> None:
    monkeypatch.setattr(
        auth_service,
        "refresh",
        AsyncMock(return_value=TokenPair(access_token="new.a.c", refresh_token="new-raw")),
    )

    response = client.post("/api/v1/auth/refresh", json={"refresh_token": "old-raw"})

    assert response.status_code == 200
    assert response.json()["access_token"] == "new.a.c"


def test_refresh_returns_401_on_reuse_detected(client, monkeypatch) -> None:
    monkeypatch.setattr(
        auth_service, "refresh", AsyncMock(side_effect=AuthenticationError("invalid"))
    )

    response = client.post("/api/v1/auth/refresh", json={"refresh_token": "stolen"})

    assert response.status_code == 401


def test_logout_returns_204_and_revokes_family(client, monkeypatch) -> None:
    mock_logout = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_service, "logout", mock_logout)

    response = client.post("/api/v1/auth/logout", json={"refresh_token": "some-raw"})

    assert response.status_code == 204
    mock_logout.assert_awaited_once()


def test_me_returns_authenticated_principal(client) -> None:
    fake_user = type(
        "FakeUser",
        (),
        {
            "id": uuid4(),
            "organization_id": uuid4(),
            "name": "Jane Doe",
            "email": "jane@example.com",
            "role": UserRole.AGENT,
            "status": UserStatus.ACTIVE,
        },
    )()
    app.dependency_overrides[deps.get_principal] = lambda: fake_user

    response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer whatever"})

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "jane@example.com"
    assert body["role"] == "agent"


def test_me_returns_401_without_a_token(client) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401 or response.status_code == 403


def test_accept_invite_returns_token_pair_on_success(client, monkeypatch) -> None:
    monkeypatch.setattr(
        auth_service,
        "accept_invite",
        AsyncMock(return_value=TokenPair(access_token="a.b.c", refresh_token="raw-refresh")),
    )

    response = client.post(
        "/api/v1/auth/accept-invite",
        json={"token": "raw-invite-token", "new_password": "a-strong-password"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] == "a.b.c"
    assert body["refresh_token"] == "raw-refresh"


def test_accept_invite_returns_400_on_invalid_token(client, monkeypatch) -> None:
    monkeypatch.setattr(
        auth_service,
        "accept_invite",
        AsyncMock(side_effect=InviteAcceptError("invalid or expired invite token")),
    )

    response = client.post(
        "/api/v1/auth/accept-invite",
        json={"token": "bad-token", "new_password": "a-strong-password"},
    )

    assert response.status_code == 400


def test_accept_invite_rejects_a_password_below_minimum_length(client) -> None:
    response = client.post(
        "/api/v1/auth/accept-invite",
        json={"token": "raw-invite-token", "new_password": "short"},
    )

    assert response.status_code == 422

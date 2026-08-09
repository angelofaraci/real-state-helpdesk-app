"""API-level tests for the `/api/v1/users/*` endpoints.

No live Postgres is available in this sandbox, so `user_service` is
monkeypatched at the module boundary and `get_session`/`get_principal` are
overridden with inert stand-ins. These tests verify HTTP contract (status
codes, request/response shapes, routing, auth/role dependency wiring) —
not real database behavior.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api import deps
from app.core.session import get_session
from app.main import app
from app.models.enums import UserRole, UserStatus
from app.models.user import User
from app.services import user_service
from app.services.user_service import ConflictError, ForbiddenError, NotFoundError


def _fake_principal(role=UserRole.ADMIN, organization_id=None):
    return type(
        "FakeUser",
        (),
        {
            "id": uuid4(),
            "organization_id": organization_id or uuid4(),
            "name": "Admin User",
            "email": "admin@example.com",
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


def _make_invited_user(admin) -> User:
    user = User(
        organization_id=admin.organization_id,
        name="Jane Doe",
        email="jane@example.com",
        role=UserRole.AGENT,
        password_hash=None,
        status=UserStatus.PENDING,
    )
    user.id = uuid4()
    user.created_at = datetime.now(UTC)
    return user


def test_invite_user_returns_201_with_created_user_and_never_the_raw_token(
    client, monkeypatch
) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    created_user = _make_invited_user(admin)
    monkeypatch.setattr(
        user_service, "invite_user", AsyncMock(return_value=created_user)
    )

    response = client.post(
        "/api/v1/users",
        json={"name": "Jane Doe", "email": "jane@example.com", "role": "agent"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "jane@example.com"
    assert body["status"] == "pending"
    assert "token" not in body
    assert "raw_token" not in body


@pytest.mark.parametrize(
    ("side_effect", "expected_status"),
    [
        (ForbiddenError("nope"), 403),
        (IntegrityError("stmt", {}, Exception("dup")), 409),
    ],
)
def test_invite_user_maps_service_errors_to_the_right_status(
    client, monkeypatch, side_effect, expected_status
) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(user_service, "invite_user", AsyncMock(side_effect=side_effect))

    response = client.post(
        "/api/v1/users",
        json={"name": "Jane Doe", "email": "jane@example.com", "role": "agent"},
    )

    assert response.status_code == expected_status


def test_invite_user_returns_422_for_an_unknown_role(client) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin

    response = client.post(
        "/api/v1/users",
        json={"name": "Jane Doe", "email": "jane@example.com", "role": "superuser"},
    )

    assert response.status_code == 422


def test_invite_user_returns_401_without_a_token(client) -> None:
    response = client.post(
        "/api/v1/users",
        json={"name": "Jane Doe", "email": "jane@example.com", "role": "agent"},
    )
    assert response.status_code == 401 or response.status_code == 403


def test_reissue_invite_returns_200_on_success(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    target_user = _make_invited_user(admin)
    monkeypatch.setattr(
        user_service, "reissue_invite", AsyncMock(return_value=target_user)
    )

    response = client.post(f"/api/v1/users/{target_user.id}/invite")

    assert response.status_code == 200
    assert response.json()["email"] == "jane@example.com"


@pytest.mark.parametrize(
    ("side_effect", "expected_status"),
    [
        (ForbiddenError("nope"), 403),
        (NotFoundError("not found"), 404),
        (ConflictError("already_active"), 409),
    ],
)
def test_reissue_invite_maps_service_errors_to_the_right_status(
    client, monkeypatch, side_effect, expected_status
) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(user_service, "reissue_invite", AsyncMock(side_effect=side_effect))

    response = client.post(f"/api/v1/users/{uuid4()}/invite")

    assert response.status_code == expected_status

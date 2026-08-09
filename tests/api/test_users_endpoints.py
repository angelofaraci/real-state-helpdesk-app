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
from app.core.exceptions import NotFoundError as CoreNotFoundError
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


# --- list / get / patch / delete (Work Unit 4) ------------------------------


def test_list_users_returns_200_with_users(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    users = [_make_invited_user(admin)]
    monkeypatch.setattr(user_service, "list_users", AsyncMock(return_value=users))

    response = client.get("/api/v1/users")

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_list_users_forwards_role_and_status_query_filters(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    mock_list = AsyncMock(return_value=[])
    monkeypatch.setattr(user_service, "list_users", mock_list)

    response = client.get("/api/v1/users", params={"role": "agent", "status": "active"})

    assert response.status_code == 200
    assert mock_list.call_args.kwargs["role"] == UserRole.AGENT
    assert mock_list.call_args.kwargs["status"] == UserStatus.ACTIVE


def test_get_user_returns_200(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    target = _make_invited_user(admin)
    monkeypatch.setattr(user_service, "get_user", AsyncMock(return_value=target))

    response = client.get(f"/api/v1/users/{target.id}")

    assert response.status_code == 200
    assert response.json()["email"] == "jane@example.com"


def test_get_user_maps_not_found_to_404(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        user_service,
        "get_user",
        AsyncMock(side_effect=CoreNotFoundError("User", uuid4())),
    )

    response = client.get(f"/api/v1/users/{uuid4()}")

    assert response.status_code == 404


def test_update_user_returns_200(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    target = _make_invited_user(admin)
    target.name = "New Name"
    monkeypatch.setattr(user_service, "update_user", AsyncMock(return_value=target))

    response = client.patch(f"/api/v1/users/{target.id}", json={"name": "New Name"})

    assert response.status_code == 200
    assert response.json()["name"] == "New Name"


def test_delete_user_returns_200_and_soft_deletes(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    target = _make_invited_user(admin)
    target.status = UserStatus.DISABLED
    mock_disable = AsyncMock(return_value=target)
    monkeypatch.setattr(user_service, "disable_user", mock_disable)

    response = client.delete(f"/api/v1/users/{target.id}")

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"


def _super_admin() -> User:
    principal = _fake_principal()
    principal.organization_id = None  # ADMIN role but no org -> super-admin
    return principal


@pytest.mark.parametrize(
    "principal", [_fake_principal(role=UserRole.AGENT), _super_admin()]
)
def test_users_crud_endpoints_reject_non_org_admins(client, principal) -> None:
    """`require_org_admin`'s full role/org-id matrix is unit-tested in
    `test_deps_admin_guards.py`; here one non-admin role and the
    super-admin edge case are enough to prove the guard is wired onto
    every route (list/get/patch/delete are declared separately, unlike
    the router-level guard on `/organizations`)."""
    app.dependency_overrides[deps.get_principal] = lambda: principal

    assert client.get("/api/v1/users").status_code == 403
    assert client.get(f"/api/v1/users/{uuid4()}").status_code == 403
    assert client.patch(f"/api/v1/users/{uuid4()}", json={"name": "X"}).status_code == 403
    assert client.delete(f"/api/v1/users/{uuid4()}").status_code == 403

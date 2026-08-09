"""API-level tests for `/api/v1/organizations/*` (super-admin only).
`require_super_admin` itself is unit-tested in `test_deps_admin_guards.py`;
here we only prove HTTP wiring (status codes, response shapes) and that the
router-level dependency actually gates the router — one route is enough to
prove that, since `dependencies=[...]` applies identically to every route.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.core.session import get_session
from app.main import app
from app.models.enums import UserRole, UserStatus
from app.models.organization import Organization
from app.services import organization_service
from app.services.organization_service import ConflictError


def _fake_principal(*, super_admin: bool = True):
    return type(
        "FakeUser",
        (),
        {
            "id": uuid4(),
            "organization_id": None if super_admin else uuid4(),
            "name": "Root",
            "email": "root@example.com",
            "role": UserRole.ADMIN,
            "status": UserStatus.ACTIVE,
        },
    )()


def _make_org(**overrides) -> Organization:
    org = Organization(id=uuid4(), name="Acme Corp")
    org.created_at = datetime.now(UTC)
    for key, value in overrides.items():
        setattr(org, key, value)
    return org


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = lambda: AsyncMock()
    app.dependency_overrides[deps.get_principal] = lambda: _fake_principal()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_create_organization_returns_201(client, monkeypatch) -> None:
    monkeypatch.setattr(
        organization_service, "create_organization", AsyncMock(return_value=_make_org())
    )

    response = client.post("/api/v1/organizations", json={"name": "Acme Corp"})

    assert response.status_code == 201
    assert response.json()["name"] == "Acme Corp"


def test_create_organization_maps_conflict_to_409(client, monkeypatch) -> None:
    monkeypatch.setattr(
        organization_service,
        "create_organization",
        AsyncMock(side_effect=ConflictError("duplicate")),
    )

    response = client.post("/api/v1/organizations", json={"name": "Acme Corp"})

    assert response.status_code == 409


def test_get_organization_returns_200(client, monkeypatch) -> None:
    org = _make_org()
    monkeypatch.setattr(
        organization_service, "get_organization", AsyncMock(return_value=org)
    )

    response = client.get(f"/api/v1/organizations/{org.id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(org.id)


def test_update_organization_returns_200(client, monkeypatch) -> None:
    org = _make_org(name="New Name")
    monkeypatch.setattr(
        organization_service, "update_organization", AsyncMock(return_value=org)
    )

    response = client.patch(f"/api/v1/organizations/{org.id}", json={"name": "New Name"})

    assert response.status_code == 200
    assert response.json()["name"] == "New Name"


def test_router_rejects_a_non_super_admin(client) -> None:
    app.dependency_overrides[deps.get_principal] = lambda: _fake_principal(super_admin=False)

    response = client.post("/api/v1/organizations", json={"name": "Acme Corp"})

    assert response.status_code == 404

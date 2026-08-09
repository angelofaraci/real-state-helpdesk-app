"""API-level tests for the `/api/v1/properties/*` endpoints.

No live Postgres is available in this sandbox, so `property_service` is
monkeypatched at the module boundary and `get_session`/`get_principal` are
overridden with inert stand-ins, following the pattern established by
`tests/api/test_users_endpoints.py`.
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
from app.models.enums import PropertyType, UserRole, UserStatus
from app.models.property import Property
from app.services import property_service
from app.services.property_service import InvalidOwnerRoleError


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


def _make_property(admin) -> Property:
    prop = Property(
        organization_id=admin.organization_id,
        owner_id=uuid4(),
        address="123 Main St",
        type=PropertyType.APARTMENT,
        deleted_at=None,
    )
    prop.id = uuid4()
    prop.created_at = datetime.now(UTC)
    return prop


def test_create_property_returns_201(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    created = _make_property(admin)
    monkeypatch.setattr(property_service, "create_property", AsyncMock(return_value=created))

    response = client.post(
        "/api/v1/properties",
        json={"owner_id": str(uuid4()), "address": "123 Main St", "type": "apartment"},
    )

    assert response.status_code == 201
    assert response.json()["address"] == "123 Main St"


def test_create_property_maps_invalid_owner_role_to_422(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        property_service,
        "create_property",
        AsyncMock(side_effect=InvalidOwnerRoleError("bad role")),
    )

    response = client.post(
        "/api/v1/properties",
        json={"owner_id": str(uuid4()), "address": "123 Main St", "type": "apartment"},
    )

    assert response.status_code == 422


def test_create_property_maps_cross_org_owner_to_404(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        property_service,
        "create_property",
        AsyncMock(side_effect=CoreNotFoundError("User", uuid4())),
    )

    response = client.post(
        "/api/v1/properties",
        json={"owner_id": str(uuid4()), "address": "123 Main St", "type": "apartment"},
    )

    assert response.status_code == 404


def test_create_property_rejects_a_non_admin_with_403(client) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent

    response = client.post(
        "/api/v1/properties",
        json={"owner_id": str(uuid4()), "address": "123 Main St", "type": "apartment"},
    )

    assert response.status_code == 403


def test_list_properties_returns_200_for_admin(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        property_service, "list_properties", AsyncMock(return_value=[_make_property(admin)])
    )

    response = client.get("/api/v1/properties")

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_list_properties_returns_200_for_agent(client, monkeypatch) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent
    monkeypatch.setattr(property_service, "list_properties", AsyncMock(return_value=[]))

    response = client.get("/api/v1/properties")

    assert response.status_code == 200


@pytest.mark.parametrize("role", [UserRole.TENANT, UserRole.OWNER])
def test_list_properties_rejects_tenant_and_owner_with_403(client, role) -> None:
    principal = _fake_principal(role=role)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    response = client.get("/api/v1/properties")

    assert response.status_code == 403


def test_get_property_maps_not_found_to_404(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        property_service,
        "get_property",
        AsyncMock(side_effect=CoreNotFoundError("Property", uuid4())),
    )

    response = client.get(f"/api/v1/properties/{uuid4()}")

    assert response.status_code == 404


def test_update_property_returns_200(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    target = _make_property(admin)
    target.address = "New Address"
    monkeypatch.setattr(property_service, "update_property", AsyncMock(return_value=target))

    response = client.patch(
        f"/api/v1/properties/{target.id}", json={"address": "New Address"}
    )

    assert response.status_code == 200
    assert response.json()["address"] == "New Address"


def test_update_property_rejects_a_non_admin_with_403(client) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent

    response = client.patch(
        f"/api/v1/properties/{uuid4()}", json={"address": "New Address"}
    )

    assert response.status_code == 403


def test_delete_property_returns_200_and_soft_deletes(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    target = _make_property(admin)
    target.deleted_at = datetime.now(UTC)
    monkeypatch.setattr(property_service, "delete_property", AsyncMock(return_value=target))

    response = client.delete(f"/api/v1/properties/{target.id}")

    assert response.status_code == 200


def test_delete_property_rejects_a_non_admin_with_403(client) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent

    response = client.delete(f"/api/v1/properties/{uuid4()}")

    assert response.status_code == 403

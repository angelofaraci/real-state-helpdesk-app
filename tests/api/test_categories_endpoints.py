"""API-level tests for the `/api/v1/categories/*` endpoints.

No live Postgres is available in this sandbox, so `category_service` is
monkeypatched at the module boundary and `get_session`/`get_principal` are
overridden with inert stand-ins, following the pattern established by
`tests/api/test_properties_endpoints.py`.

Role gate: write routes (`POST`/`PATCH`/`DELETE`) are admin-only via
`require_org_admin`; read routes (`GET`) are staff (admin+agent) via
`require_org_staff`, the same precedent Work Unit 5 established for
properties/contracts reference data — categories are reference/taxonomy
data, so the same assumption applies here.
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
from app.models.category import Category
from app.models.enums import UserRole, UserStatus
from app.services import category_service
from app.services.category_service import ConflictError
from app.services.taxonomy_service import TaxonomyDeleteResult


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


def _make_category(admin, **overrides) -> Category:
    defaults = dict(
        organization_id=admin.organization_id,
        name="Plumbing",
        description=None,
        active=True,
    )
    defaults.update(overrides)
    category = Category(**defaults)
    category.id = uuid4()
    category.created_at = datetime.now(UTC)
    return category


def test_create_category_returns_201(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    created = _make_category(admin)
    monkeypatch.setattr(category_service, "create_category", AsyncMock(return_value=created))

    response = client.post("/api/v1/categories", json={"name": "Plumbing"})

    assert response.status_code == 201
    assert response.json()["name"] == "Plumbing"


def test_create_category_maps_conflict_to_409(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        category_service, "create_category", AsyncMock(side_effect=ConflictError("dup"))
    )

    response = client.post("/api/v1/categories", json={"name": "Plumbing"})

    assert response.status_code == 409


def test_create_category_rejects_a_non_admin_with_403(client) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent

    response = client.post("/api/v1/categories", json={"name": "Plumbing"})

    assert response.status_code == 403


def test_list_categories_returns_200_for_agent(client, monkeypatch) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent
    monkeypatch.setattr(category_service, "list_categories", AsyncMock(return_value=[]))

    response = client.get("/api/v1/categories")

    assert response.status_code == 200


@pytest.mark.parametrize("role", [UserRole.TENANT, UserRole.OWNER])
def test_list_categories_rejects_tenant_and_owner_with_403(client, role) -> None:
    principal = _fake_principal(role=role)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    response = client.get("/api/v1/categories")

    assert response.status_code == 403


def test_get_category_maps_not_found_to_404(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        category_service,
        "get_category",
        AsyncMock(side_effect=CoreNotFoundError("Category", uuid4())),
    )

    response = client.get(f"/api/v1/categories/{uuid4()}")

    assert response.status_code == 404


def test_update_category_returns_200(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    target = _make_category(admin, name="New Name")
    monkeypatch.setattr(category_service, "update_category", AsyncMock(return_value=target))

    response = client.patch(f"/api/v1/categories/{target.id}", json={"name": "New Name"})

    assert response.status_code == 200
    assert response.json()["name"] == "New Name"


def test_update_category_rejects_a_non_admin_with_403(client) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent

    response = client.patch(f"/api/v1/categories/{uuid4()}", json={"name": "New Name"})

    assert response.status_code == 403


def test_delete_category_returns_200_with_deleted_true_when_hard_deleted(
    client, monkeypatch
) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    target_id = uuid4()
    monkeypatch.setattr(
        category_service,
        "delete_category",
        AsyncMock(return_value=TaxonomyDeleteResult(id=target_id, deleted=True, active=False)),
    )

    response = client.delete(f"/api/v1/categories/{target_id}")

    assert response.status_code == 200
    assert response.json() == {"id": str(target_id), "deleted": True, "active": False}


def test_delete_category_returns_200_with_deleted_false_when_still_referenced(
    client, monkeypatch
) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    target_id = uuid4()
    monkeypatch.setattr(
        category_service,
        "delete_category",
        AsyncMock(return_value=TaxonomyDeleteResult(id=target_id, deleted=False, active=False)),
    )

    response = client.delete(f"/api/v1/categories/{target_id}")

    assert response.status_code == 200
    assert response.json() == {"id": str(target_id), "deleted": False, "active": False}


def test_delete_category_rejects_a_non_admin_with_403(client) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent

    response = client.delete(f"/api/v1/categories/{uuid4()}")

    assert response.status_code == 403

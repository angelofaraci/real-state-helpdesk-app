"""API-level tests for the `/api/v1/urgency-levels/*` endpoints. Mirrors
`tests/api/test_categories_endpoints.py`, plus the `sla_hours > 0` schema
validator and the DELETE route CONFIRMED for urgency levels alongside
categories (both share the delete-or-deactivate rule).
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
from app.models.enums import UserRole, UserStatus
from app.models.urgency_level import UrgencyLevel
from app.services import urgency_level_service
from app.services.taxonomy_service import TaxonomyDeleteResult
from app.services.urgency_level_service import ConflictError


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


def _make_urgency(admin, **overrides) -> UrgencyLevel:
    defaults = dict(
        organization_id=admin.organization_id,
        name="Critical",
        sla_hours=4,
        sort_order=0,
        active=True,
        respects_business_hours=False,
    )
    defaults.update(overrides)
    urgency = UrgencyLevel(**defaults)
    urgency.id = uuid4()
    urgency.created_at = datetime.now(UTC)
    return urgency


def test_create_urgency_level_returns_201(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    created = _make_urgency(admin)
    monkeypatch.setattr(
        urgency_level_service, "create_urgency_level", AsyncMock(return_value=created)
    )

    response = client.post(
        "/api/v1/urgency-levels", json={"name": "Critical", "sla_hours": 4}
    )

    assert response.status_code == 201
    assert response.json()["sla_hours"] == 4


def test_create_urgency_level_rejects_non_positive_sla_hours_with_422(client) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin

    response = client.post(
        "/api/v1/urgency-levels", json={"name": "Critical", "sla_hours": 0}
    )

    assert response.status_code == 422


def test_create_urgency_level_maps_conflict_to_409(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        urgency_level_service,
        "create_urgency_level",
        AsyncMock(side_effect=ConflictError("dup")),
    )

    response = client.post(
        "/api/v1/urgency-levels", json={"name": "Critical", "sla_hours": 4}
    )

    assert response.status_code == 409


def test_create_urgency_level_rejects_a_non_admin_with_403(client) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent

    response = client.post(
        "/api/v1/urgency-levels", json={"name": "Critical", "sla_hours": 4}
    )

    assert response.status_code == 403


def test_list_urgency_levels_returns_200_for_agent(client, monkeypatch) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent
    monkeypatch.setattr(
        urgency_level_service, "list_urgency_levels", AsyncMock(return_value=[])
    )

    response = client.get("/api/v1/urgency-levels")

    assert response.status_code == 200


@pytest.mark.parametrize("role", [UserRole.TENANT, UserRole.OWNER])
def test_list_urgency_levels_rejects_tenant_and_owner_with_403(client, role) -> None:
    principal = _fake_principal(role=role)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    response = client.get("/api/v1/urgency-levels")

    assert response.status_code == 403


def test_get_urgency_level_maps_not_found_to_404(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        urgency_level_service,
        "get_urgency_level",
        AsyncMock(side_effect=CoreNotFoundError("UrgencyLevel", uuid4())),
    )

    response = client.get(f"/api/v1/urgency-levels/{uuid4()}")

    assert response.status_code == 404


def test_update_urgency_level_returns_200(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    target = _make_urgency(admin, sla_hours=8)
    monkeypatch.setattr(
        urgency_level_service, "update_urgency_level", AsyncMock(return_value=target)
    )

    response = client.patch(f"/api/v1/urgency-levels/{target.id}", json={"sla_hours": 8})

    assert response.status_code == 200
    assert response.json()["sla_hours"] == 8


def test_update_urgency_level_rejects_a_non_admin_with_403(client) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent

    response = client.patch(f"/api/v1/urgency-levels/{uuid4()}", json={"sla_hours": 8})

    assert response.status_code == 403


def test_delete_urgency_level_returns_200_with_deleted_true_when_hard_deleted(
    client, monkeypatch
) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    target_id = uuid4()
    monkeypatch.setattr(
        urgency_level_service,
        "delete_urgency_level",
        AsyncMock(return_value=TaxonomyDeleteResult(id=target_id, deleted=True, active=False)),
    )

    response = client.delete(f"/api/v1/urgency-levels/{target_id}")

    assert response.status_code == 200
    assert response.json() == {"id": str(target_id), "deleted": True, "active": False}


def test_delete_urgency_level_returns_200_with_deleted_false_when_still_referenced(
    client, monkeypatch
) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    target_id = uuid4()
    monkeypatch.setattr(
        urgency_level_service,
        "delete_urgency_level",
        AsyncMock(return_value=TaxonomyDeleteResult(id=target_id, deleted=False, active=False)),
    )

    response = client.delete(f"/api/v1/urgency-levels/{target_id}")

    assert response.status_code == 200
    assert response.json() == {"id": str(target_id), "deleted": False, "active": False}


def test_delete_urgency_level_rejects_a_non_admin_with_403(client) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent

    response = client.delete(f"/api/v1/urgency-levels/{uuid4()}")

    assert response.status_code == 403

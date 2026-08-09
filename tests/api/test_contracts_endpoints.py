"""API-level tests for the `/api/v1/contracts/*` endpoints.

No live Postgres is available in this sandbox, so `contract_service` is
monkeypatched at the module boundary and `get_session`/`get_principal` are
overridden with inert stand-ins, following the pattern established by
`tests/api/test_users_endpoints.py`.
"""

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.core.exceptions import NotFoundError as CoreNotFoundError
from app.core.session import get_session
from app.main import app
from app.models.contract import Contract
from app.models.enums import ContractStatus, UserRole, UserStatus
from app.services import contract_service
from app.services.contract_service import InvalidTenantRoleError


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


def _make_contract() -> Contract:
    contract = Contract(
        property_id=uuid4(),
        tenant_id=uuid4(),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        status=ContractStatus.ACTIVE,
    )
    contract.id = uuid4()
    contract.created_at = datetime.now(UTC)
    return contract


def test_create_contract_returns_201(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    created = _make_contract()
    monkeypatch.setattr(contract_service, "create_contract", AsyncMock(return_value=created))

    response = client.post(
        "/api/v1/contracts",
        json={
            "property_id": str(uuid4()),
            "tenant_id": str(uuid4()),
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        },
    )

    assert response.status_code == 201
    assert response.json()["status"] == "active"


def test_create_contract_maps_invalid_tenant_role_to_422(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        contract_service,
        "create_contract",
        AsyncMock(side_effect=InvalidTenantRoleError("bad role")),
    )

    response = client.post(
        "/api/v1/contracts",
        json={
            "property_id": str(uuid4()),
            "tenant_id": str(uuid4()),
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        },
    )

    assert response.status_code == 422


def test_create_contract_maps_cross_org_property_to_404(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        contract_service,
        "create_contract",
        AsyncMock(side_effect=CoreNotFoundError("Property", uuid4())),
    )

    response = client.post(
        "/api/v1/contracts",
        json={
            "property_id": str(uuid4()),
            "tenant_id": str(uuid4()),
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        },
    )

    assert response.status_code == 404


def test_create_contract_rejects_a_non_admin_with_403(client) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent

    response = client.post(
        "/api/v1/contracts",
        json={
            "property_id": str(uuid4()),
            "tenant_id": str(uuid4()),
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        },
    )

    assert response.status_code == 403


def test_list_contracts_returns_200_for_admin(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        contract_service, "list_contracts", AsyncMock(return_value=[_make_contract()])
    )

    response = client.get("/api/v1/contracts")

    assert response.status_code == 200
    assert len(response.json()) == 1


@pytest.mark.parametrize("role", [UserRole.TENANT, UserRole.OWNER])
def test_list_contracts_rejects_tenant_and_owner_with_403(client, role) -> None:
    principal = _fake_principal(role=role)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    response = client.get("/api/v1/contracts")

    assert response.status_code == 403


def test_get_contract_maps_not_found_to_404(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        contract_service,
        "get_contract",
        AsyncMock(side_effect=CoreNotFoundError("Contract", uuid4())),
    )

    response = client.get(f"/api/v1/contracts/{uuid4()}")

    assert response.status_code == 404


def test_update_contract_returns_200(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    target = _make_contract()
    target.status = ContractStatus.TERMINATED
    monkeypatch.setattr(contract_service, "update_contract", AsyncMock(return_value=target))

    response = client.patch(
        f"/api/v1/contracts/{target.id}", json={"status": "terminated"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "terminated"


def test_update_contract_rejects_a_non_admin_with_403(client) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent

    response = client.patch(
        f"/api/v1/contracts/{uuid4()}", json={"status": "terminated"}
    )

    assert response.status_code == 403


def test_contracts_router_has_no_delete_endpoint(client) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin

    response = client.delete(f"/api/v1/contracts/{uuid4()}")

    assert response.status_code == 405

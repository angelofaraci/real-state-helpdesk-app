"""API-level tests for the `/api/v1/knowledge-base/*` endpoints.

No live Postgres is available in this sandbox, so `knowledge_base_service`
is monkeypatched at the module boundary and `get_session`/`get_principal`
are overridden with inert stand-ins, following the pattern established by
`tests/api/test_categories_endpoints.py`.

Role gate: write routes (`POST`/`PATCH`/`DELETE`) are admin-only via
`require_org_admin`; read routes (`GET`) are staff (admin+agent) via
`require_org_staff` — same precedent as categories/properties, a
tenant/owner has no business reading the org's knowledge base directly.
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
from app.api.v1 import knowledge_base as knowledge_base_router
from app.models.knowledge_base import KnowledgeBase
from app.services import knowledge_base_service


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
def client(monkeypatch):
    app.dependency_overrides[get_session] = lambda: AsyncMock()
    # Never let a test accidentally open a real Redis connection when the
    # background task actually runs inside TestClient's request lifecycle.
    monkeypatch.setattr(knowledge_base_router, "enqueue_kb_embedding", AsyncMock())
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _make_kb(admin, **overrides) -> KnowledgeBase:
    defaults = dict(
        organization_id=admin.organization_id,
        title="Pet policy",
        content="Pets are allowed with a deposit.",
        source_type=None,
        status="pending",
        embedding_error=None,
    )
    defaults.update(overrides)
    kb = KnowledgeBase(**defaults)
    kb.id = uuid4()
    kb.created_at = datetime.now(UTC)
    return kb


# ---------------------------------------------------------------------------
# POST — admin-only
# ---------------------------------------------------------------------------


def test_create_knowledge_base_returns_201_for_admin(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    created = _make_kb(admin)
    monkeypatch.setattr(
        knowledge_base_service, "create_knowledge_base", AsyncMock(return_value=created)
    )

    response = client.post(
        "/api/v1/knowledge-base",
        json={"title": "Pet policy", "content": "Pets are allowed with a deposit."},
    )

    assert response.status_code == 201
    assert response.json()["status"] == "pending"


def test_create_knowledge_base_rejects_a_non_admin_with_403(client) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent

    response = client.post(
        "/api/v1/knowledge-base",
        json={"title": "Pet policy", "content": "Pets are allowed with a deposit."},
    )

    assert response.status_code == 403


@pytest.mark.parametrize("role", [UserRole.TENANT, UserRole.OWNER])
def test_create_knowledge_base_rejects_tenant_and_owner_with_403(client, role) -> None:
    principal = _fake_principal(role=role)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    response = client.post(
        "/api/v1/knowledge-base",
        json={"title": "Pet policy", "content": "Pets are allowed with a deposit."},
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# GET (list, by id) — staff (admin/agent)
# ---------------------------------------------------------------------------


def test_list_knowledge_base_returns_200_for_agent(client, monkeypatch) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent
    monkeypatch.setattr(
        knowledge_base_service, "list_knowledge_base", AsyncMock(return_value=[])
    )

    response = client.get("/api/v1/knowledge-base")

    assert response.status_code == 200


@pytest.mark.parametrize("role", [UserRole.TENANT, UserRole.OWNER])
def test_list_knowledge_base_rejects_tenant_and_owner_with_403(client, role) -> None:
    principal = _fake_principal(role=role)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    response = client.get("/api/v1/knowledge-base")

    assert response.status_code == 403


def test_get_knowledge_base_maps_not_found_to_404(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        knowledge_base_service,
        "get_knowledge_base",
        AsyncMock(side_effect=CoreNotFoundError("KnowledgeBase", uuid4())),
    )

    response = client.get(f"/api/v1/knowledge-base/{uuid4()}")

    assert response.status_code == 404


def test_get_knowledge_base_rejects_tenant_with_403(client) -> None:
    tenant = _fake_principal(role=UserRole.TENANT)
    app.dependency_overrides[deps.get_principal] = lambda: tenant

    response = client.get(f"/api/v1/knowledge-base/{uuid4()}")

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# PATCH — admin-only
# ---------------------------------------------------------------------------


def test_update_knowledge_base_returns_200_for_admin(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    target = _make_kb(admin, title="New title")
    monkeypatch.setattr(
        knowledge_base_service, "update_knowledge_base", AsyncMock(return_value=target)
    )

    response = client.patch(
        f"/api/v1/knowledge-base/{target.id}", json={"title": "New title"}
    )

    assert response.status_code == 200
    assert response.json()["title"] == "New title"


def test_update_knowledge_base_rejects_a_non_admin_with_403(client) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent

    response = client.patch(
        f"/api/v1/knowledge-base/{uuid4()}", json={"title": "New title"}
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# DELETE — admin-only
# ---------------------------------------------------------------------------


def test_delete_knowledge_base_returns_204_for_admin(client, monkeypatch) -> None:
    admin = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        knowledge_base_service, "delete_knowledge_base", AsyncMock(return_value=None)
    )

    response = client.delete(f"/api/v1/knowledge-base/{uuid4()}")

    assert response.status_code == 204


def test_delete_knowledge_base_rejects_a_non_admin_with_403(client) -> None:
    agent = _fake_principal(role=UserRole.AGENT)
    app.dependency_overrides[deps.get_principal] = lambda: agent

    response = client.delete(f"/api/v1/knowledge-base/{uuid4()}")

    assert response.status_code == 403


@pytest.mark.parametrize("role", [UserRole.TENANT, UserRole.OWNER])
def test_delete_knowledge_base_rejects_tenant_and_owner_with_403(client, role) -> None:
    principal = _fake_principal(role=role)
    app.dependency_overrides[deps.get_principal] = lambda: principal

    response = client.delete(f"/api/v1/knowledge-base/{uuid4()}")

    assert response.status_code == 403

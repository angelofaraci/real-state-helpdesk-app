"""API-level tests for `/api/v1/organizations/*` (super-admin only).
`require_super_admin` itself is unit-tested in `test_deps_admin_guards.py`;
here we only prove HTTP wiring (status codes, response shapes) and that the
router-level dependency actually gates the router — one route is enough to
prove that, since `dependencies=[...]` applies identically to every route.

No live Postgres is reachable in this sandbox (same constraint documented in
`tests/integration/test_auth_flow_pg.py`), so the default-taxonomy-seeding
and rollback-on-conflict tests below exercise the REAL
`organization_service.create_organization` (unlike the other tests in this
file, which monkeypatch it at the module boundary) against a mocked
`AsyncSession` that records every `session.add(...)` call — this proves the
route wiring, the org-first-flush sequencing, and that no taxonomy row is
ever staged when the org name conflicts, without needing a real database.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api import deps
from app.core.session import get_session
from app.main import app
from app.models.category import Category
from app.models.enums import UserRole, UserStatus
from app.models.organization import Organization
from app.models.urgency_level import UrgencyLevel
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


async def _assign_flush_defaults(session: AsyncMock) -> None:
    """Simulate the id/created_at defaults a real Postgres flush would
    assign — `session.flush()` is mocked, so `UUIDPrimaryKeyMixin`'s
    Python-side `default=uuid.uuid4` and `TimestampMixin`'s server-side
    `created_at` default never actually run."""
    for call in session.add.call_args_list:
        instance = call.args[0]
        if getattr(instance, "id", None) is None:
            instance.id = uuid4()
        if hasattr(instance, "created_at") and instance.created_at is None:
            instance.created_at = datetime.now(UTC)


def test_create_organization_persists_default_taxonomy(client) -> None:
    session = AsyncMock()
    session.add = MagicMock()

    async def _flush() -> None:
        await _assign_flush_defaults(session)

    session.flush = AsyncMock(side_effect=_flush)
    app.dependency_overrides[get_session] = lambda: session

    response = client.post("/api/v1/organizations", json={"name": "Acme Corp"})

    assert response.status_code == 201
    added = [call.args[0] for call in session.add.call_args_list]
    assert sum(isinstance(row, Organization) for row in added) == 1
    assert sum(isinstance(row, Category) for row in added) == 6
    assert sum(isinstance(row, UrgencyLevel) for row in added) == 4


def test_create_organization_name_conflict_leaves_no_taxonomy_rows(client) -> None:
    """Rollback proof: when the org-first flush raises `IntegrityError`
    (name conflict), the route still returns 409 AND no taxonomy row was
    ever staged on the session — the seeding step never runs, so there is
    nothing for WU1's `get_session()` rollback-on-error wrapper to undo
    beyond the single conflicting `Organization` insert itself."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush.side_effect = IntegrityError("stmt", {}, Exception("dup"))
    app.dependency_overrides[get_session] = lambda: session

    response = client.post("/api/v1/organizations", json={"name": "Acme Corp"})

    assert response.status_code == 409
    added = [call.args[0] for call in session.add.call_args_list]
    assert sum(isinstance(row, Organization) for row in added) == 1
    assert sum(isinstance(row, (Category, UrgencyLevel)) for row in added) == 0


def test_router_rejects_a_non_super_admin(client) -> None:
    app.dependency_overrides[deps.get_principal] = lambda: _fake_principal(super_admin=False)

    response = client.post("/api/v1/organizations", json={"name": "Acme Corp"})

    assert response.status_code == 404

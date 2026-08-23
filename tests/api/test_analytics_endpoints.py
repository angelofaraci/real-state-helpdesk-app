"""API-level smoke tests for `/api/v1/analytics/*` — RBAC gate and
happy-path response shape, using this codebase's standard `tests/api/`
convention (mocked `AsyncSession` + `get_principal` override, no real
Postgres — mirrors `tests/api/test_urgency_levels_endpoints.py`).

Full correctness of the underlying SQL (exact rates, boundary values,
cross-org isolation, org-local day bucketing) is proven separately against
a real database in `tests/integration/test_analytics_endpoints_pg.py`
(skipped in CI per this repo's `_pg.py` convention). This file exists so
the RBAC gate itself — the single most security-critical property of this
admin-only surface — actually runs in CI, since the `_pg` suite never
does.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.core.session import get_session
from app.main import app
from app.models.enums import UserRole, UserStatus
from app.repositories.organization_repository import OrganizationRepository
from app.services import analytics_service

ANALYTICS_ROUTES = [
    "/api/v1/analytics/overview",
    "/api/v1/analytics/trends?metric_key=tickets_created",
    "/api/v1/analytics/classifier",
    "/api/v1/analytics/rag",
    "/api/v1/analytics/chatbot",
]

NON_ADMIN_ROLES = [UserRole.TENANT, UserRole.OWNER, UserRole.AGENT]


def _fake_principal(role: UserRole = UserRole.ADMIN, organization_id=None):
    return type(
        "FakeUser",
        (),
        {
            "id": uuid4(),
            "organization_id": organization_id or uuid4(),
            "name": "Fake User",
            "email": "fake@example.com",
            "role": role,
            "status": UserStatus.ACTIVE,
        },
    )()


def _fake_organization(organization_id):
    return type("FakeOrganization", (), {"id": organization_id})()


class _FakeScalars:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class _FakeSession:
    """Minimal stand-in for `AsyncSession`, sufficient for `/trends`'
    direct `session.execute(stmt)` call — the other 4 routes never call
    `session.execute` themselves (they only pass `session` through to
    `analytics_service` functions, which are monkeypatched below)."""

    def __init__(self, rows: list | None = None) -> None:
        self._rows = rows or []

    async def execute(self, _stmt):
        return _FakeResult(self._rows)


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = lambda: AsyncMock()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# RBAC — tenant/owner/agent get 403 on every route (task 4.13/4.14)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", NON_ADMIN_ROLES)
@pytest.mark.parametrize("route", ANALYTICS_ROUTES)
def test_non_admin_role_gets_403(client, role, route) -> None:
    app.dependency_overrides[deps.get_principal] = lambda: _fake_principal(role=role)

    response = client.get(route)

    assert response.status_code == 403


@pytest.mark.parametrize("route", ANALYTICS_ROUTES)
def test_admin_role_is_not_rejected_by_rbac(client, route, monkeypatch) -> None:
    """Admin passes the RBAC gate — asserts != 403, not == 200, since the
    happy-path response shape is covered per-route below."""
    admin = _fake_principal(role=UserRole.ADMIN)
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        OrganizationRepository,
        "get_or_404",
        AsyncMock(return_value=_fake_organization(admin.organization_id)),
    )
    monkeypatch.setattr(
        analytics_service, "open_ticket_counts_by_status", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        analytics_service, "open_ticket_counts_by_urgency", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        analytics_service, "sla_window_counts", AsyncMock(return_value=(0, 0))
    )
    monkeypatch.setattr(
        analytics_service, "channel_breakdown_counts", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        analytics_service, "classifier_review_metrics", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        analytics_service, "rag_suggestion_totals", AsyncMock(return_value=(0, 0))
    )
    monkeypatch.setattr(
        analytics_service, "rag_category_breakdown", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        analytics_service, "chatbot_session_totals", AsyncMock(return_value=(0, 0))
    )
    monkeypatch.setattr(
        analytics_service, "chatbot_avg_messages_per_session", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        analytics_service, "chatbot_tool_usage_counts", AsyncMock(return_value=[])
    )
    if "/trends" in route or "/classifier" in route:
        app.dependency_overrides[get_session] = lambda: _FakeSession([])

    response = client.get(route)

    assert response.status_code != 403


# ---------------------------------------------------------------------------
# Happy-path response shape, one per route
# ---------------------------------------------------------------------------


def test_overview_happy_path_shape(client, monkeypatch) -> None:
    admin = _fake_principal(role=UserRole.ADMIN)
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        OrganizationRepository,
        "get_or_404",
        AsyncMock(return_value=_fake_organization(admin.organization_id)),
    )
    monkeypatch.setattr(
        analytics_service,
        "open_ticket_counts_by_status",
        AsyncMock(return_value=[("open", 3)]),
    )
    monkeypatch.setattr(
        analytics_service,
        "open_ticket_counts_by_urgency",
        AsyncMock(return_value=[(uuid4(), "Critical", 2)]),
    )
    monkeypatch.setattr(
        analytics_service, "sla_window_counts", AsyncMock(return_value=(4, 1))
    )
    monkeypatch.setattr(
        analytics_service,
        "channel_breakdown_counts",
        AsyncMock(return_value=[("web", 10)]),
    )

    response = client.get("/api/v1/analytics/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["open_by_status"] == [{"status": "open", "count": 3}]
    assert body["sla_window"] == {"warning": 4, "breached": 1}
    assert body["channel_breakdown_30d"] == [{"channel": "web", "count": 10}]


def test_trends_rejects_unknown_metric_key_with_422(client) -> None:
    app.dependency_overrides[deps.get_principal] = lambda: _fake_principal(
        role=UserRole.ADMIN
    )

    response = client.get("/api/v1/analytics/trends?metric_key=not_a_real_key")

    assert response.status_code == 422


def test_trends_happy_path_shape(client, monkeypatch) -> None:
    app.dependency_overrides[deps.get_principal] = lambda: _fake_principal(
        role=UserRole.ADMIN
    )
    app.dependency_overrides[get_session] = lambda: _FakeSession([])

    response = client.get("/api/v1/analytics/trends?metric_key=tickets_created")

    assert response.status_code == 200
    body = response.json()
    assert body["metric_key"] == "tickets_created"
    assert body["points"] == []


def test_classifier_happy_path_shape(client, monkeypatch) -> None:
    admin = _fake_principal(role=UserRole.ADMIN)
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        OrganizationRepository,
        "get_or_404",
        AsyncMock(return_value=_fake_organization(admin.organization_id)),
    )
    category_id = uuid4()
    monkeypatch.setattr(
        analytics_service,
        "classifier_review_metrics",
        AsyncMock(
            return_value=[(category_id, "Maintenance", 200, Decimal("0.25"))]
        ),
    )
    app.dependency_overrides[get_session] = lambda: _FakeSession([])

    response = client.get("/api/v1/analytics/classifier")

    assert response.status_code == 200
    body = response.json()
    assert body["categories"] == [
        {
            "category_id": str(category_id),
            "category_name": "Maintenance",
            "sample_size": 200,
            "correction_rate": "0.25",
            "needs_review": True,
        }
    ]


def test_rag_happy_path_shape(client, monkeypatch) -> None:
    admin = _fake_principal(role=UserRole.ADMIN)
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        OrganizationRepository,
        "get_or_404",
        AsyncMock(return_value=_fake_organization(admin.organization_id)),
    )
    monkeypatch.setattr(
        analytics_service, "rag_suggestion_totals", AsyncMock(return_value=(10, 4))
    )
    monkeypatch.setattr(
        analytics_service, "rag_category_breakdown", AsyncMock(return_value=[])
    )

    response = client.get("/api/v1/analytics/rag")

    assert response.status_code == 200
    body = response.json()
    assert body["suggestions_generated"] == 10
    assert body["suggestions_used"] == 4
    assert body["usage_rate"] == "0.4"


def test_chatbot_happy_path_shape(client, monkeypatch) -> None:
    admin = _fake_principal(role=UserRole.ADMIN)
    app.dependency_overrides[deps.get_principal] = lambda: admin
    monkeypatch.setattr(
        OrganizationRepository,
        "get_or_404",
        AsyncMock(return_value=_fake_organization(admin.organization_id)),
    )
    monkeypatch.setattr(
        analytics_service, "chatbot_session_totals", AsyncMock(return_value=(8, 2))
    )
    monkeypatch.setattr(
        analytics_service,
        "chatbot_avg_messages_per_session",
        AsyncMock(return_value=Decimal("3.5")),
    )
    monkeypatch.setattr(
        analytics_service,
        "chatbot_tool_usage_counts",
        AsyncMock(return_value=[("escalate_to_human", 2)]),
    )

    response = client.get("/api/v1/analytics/chatbot")

    assert response.status_code == 200
    body = response.json()
    assert body["total_sessions"] == 8
    assert body["escalated_sessions"] == 2
    assert body["escalation_rate"] == "0.25"
    assert body["avg_messages_per_session"] == "3.5"
    assert body["tool_usage"] == [{"tool_name": "escalate_to_human", "count": 2}]

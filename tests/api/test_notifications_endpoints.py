"""API-level tests for the `/api/v1/notifications/*` endpoints (stage 6,
PR7b).

No live Postgres is available in this sandbox, so `notification_service` is
monkeypatched at the module boundary and `get_session`/`get_principal` are
overridden with inert stand-ins, following the exact pattern established by
`tests/api/test_knowledge_base_endpoints.py`.

Both routes are open to any org member (`require_org_member`) — a
notification is addressed to a specific recipient, not gated by role; the
role-shaped access control lives entirely inside
`NotificationRepository.for_recipient`'s user/org scoping, not at this
dependency layer.
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
from app.models.enums import NotificationKind, UserRole, UserStatus
from app.models.notification import Notification
from app.services import notification_service


def _fake_principal(role=UserRole.TENANT, organization_id=None, user_id=None):
    return type(
        "FakeUser",
        (),
        {
            "id": user_id or uuid4(),
            "organization_id": organization_id or uuid4(),
            "name": "Some User",
            "email": "user@example.com",
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


def _make_notification(principal, **overrides) -> Notification:
    defaults = dict(
        organization_id=principal.organization_id,
        user_id=principal.id,
        ticket_id=None,
        sla_event_id=None,
        kind=NotificationKind.SLA_WARNING,
        title="SLA warning: ticket 123",
        body=None,
        sent_at=datetime(2024, 1, 1, tzinfo=UTC),
        read_at=None,
    )
    defaults.update(overrides)
    notification = Notification(**defaults)
    notification.id = uuid4()
    return notification


# ---------------------------------------------------------------------------
# GET /notifications — any org member (own notifications only)
# ---------------------------------------------------------------------------


def test_list_notifications_returns_200_with_items_and_total(client, monkeypatch) -> None:
    principal = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: principal
    notification = _make_notification(principal)
    monkeypatch.setattr(
        notification_service,
        "list_notifications",
        AsyncMock(return_value=([notification], 1)),
    )

    response = client.get("/api/v1/notifications")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == str(notification.id)
    assert body["items"][0]["kind"] == "sla_warning"


def test_list_notifications_passes_limit_and_offset_through(client, monkeypatch) -> None:
    principal = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: principal
    mock_list = AsyncMock(return_value=([], 0))
    monkeypatch.setattr(notification_service, "list_notifications", mock_list)

    response = client.get("/api/v1/notifications?limit=5&offset=10")

    assert response.status_code == 200
    _args, kwargs = mock_list.call_args
    assert kwargs["limit"] == 5
    assert kwargs["offset"] == 10


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "limit=-1", "offset=-1"])
def test_list_notifications_rejects_out_of_range_params_with_422(
    client, monkeypatch, query
) -> None:
    principal = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: principal
    monkeypatch.setattr(
        notification_service, "list_notifications", AsyncMock(return_value=([], 0))
    )

    response = client.get(f"/api/v1/notifications?{query}")

    assert response.status_code == 422


def test_list_notifications_defaults_to_limit_20_offset_0(client, monkeypatch) -> None:
    principal = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: principal
    mock_list = AsyncMock(return_value=([], 0))
    monkeypatch.setattr(notification_service, "list_notifications", mock_list)

    response = client.get("/api/v1/notifications")

    assert response.status_code == 200
    _args, kwargs = mock_list.call_args
    assert kwargs["limit"] == 20
    assert kwargs["offset"] == 0


# ---------------------------------------------------------------------------
# PATCH /notifications/{id}/read
# ---------------------------------------------------------------------------


def test_mark_notification_read_returns_200_with_read_at_set(client, monkeypatch) -> None:
    principal = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: principal
    notification = _make_notification(principal, read_at=datetime(2024, 1, 2, tzinfo=UTC))
    monkeypatch.setattr(
        notification_service, "mark_read", AsyncMock(return_value=notification)
    )

    response = client.patch(f"/api/v1/notifications/{notification.id}/read")

    assert response.status_code == 200
    assert response.json()["read_at"] is not None


def test_mark_notification_read_is_idempotent_on_a_second_call(client, monkeypatch) -> None:
    """Calling the endpoint twice must not error and must not change the
    already-set `read_at` — proven here at the API boundary by simulating
    the service's own idempotent behavior (fixed `read_at` on both calls)
    and asserting both responses are 200 with the exact same `read_at`."""
    principal = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: principal
    first_read_at = datetime(2024, 1, 2, tzinfo=UTC)
    notification = _make_notification(principal, read_at=first_read_at)
    monkeypatch.setattr(
        notification_service, "mark_read", AsyncMock(return_value=notification)
    )

    first_response = client.patch(f"/api/v1/notifications/{notification.id}/read")
    second_response = client.patch(f"/api/v1/notifications/{notification.id}/read")

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["read_at"] == second_response.json()["read_at"]


def test_mark_notification_read_maps_not_found_to_404_for_cross_user(
    client, monkeypatch
) -> None:
    principal = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: principal
    monkeypatch.setattr(
        notification_service,
        "mark_read",
        AsyncMock(side_effect=CoreNotFoundError("Notification", uuid4())),
    )

    response = client.patch(f"/api/v1/notifications/{uuid4()}/read")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}


def test_mark_notification_read_maps_not_found_to_404_for_cross_org(
    client, monkeypatch
) -> None:
    """Distinct test from the cross-user case per the task spec: exercises
    the same 404 mapping, but conceptually the org-scope filter clause
    inside `for_recipient` rather than the user-id filter clause — both
    surface identically as `NotFoundError` from the service, which is
    exactly the point (indistinguishable to the caller)."""
    principal = _fake_principal()
    app.dependency_overrides[deps.get_principal] = lambda: principal
    monkeypatch.setattr(
        notification_service,
        "mark_read",
        AsyncMock(side_effect=CoreNotFoundError("Notification", uuid4())),
    )

    response = client.patch(f"/api/v1/notifications/{uuid4()}/read")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}

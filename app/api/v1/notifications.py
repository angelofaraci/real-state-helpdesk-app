"""Org-scoped notification endpoints (stage 6, PR7b).

Both routes are open to any org member (`require_org_member`) — a
notification is addressed to a specific recipient, not a staff-only
concept: every role can read/ack their own notifications, and the
role-shaped access control this router would otherwise need is unnecessary
because `notification_service.list_notifications`/`get_notification_or_404`
already narrow to `scope.user_id` (see
`app.repositories.notification_repository.NotificationRepository
.for_recipient`) — there is no cross-user subset a role gate would need to
further restrict, unlike `app.api.v1.tickets`.

`app.core.exceptions.NotFoundError` is handled globally (see `app.main`),
so `PATCH /notifications/{id}/read`'s 404 mapping needs no explicit
`except` here — a notification belonging to another user or another
organization surfaces as the exact same 404 either way (see
`notification_service.get_notification_or_404`'s docstring).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_org_member
from app.core.scope import OrgScope
from app.core.session import get_session
from app.models.notification import Notification
from app.schemas.notification import (
    NOTIFICATION_LIST_LIMIT_DEFAULT,
    NOTIFICATION_LIST_LIMIT_MAX,
    NOTIFICATION_LIST_LIMIT_MIN,
    NotificationListResponse,
    NotificationResponse,
)
from app.services import notification_service

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    limit: int = Query(
        default=NOTIFICATION_LIST_LIMIT_DEFAULT,
        ge=NOTIFICATION_LIST_LIMIT_MIN,
        le=NOTIFICATION_LIST_LIMIT_MAX,
    ),
    offset: int = Query(default=0, ge=0),
    scope: OrgScope = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
) -> NotificationListResponse:
    """Page through the caller's own notifications within their
    organization, newest first. `limit` out of `[1, 100]` (or a negative
    `offset`) is rejected with 422 by FastAPI/Pydantic's own `Query`
    validation, never silently clamped."""
    items, total = await notification_service.list_notifications(
        session, scope=scope, limit=limit, offset=offset
    )
    return NotificationListResponse(
        items=[NotificationResponse.model_validate(item) for item in items], total=total
    )


@router.patch("/{notification_id}/read", response_model=NotificationResponse)
async def mark_notification_read(
    notification_id: UUID,
    scope: OrgScope = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
) -> Notification:
    """Mark one of the caller's own notifications as read. Idempotent —
    an already-read notification's `read_at` is left untouched by a
    second call (see `notification_service.mark_read`). A notification
    belonging to another user or another organization surfaces as 404,
    never 403 — indistinguishable from a missing id."""
    return await notification_service.mark_read(
        session, scope=scope, notification_id=notification_id
    )

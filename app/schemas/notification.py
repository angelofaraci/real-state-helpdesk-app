"""Pydantic request/response schemas for the `/api/v1/notifications`
endpoints (stage 6, PR7b).

Pagination convention: this codebase has no pre-existing paginated
list-endpoint response shape to mirror (verified — no other router returns
`limit`/`offset`/`items`/`total`/`has_more`-shaped responses as of stage
6), so this module establishes one: `items` + `total`, where `total` is the
full matching count regardless of `limit`/`offset` (lets a caller compute
`has_more` / a page count client-side without a second request).

`limit` is bounded via `Field(..., ge=1, le=100)` on `NotificationListQuery`
and read by the route through FastAPI's `Query(...)` (see
`app.api.v1.notifications`) so that an out-of-range value is REJECTED with
422 by FastAPI/Pydantic's own validation — never silently clamped. This
mirrors the only bounded-query-param precedent in the codebase in spirit
(`app.api.v1.users`'s `Query(default=..., alias=...)` usage), generalized
with `ge`/`le` since no prior endpoint needed numeric bounds.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.enums import NotificationKind

# Bounds enforced on `GET /notifications`'s `limit` query param — kept here
# (not duplicated as magic numbers in the router) so the router and any
# future caller/test share a single source of truth.
NOTIFICATION_LIST_LIMIT_DEFAULT = 20
NOTIFICATION_LIST_LIMIT_MIN = 1
NOTIFICATION_LIST_LIMIT_MAX = 100


class NotificationResponse(BaseModel):
    id: UUID
    kind: NotificationKind
    title: str
    body: str | None
    ticket_id: UUID | None
    sla_event_id: UUID | None
    sent_at: datetime
    read_at: datetime | None

    model_config = {"from_attributes": True}


class NotificationListResponse(BaseModel):
    """`items` + `total` — see module docstring for why this shape was
    chosen over alternatives (e.g. `has_more`)."""

    items: list[NotificationResponse]
    total: int


class NotificationListQuery(BaseModel):
    """Documents the `limit`/`offset` bounds enforced on `GET
    /notifications`. Not bound directly as a FastAPI query-parameter model
    (the router uses individual `Query(...)` params instead, matching this
    codebase's existing convention — see `app.api.v1.users`); this class
    exists so the bounds have a single, testable, importable definition.
    """

    limit: int = Field(
        default=NOTIFICATION_LIST_LIMIT_DEFAULT,
        ge=NOTIFICATION_LIST_LIMIT_MIN,
        le=NOTIFICATION_LIST_LIMIT_MAX,
    )
    offset: int = Field(default=0, ge=0)

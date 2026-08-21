"""`NotificationRepository` — direct-scoped (`Notification` owns its own
`organization_id` column, so `scope_path` stays empty).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select

from app.models.notification import Notification
from app.repositories.base import ScopedRepository


class NotificationRepository(ScopedRepository[Notification]):
    model = Notification

    def for_recipient(self, user_id: UUID) -> Select[tuple[Notification]]:
        """A scoped `SELECT` of notifications addressed to `user_id`
        within the current organization scope, newest first — backs the
        paginated `GET /notifications` and `PATCH /notifications/:id/read`
        a later PR builds. A notification belonging to another user in the
        same org is never matched: cross-user access must be invisible,
        not filtered after the fact. Ordering matches the raw
        `ix_notifications_user_id_sent_at_id` index (`sent_at DESC, id
        DESC`).
        """
        return (
            self.select()
            .where(Notification.user_id == user_id)
            .order_by(Notification.sent_at.desc(), Notification.id.desc())
        )

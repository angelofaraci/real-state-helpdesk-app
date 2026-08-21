"""Notification model — a single in-app notification delivered to a user
(currently only SLA warning/breach kinds), owned by an organization.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin
from app.models.enums import NotificationKind


class Notification(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "notifications"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # The recipient.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # Provenance: the SlaEvent that triggered this notification, if any.
    sla_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sla_events.id", ondelete="RESTRICT"),
        nullable=True,
    )
    kind: Mapped[NotificationKind] = mapped_column(
        SAEnum(
            NotificationKind,
            name="notification_kind",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    read_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "btrim(title) <> ''", name="ck_notifications_title_not_blank"
        ),
        Index("ix_notifications_organization_id", "organization_id"),
        Index("ix_notifications_ticket_id", "ticket_id"),
        # Ordered composite index (`sent_at DESC, id DESC`) backing the
        # paginated `GET /notifications` a later PR builds. Migration 0009
        # is the actual DDL source of truth (raw SQL there, same reason as
        # `SlaEvent.ux_sla_events_ticket_event_once`); this declaration
        # only documents its shape at the ORM metadata level.
        Index(
            "ix_notifications_user_id_sent_at_id",
            "user_id",
            text("sent_at DESC"),
            text("id DESC"),
        ),
    )

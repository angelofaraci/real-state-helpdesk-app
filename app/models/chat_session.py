"""ChatSession model — a single chatbot conversation (stage 4 — chatbot).

A session may be anonymous (`user_id IS NULL`, e.g. a public widget visitor
who has not authenticated) or tied to an authenticated user. It only ever
links to a `ticket_id` once escalated to a human agent, and only when it has
a real (non-anonymous) `user_id` — see `ck_chat_sessions_ticket_requires_user`.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Integer
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ChatSessionStatus


class ChatSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "chat_sessions"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Nullable: an anonymous chat-widget visitor has no authenticated user.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Nullable: set only once the session is escalated to a human agent and
    # a ticket is created for it.
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[ChatSessionStatus] = mapped_column(
        SAEnum(
            ChatSessionStatus,
            name="chat_session_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        server_default=ChatSessionStatus.ACTIVE.value,
    )
    low_confidence_streak: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # A session can only be linked to an escalation ticket if it has a
        # real (non-anonymous) user_id — an escalated ticket must be
        # traceable to a real reporter, never the anonymous chat sentinel.
        CheckConstraint(
            "ticket_id IS NULL OR user_id IS NOT NULL",
            name="ck_chat_sessions_ticket_requires_user",
        ),
        Index("ix_chat_sessions_organization_id", "organization_id"),
        Index("ix_chat_sessions_organization_id_user_id", "organization_id", "user_id"),
        Index("ix_chat_sessions_ticket_id", "ticket_id"),
    )

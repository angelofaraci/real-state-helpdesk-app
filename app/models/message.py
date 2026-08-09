"""Message model — a single entry in a ticket's conversation thread."""

import uuid

from sqlalchemy import Boolean, CheckConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import AuthorType


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "messages"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    author_type: Mapped[AuthorType] = mapped_column(
        SAEnum(
            AuthorType,
            name="author_type",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_ai_suggestion: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    __table_args__ = (
        CheckConstraint("btrim(content) <> ''", name="ck_messages_content_not_blank"),
        Index("ix_messages_ticket_id_created_at_id", "ticket_id", "created_at", "id"),
    )

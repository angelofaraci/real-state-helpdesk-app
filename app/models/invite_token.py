"""Invite token model — one-time tokens used to activate a pending user."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, LargeBinary, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class InviteToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "invite_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_invite_tokens_token_hash_unique", "token_hash", unique=True),
        # Enforces "single outstanding invite per user": at most one row per
        # user_id may have used_at IS NULL at a time.
        Index(
            "ix_invite_tokens_user_id_single_outstanding",
            "user_id",
            unique=True,
            postgresql_where=text("used_at IS NULL"),
        ),
        Index("ix_invite_tokens_user_id", "user_id"),
    )

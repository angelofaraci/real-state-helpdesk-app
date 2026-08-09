"""Refresh token model — supports rotating JWT refresh tokens."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, LargeBinary, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RefreshToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_refresh_tokens_token_hash_unique", "token_hash", unique=True),
        Index(
            "ix_refresh_tokens_family_id_active",
            "family_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_refresh_tokens_user_id", "user_id"),
    )

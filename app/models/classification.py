"""Classification model — the AI category/confidence prediction for a ticket."""

import uuid

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Classification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "classifications"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    predicted_category: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    human_corrected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    model_used: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence BETWEEN 0 AND 1)",
            name="ck_classifications_confidence_range",
        ),
        CheckConstraint(
            "model_used IS NULL OR model_used IN ('sklearn_v1', 'llm_fallback')",
            name="ck_classifications_model_used_known",
        ),
        Index("ix_classifications_ticket_id_unique", "ticket_id", unique=True),
    )

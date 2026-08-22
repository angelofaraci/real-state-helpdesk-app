"""Urgency level model — SLA-bound priority tiers, owned by an organization."""

import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UrgencyLevel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "urgency_levels"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    sla_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # Stage 6 — queues + SLA (migration 0008). Whether this urgency level's
    # SLA clock pauses outside the organization's business hours (e.g.
    # Critical/High are typically 24/7 — `False` — while Medium/Low pause
    # overnight/weekends — `True`). See `app.core.taxonomy_defaults`.
    respects_business_hours: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    __table_args__ = (
        CheckConstraint("sla_hours > 0", name="ck_urgency_levels_sla_hours_positive"),
        Index(
            "ix_urgency_levels_organization_id_name_lower_unique",
            "organization_id",
            text("lower(btrim(name))"),
            unique=True,
        ),
        Index(
            "ix_urgency_levels_organization_id_active_sort_order",
            "organization_id",
            "active",
            "sort_order",
        ),
    )

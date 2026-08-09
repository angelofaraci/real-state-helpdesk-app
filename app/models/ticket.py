"""Ticket model — the central support-request aggregate."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import TicketChannel, TicketStatus


class Ticket(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tickets"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    property_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("properties.id", ondelete="RESTRICT"),
        nullable=True,
    )
    contract_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contracts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # ON DELETE RESTRICT is deliberate: it is what makes the taxonomy
    # hard-delete-or-deactivate logic (a later work unit) safe, since a
    # category/urgency level referenced by any ticket cannot be removed.
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=False,
    )
    urgency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("urgency_levels.id", ondelete="RESTRICT"),
        nullable=False,
    )
    channel: Mapped[TicketChannel] = mapped_column(
        SAEnum(
            TicketChannel,
            name="ticket_channel",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        server_default=TicketChannel.WEB.value,
    )
    status: Mapped[TicketStatus] = mapped_column(
        SAEnum(
            TicketStatus,
            name="ticket_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        server_default=TicketStatus.OPEN.value,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    sla_due_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL)",
            name="ck_tickets_closed_at_matches_status",
        ),
        Index("ix_tickets_organization_id_user_id", "organization_id", "user_id"),
        Index("ix_tickets_property_id", "property_id"),
        Index("ix_tickets_contract_id", "contract_id"),
        Index("ix_tickets_organization_id_status", "organization_id", "status"),
        Index("ix_tickets_organization_id_agent_id", "organization_id", "agent_id"),
        Index(
            "ix_tickets_organization_id_created_at_id",
            "organization_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index("ix_tickets_category_id", "category_id"),
        Index("ix_tickets_urgency_id", "urgency_id"),
    )

"""SlaEvent model — an audit-trail row recorded each time a ticket's SLA
clock crosses a meaningful boundary (warning, breach, or resolution),
owned by an organization.
"""

import uuid
from datetime import datetime

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin
from app.models.enums import SlaEventType


class SlaEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "sla_events"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event: Mapped[SlaEventType] = mapped_column(
        SAEnum(
            SlaEventType,
            name="sla_event_type",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    # Audit snapshot of `Ticket.sla_due_at` at the moment this event was
    # recorded. Nullable: not every future event kind necessarily carries
    # one.
    sla_due_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_sla_events_ticket_id_occurred_at", "ticket_id", "occurred_at"),
        Index("ix_sla_events_organization_id_event", "organization_id", "event"),
        # The sole DB-level idempotency guard the (later PR's) SLA-monitor
        # cron relies on to avoid emitting the same warning/breach event
        # twice for one ticket. `resolved` is deliberately excluded — it
        # may legitimately repeat across a ticket's reopen/re-resolve
        # cycles. Migration 0009 is the actual DDL source of truth (raw SQL
        # there, same reason as `Ticket.ix_tickets_channel_metadata_message_ids`);
        # this declaration only documents its shape at the ORM metadata
        # level.
        Index(
            "ux_sla_events_ticket_event_once",
            "ticket_id",
            "event",
            unique=True,
            postgresql_where=text("event IN ('warning', 'breached')"),
        ),
    )

"""Ticket model — the central support-request aggregate."""

import uuid
from datetime import datetime

from typing import Any

from sqlalchemy import CheckConstraint, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ClassificationStatus, TicketChannel, TicketStatus


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
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ON DELETE RESTRICT is deliberate: it is what makes the taxonomy
    # hard-delete-or-deactivate logic (a later work unit) safe, since a
    # category/urgency level referenced by any ticket cannot be removed.
    # Nullable: a ticket may exist before classification assigns taxonomy
    # (see ck_tickets_classified_has_taxonomy below).
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=True,
    )
    urgency_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("urgency_levels.id", ondelete="RESTRICT"),
        nullable=True,
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
    classification_status: Mapped[ClassificationStatus] = mapped_column(
        SAEnum(
            ClassificationStatus,
            name="classification_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        server_default=ClassificationStatus.PENDING.value,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    sla_due_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # Stage 5 — multichannel (migration 0007). Per-channel bookkeeping
    # (inbound message ids for dedup, email thread key, etc.) — always
    # read/written through `app.services.channel_metadata`'s pure helpers,
    # never mutated in place (see that module's docstring for why).
    channel_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL)",
            name="ck_tickets_closed_at_matches_status",
        ),
        CheckConstraint("btrim(title) <> ''", name="ck_tickets_title_not_blank"),
        CheckConstraint(
            "classification_status <> 'classified' OR "
            "(category_id IS NOT NULL AND urgency_id IS NOT NULL AND "
            "sla_due_at IS NOT NULL)",
            name="ck_tickets_classified_has_taxonomy",
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
        Index(
            "ix_tickets_organization_id_classification_status",
            "organization_id",
            "classification_status",
        ),
        # GIN index over the `message_ids` path, for the inbound-message
        # dedup lookup `app.services.channel_metadata.contains` backs.
        # `postgresql_ops` (`jsonb_path_ops`) cannot be expressed through
        # this `text()`-based `Index()` (SQLAlchemy's PG DDL compiler keys
        # `postgresql_ops` off `expr.key`, which a bare `TextClause` has
        # none of) — migration 0007 is the actual DDL source of truth and
        # creates the real index (with `jsonb_path_ops`) via raw SQL; this
        # declaration only documents its shape at the ORM metadata level.
        Index(
            "ix_tickets_channel_metadata_message_ids",
            text("(channel_metadata -> 'message_ids')"),
            postgresql_using="gin",
        ),
    )

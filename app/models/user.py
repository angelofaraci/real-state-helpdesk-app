"""User model — represents tenants, owners, agents, and admins."""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import TicketChannel, UserRole, UserStatus


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(
            UserRole,
            name="user_role",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(
            UserStatus,
            name="user_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        server_default=UserStatus.PENDING.value,
    )
    # Stage 5 — multichannel (migration 0007). Global (NOT org-scoped)
    # unique phone number in E.164 form — see
    # `ck_users_phone_number_e164` — used to resolve/auto-provision a
    # WhatsApp sender's identity (`app.services.channel_identity`).
    phone_number: Mapped[str | None] = mapped_column(String, nullable=True)
    # Set only for a user auto-provisioned by an inbound channel message
    # (never for a normally-created/invited user, which stays `NULL`) —
    # see `app.services.channel_identity`.
    auto_provisioned_channel: Mapped[TicketChannel | None] = mapped_column(
        SAEnum(
            TicketChannel,
            name="ticket_channel",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status = 'pending' OR password_hash IS NOT NULL",
            name="ck_users_password_hash_required_unless_pending",
        ),
        CheckConstraint(
            "organization_id IS NOT NULL OR role = 'admin'",
            name="ck_users_organization_required_unless_admin",
        ),
        CheckConstraint(
            r"phone_number IS NULL OR phone_number ~ '^\+[1-9][0-9]{6,14}$'",
            name="ck_users_phone_number_e164",
        ),
        Index("ix_users_email_lower_unique", text("lower(email)"), unique=True),
        Index("ix_users_organization_id_role", "organization_id", "role"),
        Index("ix_users_organization_id_status", "organization_id", "status"),
        Index("ix_users_phone_number_unique", "phone_number", unique=True),
    )

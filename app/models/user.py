"""User model — represents tenants, owners, agents, and admins."""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import UserRole, UserStatus


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

    __table_args__ = (
        CheckConstraint(
            "status = 'pending' OR password_hash IS NOT NULL",
            name="ck_users_password_hash_required_unless_pending",
        ),
        CheckConstraint(
            "organization_id IS NOT NULL OR role = 'admin'",
            name="ck_users_organization_required_unless_admin",
        ),
        Index("ix_users_email_lower_unique", text("lower(email)"), unique=True),
        Index("ix_users_organization_id_role", "organization_id", "role"),
        Index("ix_users_organization_id_status", "organization_id", "status"),
    )

"""Property model — a managed real-estate unit owned by a user."""

import uuid
from datetime import datetime

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import PropertyType


class Property(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "properties"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    address: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[PropertyType] = mapped_column(
        SAEnum(
            PropertyType,
            name="property_type",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "ix_properties_organization_id_active",
            "organization_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_properties_owner_id", "owner_id"),
        Index("ix_properties_organization_id", "organization_id"),
    )

"""Contract model — a tenancy agreement linking a tenant to a property.

Deliberately has NO `organization_id` column: it is scoped for multi-tenancy
via its `property_id` -> `properties.organization_id` join, per the approved
design.
"""

import uuid
from datetime import date

from sqlalchemy import CheckConstraint, Date
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ContractStatus


class Contract(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contracts"

    property_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("properties.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[ContractStatus] = mapped_column(
        SAEnum(
            ContractStatus,
            name="contract_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        server_default=ContractStatus.ACTIVE.value,
    )

    __table_args__ = (
        CheckConstraint(
            "end_date >= start_date", name="ck_contracts_end_date_after_start_date"
        ),
        Index("ix_contracts_property_id", "property_id"),
        Index("ix_contracts_tenant_id", "tenant_id"),
        Index(
            "ix_contracts_property_id_active",
            "property_id",
            postgresql_where=text("status = 'active'"),
        ),
    )

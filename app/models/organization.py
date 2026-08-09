"""Organization model — the tenancy root for most other aggregates."""

from sqlalchemy import CheckConstraint, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_organizations_name_not_blank"),
        Index(
            "ix_organizations_name_lower_unique",
            text("lower(btrim(name))"),
            unique=True,
        ),
    )

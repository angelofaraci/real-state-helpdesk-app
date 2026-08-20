"""Organization model — the tenancy root for most other aggregates."""

from sqlalchemy import CheckConstraint, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String, nullable=False)
    # Stage 4 — chatbot: the public, unguessable key embedded in an
    # organization's chat-widget snippet to identify which org an anonymous
    # visitor's session belongs to. Migration 0005 adds this column
    # nullable, backfills every existing row with a unique generated key,
    # then sets it NOT NULL — so at the ORM level every org (existing and
    # new) always has exactly one. Generated at organization-creation time
    # in `organization_service.create_organization`.
    chat_widget_key: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_organizations_name_not_blank"),
        Index(
            "ix_organizations_name_lower_unique",
            text("lower(btrim(name))"),
            unique=True,
        ),
        Index(
            "ix_organizations_chat_widget_key_unique",
            "chat_widget_key",
            unique=True,
        ),
    )

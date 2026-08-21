"""Organization model — the tenancy root for most other aggregates."""

from sqlalchemy import CheckConstraint, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
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

    # Stage 5 — multichannel (migration 0007). WhatsApp Cloud API config:
    # both columns are nullable, but always set together — see
    # `ck_organizations_whatsapp_config_complete` below — an org with no
    # WhatsApp channel configured has both `NULL`. The access token is
    # NEVER stored in plaintext; `organization_service.update_organization`
    # encrypts it via `app.core.crypto.encrypt_secret` before it ever
    # reaches this column.
    whatsapp_phone_number_id: Mapped[str | None] = mapped_column(String, nullable=True)
    whatsapp_access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The inbound-email address routed to this org (e.g. via a Mailgun
    # route), matched case-insensitively — see
    # `ix_organizations_support_email_address_lower_unique` below.
    support_email_address: Mapped[str | None] = mapped_column(String, nullable=True)

    # Stage 6 — queues + SLA (migration 0008). IANA timezone the org's
    # business hours are interpreted in, and the weekly business-hours
    # schedule itself (see `app.core.sla_defaults` for the default shape
    # every org is bootstrapped with). Both NOT NULL — every org always has
    # exactly one of each.
    timezone: Mapped[str] = mapped_column(String, nullable=False)
    business_hours: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_organizations_name_not_blank"),
        CheckConstraint(
            "(whatsapp_phone_number_id IS NULL) = (whatsapp_access_token_encrypted IS NULL)",
            name="ck_organizations_whatsapp_config_complete",
        ),
        CheckConstraint(
            "btrim(timezone) <> ''", name="ck_organizations_timezone_not_blank"
        ),
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
        Index(
            "ix_organizations_whatsapp_phone_number_id_unique",
            "whatsapp_phone_number_id",
            unique=True,
        ),
        Index(
            "ix_organizations_support_email_address_lower_unique",
            text("lower(support_email_address)"),
            unique=True,
        ),
    )

    @property
    def whatsapp_access_token_set(self) -> bool:
        """`True` iff a WhatsApp access token is currently stored for this
        org — used by `OrganizationResponse` to expose a presence flag
        without ever exposing the token (plaintext or ciphertext) itself."""
        return self.whatsapp_access_token_encrypted is not None

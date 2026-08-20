"""stage 5 multichannel foundation: organizations WhatsApp/email channel
configuration, users.phone_number + auto_provisioned_channel,
tickets.channel_metadata, chat_sessions.channel_metadata + channel.

Fully additive/nullable — no `CREATE TYPE`/`ALTER TYPE` needed. The
`ticket_channel` Postgres enum type (created in 0001_initial.py) already
has the `web`/`email`/`whatsapp` labels this migration's two new enum
columns need (see `app/models/enums.py::TicketChannel`), so both
`users.auto_provisioned_channel` and `chat_sessions.channel` merely
reference the EXISTING type via `create_type=False` — same pattern as
0005's `_chat_session_status_enum()`/`_chat_message_role_enum()` helpers.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-20

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TICKET_CHANNEL_VALUES = ["web", "email", "whatsapp"]


def _ticket_channel_enum() -> postgresql.ENUM:
    return postgresql.ENUM(*_TICKET_CHANNEL_VALUES, name="ticket_channel", create_type=False)


def upgrade() -> None:
    # --- organizations: WhatsApp + email channel configuration ---------
    op.add_column(
        "organizations",
        sa.Column("whatsapp_phone_number_id", sa.String(), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("whatsapp_access_token_encrypted", sa.Text(), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("support_email_address", sa.String(), nullable=True),
    )
    op.create_check_constraint(
        "ck_organizations_whatsapp_config_complete",
        "organizations",
        "(whatsapp_phone_number_id IS NULL) = (whatsapp_access_token_encrypted IS NULL)",
    )
    op.create_index(
        "ix_organizations_whatsapp_phone_number_id_unique",
        "organizations",
        ["whatsapp_phone_number_id"],
        unique=True,
    )
    # Functional (case-insensitive) unique index — mirrors
    # `ix_users_email_lower_unique` exactly. Alembic's `op.create_index`
    # has no first-class support for a bare functional expression here, so
    # this is raw SQL, same convention already used for the pgvector HNSW
    # indexes in 0004_rag_stage3.py.
    op.execute(
        "CREATE UNIQUE INDEX ix_organizations_support_email_address_lower_unique "
        "ON organizations (lower(support_email_address))"
    )

    # --- users: global phone_number + auto-provisioning channel --------
    op.add_column("users", sa.Column("phone_number", sa.String(), nullable=True))
    op.add_column(
        "users",
        sa.Column("auto_provisioned_channel", _ticket_channel_enum(), nullable=True),
    )
    op.create_check_constraint(
        "ck_users_phone_number_e164",
        "users",
        r"phone_number IS NULL OR phone_number ~ '^\+[1-9][0-9]{6,14}$'",
    )
    op.create_index(
        "ix_users_phone_number_unique", "users", ["phone_number"], unique=True
    )

    # --- tickets: channel_metadata (message dedup ids etc.) -------------
    op.add_column(
        "tickets",
        sa.Column("channel_metadata", postgresql.JSONB(), nullable=True),
    )
    op.execute(
        "CREATE INDEX ix_tickets_channel_metadata_message_ids ON tickets "
        "USING gin ((channel_metadata -> 'message_ids') jsonb_path_ops)"
    )

    # --- chat_sessions: channel_metadata + channel -----------------------
    op.add_column(
        "chat_sessions",
        sa.Column("channel_metadata", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "chat_sessions",
        sa.Column(
            "channel", _ticket_channel_enum(), nullable=False, server_default="web"
        ),
    )
    op.execute(
        "CREATE INDEX ix_chat_sessions_org_wa_id ON chat_sessions "
        "(organization_id, (channel_metadata ->> 'wa_id'))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_chat_sessions_org_wa_id")
    op.drop_column("chat_sessions", "channel")
    op.drop_column("chat_sessions", "channel_metadata")

    op.execute("DROP INDEX ix_tickets_channel_metadata_message_ids")
    op.drop_column("tickets", "channel_metadata")

    op.drop_index("ix_users_phone_number_unique", table_name="users")
    op.drop_constraint("ck_users_phone_number_e164", "users", type_="check")
    op.drop_column("users", "auto_provisioned_channel")
    op.drop_column("users", "phone_number")

    op.execute("DROP INDEX ix_organizations_support_email_address_lower_unique")
    op.drop_index(
        "ix_organizations_whatsapp_phone_number_id_unique", table_name="organizations"
    )
    op.drop_constraint(
        "ck_organizations_whatsapp_config_complete", "organizations", type_="check"
    )
    op.drop_column("organizations", "support_email_address")
    op.drop_column("organizations", "whatsapp_access_token_encrypted")
    op.drop_column("organizations", "whatsapp_phone_number_id")

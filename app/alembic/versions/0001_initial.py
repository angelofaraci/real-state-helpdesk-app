"""initial schema: organizations, users, and ticketing tables

Revision ID: 0001
Revises:
Create Date: 2026-08-09

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Native Postgres enum types, created explicitly via raw SQL below so table
# creation can reference them with `create_type=False`. This avoids
# SQLAlchemy's implicit `checkfirst` existence query, which requires a live
# connection and would break Alembic's offline SQL generation.
_ENUM_TYPES: dict[str, list[str]] = {
    "user_role": ["tenant", "owner", "agent", "admin"],
    "user_status": ["pending", "active", "disabled"],
    "property_type": ["apartment", "house", "commercial", "other"],
    "contract_status": ["active", "expired", "terminated"],
    "ticket_channel": ["web", "email", "whatsapp"],
    "ticket_status": [
        "open",
        "in_progress",
        "waiting_on_customer",
        "resolved",
        "closed",
    ],
    "author_type": ["user", "agent", "bot"],
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*_ENUM_TYPES[name], name=name, create_type=False)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    for type_name, labels in _ENUM_TYPES.items():
        labels_sql = ", ".join(f"'{label}'" for label in labels)
        op.execute(f"CREATE TYPE {type_name} AS ENUM ({labels_sql})")

    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_organizations_name_not_blank"),
    )
    op.create_index(
        "ix_organizations_name_lower_unique",
        "organizations",
        [sa.text("lower(btrim(name))")],
        unique=True,
    )

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("role", _enum("user_role"), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column(
            "status", _enum("user_status"), nullable=False, server_default="pending"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status = 'pending' OR password_hash IS NOT NULL",
            name="ck_users_password_hash_required_unless_pending",
        ),
        sa.CheckConstraint(
            "organization_id IS NOT NULL OR role = 'admin'",
            name="ck_users_organization_required_unless_admin",
        ),
    )
    op.create_index(
        "ix_users_email_lower_unique", "users", [sa.text("lower(email)")], unique=True
    )
    op.create_index("ix_users_organization_id_role", "users", ["organization_id", "role"])
    op.create_index(
        "ix_users_organization_id_status", "users", ["organization_id", "status"]
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_refresh_tokens_token_hash_unique", "refresh_tokens", ["token_hash"], unique=True
    )
    op.create_index(
        "ix_refresh_tokens_family_id_active",
        "refresh_tokens",
        ["family_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])

    op.create_table(
        "invite_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_invite_tokens_token_hash_unique", "invite_tokens", ["token_hash"], unique=True
    )
    # Enforces "single outstanding invite per user".
    op.create_index(
        "ix_invite_tokens_user_id_single_outstanding",
        "invite_tokens",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("used_at IS NULL"),
    )
    op.create_index("ix_invite_tokens_user_id", "invite_tokens", ["user_id"])

    op.create_table(
        "properties",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("type", _enum("property_type"), nullable=False),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_properties_organization_id_active",
        "properties",
        ["organization_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_properties_owner_id", "properties", ["owner_id"])
    op.create_index("ix_properties_organization_id", "properties", ["organization_id"])

    op.create_table(
        "contracts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "property_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("properties.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column(
            "status", _enum("contract_status"), nullable=False, server_default="active"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "end_date >= start_date", name="ck_contracts_end_date_after_start_date"
        ),
    )
    op.create_index("ix_contracts_property_id", "contracts", ["property_id"])
    op.create_index("ix_contracts_tenant_id", "contracts", ["tenant_id"])
    op.create_index(
        "ix_contracts_property_id_active",
        "contracts",
        ["property_id"],
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_categories_organization_id_name_lower_unique",
        "categories",
        ["organization_id", sa.text("lower(btrim(name))")],
        unique=True,
    )
    op.create_index(
        "ix_categories_organization_id_active", "categories", ["organization_id", "active"]
    )

    op.create_table(
        "urgency_levels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("sla_hours", sa.Integer(), nullable=False),
        sa.Column(
            "sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("sla_hours > 0", name="ck_urgency_levels_sla_hours_positive"),
    )
    op.create_index(
        "ix_urgency_levels_organization_id_name_lower_unique",
        "urgency_levels",
        ["organization_id", sa.text("lower(btrim(name))")],
        unique=True,
    )
    op.create_index(
        "ix_urgency_levels_organization_id_active_sort_order",
        "urgency_levels",
        ["organization_id", "active", "sort_order"],
    )

    op.create_table(
        "tickets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "property_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("properties.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "contract_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contracts.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        # ON DELETE RESTRICT is deliberate: it is what makes the taxonomy
        # hard-delete-or-deactivate logic (a later work unit) safe.
        sa.Column(
            "category_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("categories.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "urgency_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("urgency_levels.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "channel", _enum("ticket_channel"), nullable=False, server_default="web"
        ),
        sa.Column("status", _enum("ticket_status"), nullable=False, server_default="open"),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("sla_due_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL)",
            name="ck_tickets_closed_at_matches_status",
        ),
    )
    op.create_index("ix_tickets_organization_id_user_id", "tickets", ["organization_id", "user_id"])
    op.create_index("ix_tickets_property_id", "tickets", ["property_id"])
    op.create_index("ix_tickets_contract_id", "tickets", ["contract_id"])
    op.create_index("ix_tickets_organization_id_status", "tickets", ["organization_id", "status"])
    op.create_index(
        "ix_tickets_organization_id_agent_id", "tickets", ["organization_id", "agent_id"]
    )
    op.create_index(
        "ix_tickets_organization_id_created_at_id",
        "tickets",
        ["organization_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index("ix_tickets_category_id", "tickets", ["category_id"])
    op.create_index("ix_tickets_urgency_id", "tickets", ["urgency_id"])

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("author_type", _enum("author_type"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "is_ai_suggestion", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("btrim(content) <> ''", name="ck_messages_content_not_blank"),
    )
    op.create_index(
        "ix_messages_ticket_id_created_at_id", "messages", ["ticket_id", "created_at", "id"]
    )

    op.create_table(
        "classifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("predicted_category", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "human_corrected", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence BETWEEN 0 AND 1)",
            name="ck_classifications_confidence_range",
        ),
    )
    op.create_index(
        "ix_classifications_ticket_id_unique", "classifications", ["ticket_id"], unique=True
    )


def downgrade() -> None:
    op.drop_table("classifications")
    op.drop_table("messages")
    op.drop_table("tickets")
    op.drop_table("urgency_levels")
    op.drop_table("categories")
    op.drop_table("contracts")
    op.drop_table("properties")
    op.drop_table("invite_tokens")
    op.drop_table("refresh_tokens")
    op.drop_table("users")
    op.drop_table("organizations")

    for type_name in _ENUM_TYPES:
        op.execute(f"DROP TYPE {type_name}")

    op.execute("DROP EXTENSION IF EXISTS vector")

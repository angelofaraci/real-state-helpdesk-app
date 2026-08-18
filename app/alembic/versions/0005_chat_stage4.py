"""stage 4 chatbot foundation: chat_sessions, chat_messages,
organizations.chat_widget_key

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-18

"""
from __future__ import annotations

import secrets
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CHAT_SESSION_STATUS_VALUES = ["active", "escalated", "closed"]
_CHAT_MESSAGE_ROLE_VALUES = ["user", "assistant", "tool"]


def _chat_session_status_enum() -> postgresql.ENUM:
    return postgresql.ENUM(
        *_CHAT_SESSION_STATUS_VALUES, name="chat_session_status", create_type=False
    )


def _chat_message_role_enum() -> postgresql.ENUM:
    return postgresql.ENUM(
        *_CHAT_MESSAGE_ROLE_VALUES, name="chat_message_role", create_type=False
    )


def upgrade() -> None:
    labels_sql = ", ".join(f"'{label}'" for label in _CHAT_SESSION_STATUS_VALUES)
    op.execute(f"CREATE TYPE chat_session_status AS ENUM ({labels_sql})")
    labels_sql = ", ".join(f"'{label}'" for label in _CHAT_MESSAGE_ROLE_VALUES)
    op.execute(f"CREATE TYPE chat_message_role AS ENUM ({labels_sql})")

    op.create_table(
        "chat_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "status", _chat_session_status_enum(), nullable=False, server_default="active"
        ),
        sa.Column(
            "low_confidence_streak", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "ticket_id IS NULL OR user_id IS NOT NULL",
            name="ck_chat_sessions_ticket_requires_user",
        ),
    )
    op.create_index("ix_chat_sessions_organization_id", "chat_sessions", ["organization_id"])
    op.create_index(
        "ix_chat_sessions_organization_id_user_id",
        "chat_sessions",
        ["organization_id", "user_id"],
    )
    op.create_index("ix_chat_sessions_ticket_id", "chat_sessions", ["ticket_id"])

    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "chat_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", _chat_message_role_enum(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=True),
        sa.Column("tool_call_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "(role = 'tool') = (tool_name IS NOT NULL)",
            name="ck_chat_messages_tool_name_iff_role_tool",
        ),
        sa.CheckConstraint(
            "role = 'assistant' OR btrim(content) <> ''",
            name="ck_chat_messages_content_not_blank_unless_assistant",
        ),
    )
    op.create_index(
        "ix_chat_messages_chat_session_id_created_at_id",
        "chat_messages",
        ["chat_session_id", "created_at", "id"],
    )

    op.add_column(
        "organizations", sa.Column("chat_widget_key", sa.String(), nullable=True)
    )

    # Backfill each existing organization with a unique generated key.
    # Deliberately a Python loop (one UPDATE per row) rather than a single
    # SQL expression, so the exact same `secrets.token_urlsafe` generator
    # used at organization-creation time (see
    # `app.services.organization_service.create_organization`) is also used
    # here, keeping key generation logic in one place conceptually.
    #
    # `op.get_bind()` requires a live connection, so this step is skipped in
    # Alembic's offline `--sql` rendering mode (used by this repo's
    # offline-SQL migration tests to statically verify migration shape) —
    # there is nothing meaningful to render for a data-dependent, per-row
    # loop anyway. It always runs for a real `alembic upgrade`.
    if not context.is_offline_mode():
        bind = op.get_bind()
        organizations = sa.table(
            "organizations",
            sa.column("id", postgresql.UUID(as_uuid=True)),
            sa.column("chat_widget_key", sa.String()),
        )
        rows = bind.execute(sa.select(organizations.c.id)).fetchall()
        for (org_id,) in rows:
            bind.execute(
                organizations.update()
                .where(organizations.c.id == org_id)
                .values(chat_widget_key=secrets.token_urlsafe(32))
            )

    op.alter_column("organizations", "chat_widget_key", nullable=False)
    op.create_index(
        "ix_organizations_chat_widget_key_unique",
        "organizations",
        ["chat_widget_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organizations_chat_widget_key_unique", table_name="organizations"
    )
    op.drop_column("organizations", "chat_widget_key")

    op.drop_index(
        "ix_chat_messages_chat_session_id_created_at_id", table_name="chat_messages"
    )
    op.drop_table("chat_messages")

    op.drop_index("ix_chat_sessions_ticket_id", table_name="chat_sessions")
    op.drop_index(
        "ix_chat_sessions_organization_id_user_id", table_name="chat_sessions"
    )
    op.drop_index("ix_chat_sessions_organization_id", table_name="chat_sessions")
    op.drop_table("chat_sessions")

    op.execute("DROP TYPE chat_message_role")
    op.execute("DROP TYPE chat_session_status")

"""stage 6 queues + SLA: sla_events + notifications tables, deferred from
0008 until their SQLAlchemy models existed (see `app/models/sla_event.py`,
`app/models/notification.py`).

Both `event` (`sla_events`) and `kind` (`notifications`) reference the
`sla_event_type`/`notification_kind` enum types created by 0008 via
`postgresql.ENUM(..., create_type=False)` — same idiom as 0007's
`_ticket_channel_enum()` helper for a type created ahead of this migration.

`sla_events` carries a raw-SQL partial unique index,
`ux_sla_events_ticket_event_once`, on `(ticket_id, event) WHERE event IN
('warning', 'breached')` — the sole DB-level idempotency guard a later PR's
SLA-monitor cron relies on to avoid emitting the same warning/breach event
twice for one ticket. `resolved` is deliberately excluded: it may
legitimately repeat across a ticket's reopen/re-resolve cycles.

`notifications` carries a raw-SQL ordered composite index,
`ix_notifications_user_id_sent_at_id`, on `(user_id, sent_at DESC, id
DESC)` — same idiom as 0007/0008's raw-SQL indexes for shapes Alembic's
`op.create_index` cannot express directly (a `DESC` ordering per column).
It backs the paginated `GET /notifications` a later PR will build.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-21

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _sla_event_type_enum() -> postgresql.ENUM:
    return postgresql.ENUM(
        "warning", "breached", "resolved", name="sla_event_type", create_type=False
    )


def _notification_kind_enum() -> postgresql.ENUM:
    return postgresql.ENUM(
        "sla_warning", "sla_breached", name="notification_kind", create_type=False
    )


def upgrade() -> None:
    # --- sla_events -------------------------------------------------------
    op.create_table(
        "sla_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event", _sla_event_type_enum(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Audit snapshot of the ticket's `sla_due_at` at the moment this
        # event was recorded — nullable (e.g. a future event kind might not
        # carry one).
        sa.Column("sla_due_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sla_events_ticket_id_occurred_at",
        "sla_events",
        ["ticket_id", "occurred_at"],
    )
    op.create_index(
        "ix_sla_events_organization_id_event",
        "sla_events",
        ["organization_id", "event"],
    )
    # Partial unique index — cannot be expressed as a plain `op.create_index`
    # column list plus a `postgresql_where` kwarg here without losing the
    # exact literal predicate this migration test pins byte-for-byte; same
    # raw-SQL convention as 0007's functional/GIN indexes.
    op.execute(
        "CREATE UNIQUE INDEX ux_sla_events_ticket_event_once ON sla_events "
        "(ticket_id, event) WHERE event IN ('warning', 'breached')"
    )

    # --- notifications ------------------------------------------------------
    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sla_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", _notification_kind_enum(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "sent_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("read_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["sla_event_id"], ["sla_events.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "btrim(title) <> ''", name="ck_notifications_title_not_blank"
        ),
    )
    op.create_index(
        "ix_notifications_organization_id", "notifications", ["organization_id"]
    )
    op.create_index("ix_notifications_ticket_id", "notifications", ["ticket_id"])
    # Ordered composite index (DESC per column) — backs the paginated
    # `GET /notifications` a later PR builds; raw SQL for the same reason as
    # `ux_sla_events_ticket_event_once` above.
    op.execute(
        "CREATE INDEX ix_notifications_user_id_sent_at_id ON notifications "
        "(user_id, sent_at DESC, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_notifications_user_id_sent_at_id")
    op.drop_index("ix_notifications_ticket_id", table_name="notifications")
    op.drop_index("ix_notifications_organization_id", table_name="notifications")
    op.drop_table("notifications")

    op.execute("DROP INDEX ux_sla_events_ticket_event_once")
    op.drop_index("ix_sla_events_organization_id_event", table_name="sla_events")
    op.drop_index("ix_sla_events_ticket_id_occurred_at", table_name="sla_events")
    op.drop_table("sla_events")

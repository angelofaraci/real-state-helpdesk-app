"""stage 7 analytics: daily_metrics table + chat_sessions.escalated_at.

`daily_metrics` is an organization-scoped, upserted fact table — one row
per `(organization_id, metric_date, metric_key)`, recomputed nightly by a
later PR's analytics-rollup cron via `INSERT ... ON CONFLICT
(organization_id, metric_date, metric_key) DO UPDATE`. Its unique
constraint `ux_daily_metrics_org_date_key` is the DB-level idempotency
guard that upsert relies on. `metric_key` is a plain `String` + CHECK
constraint rather than a Postgres enum — a deliberate divergence from this
codebase's `ticket_status`/`sla_event_type` enum convention, documented on
`app.models.daily_metric.DailyMetric` (adding a new metric later must not
require an `ALTER TYPE ... ADD VALUE`, which cannot run inside a
transaction — see 0006's autocommit-block workaround for that exact
friction). `ix_daily_metrics_org_key_date` backs the read path: "give me
this org's time series for one metric key".

`chat_sessions.escalated_at` records when a session was handed off to a
human agent (nullable — most sessions never escalate); backs the
`chat_escalation_rate` metric. `ix_chat_sessions_organization_id_created_at`
backs the `chat_sessions_started` daily rollup query (count sessions for an
org within a date range, bucketed by `created_at`).

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-22

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- daily_metrics ------------------------------------------------------
    op.create_table(
        "daily_metrics",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("metric_key", sa.String(), nullable=False),
        sa.Column("metric_value", sa.Numeric(14, 4), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "metric_date",
            "metric_key",
            name="ux_daily_metrics_org_date_key",
        ),
        sa.CheckConstraint(
            "btrim(metric_key) <> ''", name="ck_daily_metrics_metric_key_not_blank"
        ),
        sa.CheckConstraint(
            "metric_value >= 0", name="ck_daily_metrics_metric_value_non_negative"
        ),
    )
    op.create_index(
        "ix_daily_metrics_org_key_date",
        "daily_metrics",
        ["organization_id", "metric_key", "metric_date"],
    )

    # --- chat_sessions: escalated_at ----------------------------------------
    op.add_column(
        "chat_sessions",
        sa.Column("escalated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_chat_sessions_organization_id_created_at",
        "chat_sessions",
        ["organization_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chat_sessions_organization_id_created_at", table_name="chat_sessions"
    )
    op.drop_column("chat_sessions", "escalated_at")

    op.drop_index("ix_daily_metrics_org_key_date", table_name="daily_metrics")
    op.drop_table("daily_metrics")

"""DailyMetric model — one org-scoped fact row per `(organization_id,
metric_date, metric_key)`, upserted nightly by a later PR's
analytics-rollup cron via `INSERT ... ON CONFLICT
(organization_id, metric_date, metric_key) DO UPDATE`.

`metric_key` is a plain `String` column guarded by the
`ck_daily_metrics_metric_key_not_blank` CHECK constraint, NOT a Postgres
enum — a deliberate divergence from this codebase's
`ticket_status`/`sla_event_type` enum convention (see
`app/models/sla_event.py`). A Postgres enum requires `ALTER TYPE ... ADD
VALUE` to add a new label, which cannot run inside a transaction (see
migration 0006's autocommit-block workaround), making every future metric
addition a migration-authoring hazard for a vocabulary expected to grow
often. `METRIC_KEYS` is the current known vocabulary, validated at the
application layer instead of the database layer.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Index, Numeric, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

METRIC_KEYS: frozenset[str] = frozenset(
    {
        "tickets_created",
        "tickets_resolved",
        "tickets_open",
        "avg_resolution_hours",
        "sla_compliance_rate",
        "classifier_accuracy",
        "llm_fallback_rate",
        "rag_suggestion_usage_rate",
        "chat_sessions_started",
        "chat_to_ticket_rate",
        "chat_escalation_rate",
    }
)


class DailyMetric(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One org-scoped `(metric_date, metric_key)` fact row. `metric_key` is
    a plain `String` + CHECK constraint, not a Postgres enum — see module
    docstring for the rationale."""

    __tablename__ = "daily_metrics"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    metric_key: Mapped[str] = mapped_column(String, nullable=False)
    metric_value: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    # `TimestampMixin` only provides `created_at` — `updated_at` is declared
    # here directly, bumped on every upsert by the analytics-rollup cron's
    # `ON CONFLICT ... DO UPDATE`.
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "metric_date",
            "metric_key",
            name="ux_daily_metrics_org_date_key",
        ),
        Index(
            "ix_daily_metrics_org_key_date",
            "organization_id",
            "metric_key",
            "metric_date",
        ),
    )

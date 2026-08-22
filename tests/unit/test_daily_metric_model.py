"""Unit tests for the `DailyMetric` ORM model (stage 7 — analytics).

Like `test_sla_event_model.py`, these inspect `Base.metadata` directly (pure
Python object graph) — no database connection required. Migration 0010 is
the actual DDL source of truth; these tests only prove the ORM-level
declaration documents the same shape.
"""

from app.models import Base
from app.models.daily_metric import METRIC_KEYS, DailyMetric


def test_daily_metrics_table_registered() -> None:
    assert "daily_metrics" in Base.metadata.tables


def test_daily_metrics_id_is_the_primary_key() -> None:
    daily_metrics = Base.metadata.tables["daily_metrics"]
    pk_columns = list(daily_metrics.primary_key.columns)
    assert len(pk_columns) == 1
    assert pk_columns[0].name == "id"


def test_daily_metrics_organization_id_is_not_null_restrict_fk() -> None:
    daily_metrics = Base.metadata.tables["daily_metrics"]

    assert daily_metrics.columns["organization_id"].nullable is False
    org_fk_targets = {
        fk.target_fullname
        for fk in daily_metrics.columns["organization_id"].foreign_keys
    }
    assert org_fk_targets == {"organizations.id"}
    org_fk = next(iter(daily_metrics.columns["organization_id"].foreign_keys))
    assert org_fk.ondelete == "RESTRICT"


def test_daily_metrics_metric_date_and_metric_key_not_null() -> None:
    daily_metrics = Base.metadata.tables["daily_metrics"]
    assert daily_metrics.columns["metric_date"].nullable is False
    assert daily_metrics.columns["metric_key"].nullable is False


def test_daily_metrics_metric_key_is_plain_string_not_enum() -> None:
    """Deliberate divergence from this codebase's `ticket_status`/
    `sla_event_type` enum convention — `metric_key` is a plain `String` +
    CHECK constraint, documented on `DailyMetric`'s docstring."""
    daily_metrics = Base.metadata.tables["daily_metrics"]
    column = daily_metrics.columns["metric_key"]

    assert column.type.__class__.__name__ == "String"
    assert not hasattr(column.type, "enums")


def test_daily_metrics_metric_value_not_null() -> None:
    daily_metrics = Base.metadata.tables["daily_metrics"]
    assert daily_metrics.columns["metric_value"].nullable is False


def test_daily_metrics_created_at_and_updated_at_have_server_defaults() -> None:
    daily_metrics = Base.metadata.tables["daily_metrics"]
    assert daily_metrics.columns["created_at"].nullable is False
    assert daily_metrics.columns["created_at"].server_default is not None
    assert daily_metrics.columns["updated_at"].nullable is False
    assert daily_metrics.columns["updated_at"].server_default is not None


def test_daily_metrics_unique_constraint_declared() -> None:
    daily_metrics = Base.metadata.tables["daily_metrics"]
    unique_constraint_columns = [
        {c.name for c in uc.columns}
        for uc in daily_metrics.constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    ]
    assert {"organization_id", "metric_date", "metric_key"} in unique_constraint_columns


def test_daily_metrics_ordinary_index_declared() -> None:
    daily_metrics = Base.metadata.tables["daily_metrics"]
    index_names_and_columns = [
        ({c.name for c in ix.columns}, ix.name) for ix in daily_metrics.indexes
    ]
    assert (
        {"organization_id", "metric_key", "metric_date"},
        "ix_daily_metrics_org_key_date",
    ) in index_names_and_columns


def test_metric_keys_vocabulary_contains_exact_expected_set() -> None:
    assert METRIC_KEYS == frozenset(
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


def test_daily_metric_model_docstring_documents_string_over_enum_choice() -> None:
    assert DailyMetric.__doc__ is not None
    assert "enum" in DailyMetric.__doc__.lower()

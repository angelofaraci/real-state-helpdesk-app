"""Tests verifying migration 0010 (stage 7 — analytics: daily_metrics table
+ chat_sessions.escalated_at). Same offline-SQL-mode strategy as
`test_migration_0009.py`: render SQL via `alembic ... --sql`, no live
Postgres connection — this project's migration tests have no DB fixture.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_alembic(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args, "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture(scope="module")
def upgrade_section() -> str:
    """The SQL slice belonging only to 0009 -> 0010, bounded at the next
    `-- Running upgrade` marker (or EOF) so a future migration's own output
    never leaks in — mirrors `test_migration_0009.py::upgrade_section`."""
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr
    sql = result.stdout
    marker = "-- Running upgrade 0009 -> 0010"
    assert marker in sql, f"expected marker {marker!r} in rendered SQL"
    start = sql.index(marker)
    next_marker = sql.find("-- Running upgrade", start + len(marker))
    return sql[start : next_marker if next_marker != -1 else len(sql)]


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    result = _run_alembic("downgrade", "0010:0009")
    assert result.returncode == 0, result.stderr
    return result.stdout


# ---------------------------------------------------------------------------
# upgrade(): daily_metrics
# ---------------------------------------------------------------------------


def test_upgrade_creates_daily_metrics_table_with_expected_columns(
    upgrade_section: str,
) -> None:
    assert "CREATE TABLE daily_metrics" in upgrade_section
    assert "id UUID NOT NULL" in upgrade_section
    assert "organization_id UUID NOT NULL" in upgrade_section
    assert "metric_date DATE NOT NULL" in upgrade_section
    assert "metric_key VARCHAR NOT NULL" in upgrade_section
    assert "metric_value NUMERIC(14, 4) NOT NULL" in upgrade_section
    assert "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in upgrade_section
    assert "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in upgrade_section


def test_upgrade_daily_metrics_has_restrict_foreign_key(upgrade_section: str) -> None:
    assert (
        "FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE RESTRICT"
        in upgrade_section
    )


def test_upgrade_daily_metrics_unique_constraint(upgrade_section: str) -> None:
    assert "ux_daily_metrics_org_date_key" in upgrade_section
    assert "UNIQUE (organization_id, metric_date, metric_key)" in upgrade_section


def test_upgrade_daily_metrics_ordinary_index(upgrade_section: str) -> None:
    assert (
        "CREATE INDEX ix_daily_metrics_org_key_date ON daily_metrics "
        "(organization_id, metric_key, metric_date)" in upgrade_section
    )


def test_upgrade_daily_metrics_metric_key_not_blank_check(upgrade_section: str) -> None:
    assert "ck_daily_metrics_metric_key_not_blank" in upgrade_section
    assert "btrim(metric_key) <> ''" in upgrade_section


def test_upgrade_daily_metrics_metric_value_non_negative_check(
    upgrade_section: str,
) -> None:
    assert "ck_daily_metrics_metric_value_non_negative" in upgrade_section
    assert "metric_value >= 0" in upgrade_section


# ---------------------------------------------------------------------------
# upgrade(): chat_sessions.escalated_at
# ---------------------------------------------------------------------------


def test_upgrade_adds_chat_sessions_escalated_at_column(upgrade_section: str) -> None:
    assert "ALTER TABLE chat_sessions ADD COLUMN escalated_at TIMESTAMP WITH TIME ZONE" in (
        upgrade_section
    )
    # Nullable — no "NOT NULL" glued right after it.
    assert (
        "ALTER TABLE chat_sessions ADD COLUMN escalated_at TIMESTAMP WITH TIME ZONE NOT NULL"
        not in upgrade_section
    )


def test_upgrade_adds_chat_sessions_organization_id_created_at_index(
    upgrade_section: str,
) -> None:
    assert (
        "CREATE INDEX ix_chat_sessions_organization_id_created_at ON chat_sessions "
        "(organization_id, created_at)" in upgrade_section
    )


# ---------------------------------------------------------------------------
# downgrade(): exact inverse
# ---------------------------------------------------------------------------


def test_downgrade_drops_everything_added(downgrade_sql: str) -> None:
    assert (
        "DROP INDEX ix_chat_sessions_organization_id_created_at" in downgrade_sql
    )
    assert "ALTER TABLE chat_sessions DROP COLUMN escalated_at" in downgrade_sql
    assert "DROP TABLE daily_metrics" in downgrade_sql


def test_downgrade_drops_chat_sessions_changes_before_daily_metrics(
    downgrade_sql: str,
) -> None:
    escalated_at_drop = downgrade_sql.index(
        "ALTER TABLE chat_sessions DROP COLUMN escalated_at"
    )
    daily_metrics_drop = downgrade_sql.index("DROP TABLE daily_metrics")
    assert escalated_at_drop < daily_metrics_drop

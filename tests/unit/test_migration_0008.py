"""Tests verifying migration 0008 (stage 6 — queues + SLA foundation).
Same offline-SQL-mode strategy as `test_migration_0005.py` through
`test_migration_0007.py`: render SQL via `alembic ... --sql`, no live
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
    """The SQL slice belonging only to 0007 -> 0008, bounded at the next
    `-- Running upgrade` marker (or EOF) so a future migration's own
    output never leaks in — mirrors the fix applied to
    `test_migration_0007.py::_migration_0007_section`."""
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr
    sql = result.stdout
    marker = "-- Running upgrade 0007 -> 0008"
    assert marker in sql, f"expected marker {marker!r} in rendered SQL"
    start = sql.index(marker)
    next_marker = sql.find("-- Running upgrade", start + len(marker))
    return sql[start : next_marker if next_marker != -1 else len(sql)]


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    result = _run_alembic("downgrade", "0008:0007")
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_migration_0008_upgrade_creates_only_the_two_enum_types(upgrade_section: str) -> None:
    assert "CREATE TYPE sla_event_type AS ENUM ('warning', 'breached', 'resolved')" in (
        upgrade_section
    )
    assert "CREATE TYPE notification_kind AS ENUM ('sla_warning', 'sla_breached')" in (
        upgrade_section
    )
    # sla_events/notifications tables belong to a later PR, once their
    # SQLAlchemy models exist.
    assert "CREATE TABLE sla_events" not in upgrade_section
    assert "CREATE TABLE notifications" not in upgrade_section


def test_migration_0008_upgrade_adds_organizations_business_hours_columns(
    upgrade_section: str,
) -> None:
    assert (
        "ALTER TABLE organizations ADD COLUMN timezone VARCHAR "
        "DEFAULT 'America/Argentina/Buenos_Aires' NOT NULL" in upgrade_section
    )
    assert "ALTER TABLE organizations ADD COLUMN business_hours JSONB" in upgrade_section
    assert (
        '\'{"mon":[["09:00","18:00"]],"tue":[["09:00","18:00"]],"wed":[["09:00","18:00"]],'
        '"thu":[["09:00","18:00"]],"fri":[["09:00","18:00"]]}\'::jsonb' in upgrade_section
    )
    assert "ck_organizations_timezone_not_blank" in upgrade_section
    assert "btrim(timezone) <> ''" in upgrade_section


def test_migration_0008_upgrade_adds_and_backfills_respects_business_hours(
    upgrade_section: str,
) -> None:
    assert (
        "ALTER TABLE urgency_levels ADD COLUMN respects_business_hours BOOLEAN "
        "DEFAULT true NOT NULL" in upgrade_section
    )
    assert (
        "UPDATE urgency_levels SET respects_business_hours = false "
        "WHERE lower(btrim(name)) IN ('critical', 'high')" in upgrade_section
    )


def test_migration_0008_downgrade_drops_everything_in_dependency_order(
    downgrade_sql: str,
) -> None:
    assert "ALTER TABLE urgency_levels DROP COLUMN respects_business_hours" in downgrade_sql
    assert (
        "ALTER TABLE organizations DROP CONSTRAINT ck_organizations_timezone_not_blank"
        in downgrade_sql
    )
    assert "ALTER TABLE organizations DROP COLUMN business_hours" in downgrade_sql
    assert "ALTER TABLE organizations DROP COLUMN timezone" in downgrade_sql
    assert "DROP TYPE notification_kind" in downgrade_sql
    assert "DROP TYPE sla_event_type" in downgrade_sql

    # Enum types can only be dropped once nothing references them.
    timezone_drop = downgrade_sql.index("ALTER TABLE organizations DROP COLUMN timezone")
    respects_drop = downgrade_sql.index(
        "ALTER TABLE urgency_levels DROP COLUMN respects_business_hours"
    )
    sla_event_type_drop = downgrade_sql.index("DROP TYPE sla_event_type")
    notification_kind_drop = downgrade_sql.index("DROP TYPE notification_kind")

    assert timezone_drop < sla_event_type_drop
    assert timezone_drop < notification_kind_drop
    assert respects_drop < sla_event_type_drop
    assert respects_drop < notification_kind_drop

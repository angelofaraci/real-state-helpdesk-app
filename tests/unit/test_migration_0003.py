"""Tests verifying migration 0003 (classification stage 2 foundation).

Same offline-SQL-mode strategy as `test_alembic_migration.py`: render SQL
via `alembic upgrade head --sql` / `alembic downgrade ... --sql` without a
live Postgres connection, and assert on the exact rendered SQL strings.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_alembic_offline_sql() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_migration_0003_adds_title_backfills_and_sets_not_null() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "ALTER TABLE tickets ADD COLUMN title VARCHAR" in sql
    assert "UPDATE tickets SET title = 'Untitled ticket' WHERE title IS NULL" in sql
    assert "ALTER TABLE tickets ALTER COLUMN title SET NOT NULL" in sql

    # Backfill and NOT NULL must happen in order.
    add_idx = sql.index("ALTER TABLE tickets ADD COLUMN title VARCHAR")
    backfill_idx = sql.index(
        "UPDATE tickets SET title = 'Untitled ticket' WHERE title IS NULL"
    )
    not_null_idx = sql.index("ALTER TABLE tickets ALTER COLUMN title SET NOT NULL")
    assert add_idx < backfill_idx < not_null_idx


def test_migration_0003_adds_description_column_nullable() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "ALTER TABLE tickets ADD COLUMN description TEXT" in sql
    assert "ALTER TABLE tickets ADD COLUMN description TEXT NOT NULL" not in sql


def test_migration_0003_adds_title_not_blank_check_constraint() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert (
        "ALTER TABLE tickets ADD CONSTRAINT ck_tickets_title_not_blank "
        "CHECK (btrim(title) <> '')" in sql
    )


def test_migration_0003_makes_category_urgency_sla_columns_nullable() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "ALTER TABLE tickets ALTER COLUMN category_id DROP NOT NULL" in sql
    assert "ALTER TABLE tickets ALTER COLUMN urgency_id DROP NOT NULL" in sql
    assert "ALTER TABLE tickets ALTER COLUMN sla_due_at DROP NOT NULL" in sql


def test_migration_0003_adds_classified_has_taxonomy_check_constraint() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert (
        "ALTER TABLE tickets ADD CONSTRAINT ck_tickets_classified_has_taxonomy "
        "CHECK (classification_status <> 'classified' OR "
        "(category_id IS NOT NULL AND urgency_id IS NOT NULL AND "
        "sla_due_at IS NOT NULL))" in sql
    )


def test_migration_0003_adds_urgency_levels_description_column_nullable() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "ALTER TABLE urgency_levels ADD COLUMN description VARCHAR" in sql
    assert (
        "ALTER TABLE urgency_levels ADD COLUMN description VARCHAR NOT NULL" not in sql
    )


def test_migration_0003_adds_classifications_predicted_urgency_and_confidence() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "ALTER TABLE classifications ADD COLUMN predicted_urgency VARCHAR" in sql
    assert (
        "ALTER TABLE classifications ADD COLUMN urgency_confidence DOUBLE PRECISION"
        in sql
    )


def test_migration_0003_adds_urgency_confidence_range_check_constraint() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert (
        "ALTER TABLE classifications ADD CONSTRAINT "
        "ck_classifications_urgency_confidence_range CHECK (urgency_confidence "
        "IS NULL OR (urgency_confidence BETWEEN 0 AND 1))" in sql
    )


def test_migration_0003_downgrade_reverses_in_exact_inverse_order() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0003:0002", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"alembic downgrade 0003:0002 --sql failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    sql = result.stdout

    # Constraints/columns added last in upgrade must be dropped first in
    # downgrade (exact inverse order), mirroring migration 0002's tested
    # ordering discipline.
    urgency_confidence_constraint_drop = sql.index(
        "ALTER TABLE classifications DROP CONSTRAINT "
        "ck_classifications_urgency_confidence_range"
    )
    urgency_confidence_col_drop = sql.index(
        "ALTER TABLE classifications DROP COLUMN urgency_confidence"
    )
    predicted_urgency_col_drop = sql.index(
        "ALTER TABLE classifications DROP COLUMN predicted_urgency"
    )
    urgency_levels_desc_drop = sql.index(
        "ALTER TABLE urgency_levels DROP COLUMN description"
    )
    classified_taxonomy_constraint_drop = sql.index(
        "ALTER TABLE tickets DROP CONSTRAINT ck_tickets_classified_has_taxonomy"
    )
    sla_due_at_set_not_null = sql.index(
        "ALTER TABLE tickets ALTER COLUMN sla_due_at SET NOT NULL"
    )
    urgency_id_set_not_null = sql.index(
        "ALTER TABLE tickets ALTER COLUMN urgency_id SET NOT NULL"
    )
    category_id_set_not_null = sql.index(
        "ALTER TABLE tickets ALTER COLUMN category_id SET NOT NULL"
    )
    title_not_blank_constraint_drop = sql.index(
        "ALTER TABLE tickets DROP CONSTRAINT ck_tickets_title_not_blank"
    )
    description_col_drop = sql.index("ALTER TABLE tickets DROP COLUMN description")
    title_drop_not_null = sql.index(
        "ALTER TABLE tickets ALTER COLUMN title DROP NOT NULL"
    )
    title_col_drop = sql.index("ALTER TABLE tickets DROP COLUMN title")

    assert (
        urgency_confidence_constraint_drop
        < urgency_confidence_col_drop
        < predicted_urgency_col_drop
        < urgency_levels_desc_drop
        < classified_taxonomy_constraint_drop
        < sla_due_at_set_not_null
        < urgency_id_set_not_null
        < category_id_set_not_null
        < title_not_blank_constraint_drop
        < description_col_drop
        < title_drop_not_null
        < title_col_drop
    )

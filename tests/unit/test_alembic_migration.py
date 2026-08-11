"""Tests verifying the Alembic wiring and initial migration are correct.

These run Alembic in **offline SQL mode** (`alembic upgrade head --sql`),
which renders the migration's SQL to stdout without opening a real database
connection. This lets us statically verify the migration's shape (extension,
enum types, tables, constraints, indexes) without a live Postgres instance.
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


def test_alembic_offline_sql_generation_succeeds() -> None:
    result = _run_alembic_offline_sql()

    assert result.returncode == 0, (
        f"alembic upgrade head --sql failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )


def test_migration_creates_pgvector_extension_and_all_expected_tables() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql

    expected_tables = [
        "organizations",
        "users",
        "refresh_tokens",
        "invite_tokens",
        "properties",
        "contracts",
        "categories",
        "urgency_levels",
        "tickets",
        "messages",
        "classifications",
    ]
    for table_name in expected_tables:
        assert f"CREATE TABLE {table_name}" in sql, f"missing CREATE TABLE for {table_name}"


def test_migration_downgrade_drops_all_tables_and_enum_types() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0001:base", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"alembic downgrade 0001:base --sql failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    sql = result.stdout

    assert "DROP TABLE organizations" in sql
    assert "DROP TABLE tickets" in sql
    assert "DROP TYPE user_role" in sql
    assert "DROP EXTENSION IF EXISTS vector" in sql


def test_migration_creates_all_expected_native_enum_types() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    expected_enum_types = [
        "user_role",
        "user_status",
        "property_type",
        "contract_status",
        "ticket_channel",
        "ticket_status",
        "author_type",
    ]
    for enum_name in expected_enum_types:
        assert f"CREATE TYPE {enum_name}" in sql, f"missing CREATE TYPE for {enum_name}"


def test_migration_0002_creates_classification_status_enum_type() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "CREATE TYPE classification_status AS ENUM ('pending', 'classified')" in sql


def test_migration_0002_adds_tickets_classification_status_column_not_null_default_pending() -> (
    None
):
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert (
        "ALTER TABLE tickets ADD COLUMN classification_status classification_status "
        "DEFAULT 'pending' NOT NULL" in sql
    )


def test_migration_0002_backfills_existing_tickets_to_classified() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "UPDATE tickets SET classification_status = 'classified'" in sql


def test_migration_0002_creates_organization_id_classification_status_index() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert (
        "CREATE INDEX ix_tickets_organization_id_classification_status "
        "ON tickets (organization_id, classification_status)" in sql
    )


def test_migration_0002_adds_classifications_model_used_column() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "ALTER TABLE classifications ADD COLUMN model_used VARCHAR" in sql


def test_migration_0002_adds_model_used_check_constraint() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "ck_classifications_model_used_known" in sql
    assert "sklearn_v1" in sql
    assert "llm_fallback" in sql


def test_migration_0002_downgrade_reverses_in_order_and_drops_enum_last() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0002:0001", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"alembic downgrade 0002:0001 --sql failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    sql = result.stdout

    assert "ck_classifications_model_used_known" in sql
    assert "ALTER TABLE classifications DROP COLUMN model_used" in sql
    assert "DROP INDEX ix_tickets_organization_id_classification_status" in sql
    assert "ALTER TABLE tickets DROP COLUMN classification_status" in sql
    assert "DROP TYPE classification_status" in sql

    # Ordering: constraint/column drops on classifications and the index
    # drop must all happen before the enum type is dropped.
    constraint_drop_idx = sql.index("ck_classifications_model_used_known")
    model_used_drop_idx = sql.index("ALTER TABLE classifications DROP COLUMN model_used")
    index_drop_idx = sql.index("DROP INDEX ix_tickets_organization_id_classification_status")
    column_drop_idx = sql.index("ALTER TABLE tickets DROP COLUMN classification_status")
    enum_drop_idx = sql.index("DROP TYPE classification_status")

    assert constraint_drop_idx < model_used_drop_idx < enum_drop_idx
    assert index_drop_idx < column_drop_idx < enum_drop_idx


def test_migration_0002_downgrade_all_the_way_still_drops_all_0001_enum_types() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0002:base", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"alembic downgrade 0002:base --sql failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    sql = result.stdout

    assert "DROP TYPE classification_status" in sql
    assert "DROP TYPE user_role" in sql
    assert "DROP TABLE tickets" in sql

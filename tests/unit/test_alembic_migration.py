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

"""Tests verifying migration 0006 (add 'escalated' value to the existing
ticket_status enum, for stage-4 chatbot escalation).

Same offline-SQL-mode strategy as the other migration test files.
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


def test_migration_0006_offline_sql_generation_succeeds() -> None:
    result = _run_alembic_offline_sql()

    assert result.returncode == 0, (
        f"alembic upgrade head --sql failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )


def test_migration_0006_adds_escalated_value_to_ticket_status_enum() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "ALTER TYPE ticket_status ADD VALUE IF NOT EXISTS 'escalated'" in sql


def test_migration_0006_downgrade_is_a_documented_no_op() -> None:
    """Postgres cannot drop an enum value, so downgrading past 0006 must
    succeed without error and without attempting to touch ticket_status."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0006:0005", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"alembic downgrade 0006:0005 --sql failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    sql = result.stdout

    assert "ticket_status" not in sql

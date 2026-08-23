"""Tests verifying migration 0011 (invite token revoke-not-delete). Same
offline-SQL-mode strategy as `test_migration_0009.py`/`test_migration_0010_analytics.py`:
render SQL via `alembic ... --sql`, no live Postgres connection — this
project's migration tests have no DB fixture.
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
    """The SQL slice belonging only to 0010 -> 0011, bounded at the next
    `-- Running upgrade` marker (or EOF) so a future migration's own output
    never leaks in — mirrors `test_migration_0009.py::upgrade_section`."""
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr
    sql = result.stdout
    marker = "-- Running upgrade 0010 -> 0011"
    assert marker in sql, f"expected marker {marker!r} in rendered SQL"
    start = sql.index(marker)
    next_marker = sql.find("-- Running upgrade", start + len(marker))
    return sql[start : next_marker if next_marker != -1 else len(sql)]


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    result = _run_alembic("downgrade", "0011:0010")
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_upgrade_adds_nullable_revoked_at_column(upgrade_section: str) -> None:
    assert (
        "ALTER TABLE invite_tokens ADD COLUMN revoked_at TIMESTAMP WITH TIME ZONE"
        in upgrade_section
    )
    assert (
        "ALTER TABLE invite_tokens ADD COLUMN revoked_at TIMESTAMP WITH TIME ZONE NOT NULL"
        not in upgrade_section
    )


def test_upgrade_drops_and_recreates_partial_unique_index_with_widened_predicate(
    upgrade_section: str,
) -> None:
    assert "DROP INDEX ix_invite_tokens_user_id_single_outstanding" in upgrade_section
    assert (
        "CREATE UNIQUE INDEX ix_invite_tokens_user_id_single_outstanding "
        "ON invite_tokens (user_id) WHERE used_at IS NULL AND revoked_at IS NULL"
        in upgrade_section
    )

    drop_idx = upgrade_section.index("DROP INDEX ix_invite_tokens_user_id_single_outstanding")
    create_idx = upgrade_section.index(
        "CREATE UNIQUE INDEX ix_invite_tokens_user_id_single_outstanding"
    )
    add_column_idx = upgrade_section.index("ADD COLUMN revoked_at")
    assert add_column_idx < drop_idx < create_idx


def test_upgrade_does_not_use_concurrently(upgrade_section: str) -> None:
    # Small table — plain blocking DROP+CREATE INDEX inside Alembic's
    # standard transaction is preferred over CONCURRENTLY for atomicity.
    assert "CONCURRENTLY" not in upgrade_section


def test_downgrade_restores_original_index_predicate_and_drops_column(
    downgrade_sql: str,
) -> None:
    assert "DROP INDEX ix_invite_tokens_user_id_single_outstanding" in downgrade_sql
    assert (
        "CREATE UNIQUE INDEX ix_invite_tokens_user_id_single_outstanding "
        "ON invite_tokens (user_id) WHERE used_at IS NULL" in downgrade_sql
    )
    assert "ALTER TABLE invite_tokens DROP COLUMN revoked_at" in downgrade_sql

    # Widened predicate must not appear anywhere in the downgrade output.
    assert "used_at IS NULL AND revoked_at IS NULL" not in downgrade_sql

    drop_idx = downgrade_sql.index("DROP INDEX ix_invite_tokens_user_id_single_outstanding")
    create_idx = downgrade_sql.index(
        "CREATE UNIQUE INDEX ix_invite_tokens_user_id_single_outstanding"
    )
    drop_column_idx = downgrade_sql.index("ALTER TABLE invite_tokens DROP COLUMN revoked_at")
    assert drop_idx < create_idx < drop_column_idx

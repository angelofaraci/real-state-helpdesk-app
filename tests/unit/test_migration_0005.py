"""Tests verifying migration 0005 (stage-4 chatbot foundation: chat_sessions,
chat_messages, organizations.chat_widget_key).

Same offline-SQL-mode strategy as `test_migration_0004.py`: render SQL via
`alembic upgrade head --sql` / `alembic downgrade ... --sql` without a live
Postgres connection, and assert on the exact rendered SQL strings. The
per-row `chat_widget_key` backfill loop is data-dependent and requires a
live connection (`op.get_bind()`), so it is deliberately skipped in offline
mode (see the migration's `upgrade()`) and is not exercised here — it needs
a live Postgres instance, unavailable in this sandbox.
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


def test_migration_0005_offline_sql_generation_succeeds() -> None:
    result = _run_alembic_offline_sql()

    assert result.returncode == 0, (
        f"alembic upgrade head --sql failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )


def test_migration_0005_creates_chat_session_and_chat_message_role_enum_types() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "CREATE TYPE chat_session_status AS ENUM ('active', 'escalated', 'closed')" in sql
    assert "CREATE TYPE chat_message_role AS ENUM ('user', 'assistant', 'tool')" in sql


def test_migration_0005_creates_chat_sessions_table_with_check_constraint() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "CREATE TABLE chat_sessions" in sql
    assert "ck_chat_sessions_ticket_requires_user" in sql
    assert "ticket_id IS NULL OR user_id IS NOT NULL" in sql


def test_migration_0005_creates_chat_messages_table_with_check_constraints() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "CREATE TABLE chat_messages" in sql
    assert "ck_chat_messages_tool_name_iff_role_tool" in sql
    assert "ck_chat_messages_content_not_blank_unless_assistant" in sql
    assert (
        "CREATE INDEX ix_chat_messages_chat_session_id_created_at_id ON chat_messages "
        "(chat_session_id, created_at, id)" in sql
    )


def test_migration_0005_adds_chat_widget_key_nullable_then_not_null_then_unique_index() -> None:
    """The exact ordering that makes the backfill-before-NOT-NULL sequence
    safe: ADD COLUMN (nullable) must precede SET NOT NULL, which must
    precede the unique index creation."""
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "ALTER TABLE organizations ADD COLUMN chat_widget_key VARCHAR" in sql
    assert "ALTER TABLE organizations ALTER COLUMN chat_widget_key SET NOT NULL" in sql
    assert (
        "CREATE UNIQUE INDEX ix_organizations_chat_widget_key_unique "
        "ON organizations (chat_widget_key)" in sql
    )

    add_column_idx = sql.index("ALTER TABLE organizations ADD COLUMN chat_widget_key")
    not_null_idx = sql.index(
        "ALTER TABLE organizations ALTER COLUMN chat_widget_key SET NOT NULL"
    )
    unique_index_idx = sql.index("CREATE UNIQUE INDEX ix_organizations_chat_widget_key_unique")

    assert add_column_idx < not_null_idx < unique_index_idx


def test_migration_0005_downgrade_drops_everything_in_dependency_order() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0005:0004", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"alembic downgrade 0005:0004 --sql failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    sql = result.stdout

    assert "DROP INDEX ix_organizations_chat_widget_key_unique" in sql
    assert "ALTER TABLE organizations DROP COLUMN chat_widget_key" in sql
    assert "DROP TABLE chat_messages" in sql
    assert "DROP TABLE chat_sessions" in sql
    assert "DROP TYPE chat_message_role" in sql
    assert "DROP TYPE chat_session_status" in sql

    messages_drop_idx = sql.index("DROP TABLE chat_messages")
    sessions_drop_idx = sql.index("DROP TABLE chat_sessions")
    # chat_messages (child, FK -> chat_sessions) must be dropped before
    # chat_sessions (parent).
    assert messages_drop_idx < sessions_drop_idx

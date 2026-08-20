"""Tests verifying migration 0007 (stage 5 — multichannel foundation:
organizations WhatsApp/email config, users.phone_number +
auto_provisioned_channel, tickets.channel_metadata,
chat_sessions.channel_metadata + channel).

Same offline-SQL-mode strategy as `test_migration_0005.py`/
`test_migration_0006.py`: render SQL via `alembic upgrade head --sql` /
`alembic downgrade ... --sql` without a live Postgres connection, and
assert on the exact rendered SQL strings.
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


def test_migration_0007_offline_sql_generation_succeeds() -> None:
    result = _run_alembic_offline_sql()

    assert result.returncode == 0, (
        f"alembic upgrade head --sql failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )


def _migration_0007_section(sql: str) -> str:
    """The slice of rendered SQL belonging only to 0006 -> 0007 (earlier
    migrations legitimately CREATE TYPE for their own new enums)."""
    marker = "-- Running upgrade 0006 -> 0007"
    assert marker in sql, f"expected marker {marker!r} in rendered SQL"
    return sql[sql.index(marker) :]


def test_migration_0007_adds_no_new_postgres_enum_types() -> None:
    """Fully additive/nullable migration: `ticket_channel` already has
    web/email/whatsapp values (see `app/models/enums.py`), so THIS
    migration's own section must never CREATE TYPE or ALTER TYPE (earlier
    migrations legitimately do, for their own new enums)."""
    result = _run_alembic_offline_sql()
    section = _migration_0007_section(result.stdout)

    assert "CREATE TYPE" not in section
    assert "ALTER TYPE" not in section


def test_migration_0007_adds_organizations_whatsapp_and_email_columns() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "ALTER TABLE organizations ADD COLUMN whatsapp_phone_number_id VARCHAR" in sql
    assert (
        "ALTER TABLE organizations ADD COLUMN whatsapp_access_token_encrypted TEXT" in sql
    )
    assert "ALTER TABLE organizations ADD COLUMN support_email_address VARCHAR" in sql


def test_migration_0007_adds_organizations_whatsapp_config_complete_check() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "ck_organizations_whatsapp_config_complete" in sql
    assert (
        "(whatsapp_phone_number_id IS NULL) = (whatsapp_access_token_encrypted IS NULL)"
        in sql
    )


def test_migration_0007_adds_organizations_unique_indexes() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert (
        "CREATE UNIQUE INDEX ix_organizations_whatsapp_phone_number_id_unique "
        "ON organizations (whatsapp_phone_number_id)" in sql
    )
    assert (
        "CREATE UNIQUE INDEX ix_organizations_support_email_address_lower_unique "
        "ON organizations (lower(support_email_address))" in sql
    )


def test_migration_0007_adds_users_phone_number_and_auto_provisioned_channel() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "ALTER TABLE users ADD COLUMN phone_number VARCHAR" in sql
    assert "ALTER TABLE users ADD COLUMN auto_provisioned_channel ticket_channel" in sql


def test_migration_0007_adds_users_phone_number_e164_check() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "ck_users_phone_number_e164" in sql
    assert "phone_number ~ '^\\+[1-9][0-9]{6,14}$'" in sql


def test_migration_0007_adds_users_phone_number_unique_index() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert (
        "CREATE UNIQUE INDEX ix_users_phone_number_unique ON users (phone_number)" in sql
    )


def test_migration_0007_adds_tickets_channel_metadata_jsonb_column() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "ALTER TABLE tickets ADD COLUMN channel_metadata JSONB" in sql


def test_migration_0007_adds_tickets_channel_metadata_gin_index() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert (
        "CREATE INDEX ix_tickets_channel_metadata_message_ids ON tickets "
        "USING gin ((channel_metadata -> 'message_ids') jsonb_path_ops)" in sql
    )


def test_migration_0007_adds_chat_sessions_channel_metadata_and_channel_columns() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "ALTER TABLE chat_sessions ADD COLUMN channel_metadata JSONB" in sql
    assert (
        "ALTER TABLE chat_sessions ADD COLUMN channel ticket_channel "
        "DEFAULT 'web' NOT NULL" in sql
    )


def test_migration_0007_adds_chat_sessions_org_wa_id_expression_index() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert (
        "CREATE INDEX ix_chat_sessions_org_wa_id ON chat_sessions "
        "(organization_id, (channel_metadata ->> 'wa_id'))" in sql
    )


def test_migration_0007_downgrade_drops_everything_added() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0007:0006", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"alembic downgrade 0007:0006 --sql failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    sql = result.stdout

    assert "DROP INDEX ix_chat_sessions_org_wa_id" in sql
    assert "ALTER TABLE chat_sessions DROP COLUMN channel" in sql
    assert "ALTER TABLE chat_sessions DROP COLUMN channel_metadata" in sql
    assert "DROP INDEX ix_tickets_channel_metadata_message_ids" in sql
    assert "ALTER TABLE tickets DROP COLUMN channel_metadata" in sql
    assert "DROP INDEX ix_users_phone_number_unique" in sql
    assert "ALTER TABLE users DROP COLUMN auto_provisioned_channel" in sql
    assert "ALTER TABLE users DROP COLUMN phone_number" in sql
    assert "DROP INDEX ix_organizations_support_email_address_lower_unique" in sql
    assert "DROP INDEX ix_organizations_whatsapp_phone_number_id_unique" in sql
    assert "ALTER TABLE organizations DROP COLUMN support_email_address" in sql
    assert "ALTER TABLE organizations DROP COLUMN whatsapp_access_token_encrypted" in sql
    assert "ALTER TABLE organizations DROP COLUMN whatsapp_phone_number_id" in sql

    # No DROP TYPE of ticket_channel: this migration never created it.
    assert "DROP TYPE" not in sql


def test_migration_0007_downgrade_drops_indexes_before_their_columns() -> None:
    """An index on a column must be dropped before the column itself."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0007:0006", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    sql = result.stdout

    org_wa_id_index_idx = sql.index("DROP INDEX ix_chat_sessions_org_wa_id")
    channel_column_drop_idx = sql.index("ALTER TABLE chat_sessions DROP COLUMN channel")
    assert org_wa_id_index_idx < channel_column_drop_idx

    phone_index_idx = sql.index("DROP INDEX ix_users_phone_number_unique")
    phone_column_drop_idx = sql.index("ALTER TABLE users DROP COLUMN phone_number")
    assert phone_index_idx < phone_column_drop_idx

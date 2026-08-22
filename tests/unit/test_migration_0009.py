"""Tests verifying migration 0009 (stage 6 — sla_events + notifications
tables, deferred from 0008 until their SQLAlchemy models existed). Same
offline-SQL-mode strategy as `test_migration_0007.py`/`test_migration_0008.py`:
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
    """The SQL slice belonging only to 0008 -> 0009, bounded at the next
    `-- Running upgrade` marker (or EOF) so a future migration's own output
    never leaks in — mirrors `test_migration_0008.py::upgrade_section`."""
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr
    sql = result.stdout
    marker = "-- Running upgrade 0008 -> 0009"
    assert marker in sql, f"expected marker {marker!r} in rendered SQL"
    start = sql.index(marker)
    next_marker = sql.find("-- Running upgrade", start + len(marker))
    return sql[start : next_marker if next_marker != -1 else len(sql)]


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    result = _run_alembic("downgrade", "0009:0008")
    assert result.returncode == 0, result.stderr
    return result.stdout


# ---------------------------------------------------------------------------
# upgrade(): sla_events
# ---------------------------------------------------------------------------


def test_upgrade_creates_sla_events_table_with_expected_columns(
    upgrade_section: str,
) -> None:
    assert "CREATE TABLE sla_events" in upgrade_section
    assert "id UUID NOT NULL" in upgrade_section
    assert "organization_id UUID NOT NULL" in upgrade_section
    assert "ticket_id UUID NOT NULL" in upgrade_section
    assert "event sla_event_type NOT NULL" in upgrade_section
    assert "occurred_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in upgrade_section
    assert "sla_due_at TIMESTAMP WITH TIME ZONE" in upgrade_section
    # sla_due_at must stay nullable — no "NOT NULL" glued right after it.
    assert "sla_due_at TIMESTAMP WITH TIME ZONE NOT NULL" not in upgrade_section
    # sla_event_type is consumed via create_type=False — 0009 must not try
    # to (re)create either enum type that 0008 already created.
    assert "CREATE TYPE sla_event_type" not in upgrade_section
    assert "CREATE TYPE notification_kind" not in upgrade_section


def test_upgrade_sla_events_has_restrict_foreign_keys(upgrade_section: str) -> None:
    assert (
        "FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE RESTRICT"
        in upgrade_section
    )
    assert (
        "FOREIGN KEY(ticket_id) REFERENCES tickets (id) ON DELETE RESTRICT"
        in upgrade_section
    )


def test_upgrade_sla_events_ordinary_indexes(upgrade_section: str) -> None:
    assert (
        "CREATE INDEX ix_sla_events_ticket_id_occurred_at ON sla_events "
        "(ticket_id, occurred_at)" in upgrade_section
    )
    assert (
        "CREATE INDEX ix_sla_events_organization_id_event ON sla_events "
        "(organization_id, event)" in upgrade_section
    )


def test_upgrade_sla_events_partial_unique_index_excludes_resolved(
    upgrade_section: str,
) -> None:
    assert (
        "CREATE UNIQUE INDEX ux_sla_events_ticket_event_once ON sla_events "
        "(ticket_id, event) WHERE event IN ('warning', 'breached')" in upgrade_section
    )
    # `resolved` is deliberately excluded — it may repeat across a ticket's
    # reopen/re-resolve cycles.
    assert "'resolved'" not in upgrade_section.split(
        "ux_sla_events_ticket_event_once"
    )[1].split(";")[0]


# ---------------------------------------------------------------------------
# upgrade(): notifications
# ---------------------------------------------------------------------------


def test_upgrade_creates_notifications_table_with_expected_columns(
    upgrade_section: str,
) -> None:
    assert "CREATE TABLE notifications" in upgrade_section
    assert "organization_id UUID NOT NULL" in upgrade_section
    assert "user_id UUID NOT NULL" in upgrade_section
    assert "ticket_id UUID" in upgrade_section
    assert "sla_event_id UUID" in upgrade_section
    assert "kind notification_kind NOT NULL" in upgrade_section
    assert "title VARCHAR NOT NULL" in upgrade_section
    assert "body TEXT" in upgrade_section
    assert "sent_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in upgrade_section
    assert "read_at TIMESTAMP WITH TIME ZONE" in upgrade_section
    assert "read_at TIMESTAMP WITH TIME ZONE NOT NULL" not in upgrade_section


def test_upgrade_notifications_has_restrict_foreign_keys(upgrade_section: str) -> None:
    notifications_section = upgrade_section[upgrade_section.index("CREATE TABLE notifications") :]
    assert (
        "FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE RESTRICT"
        in notifications_section
    )
    assert (
        "FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT"
        in notifications_section
    )
    assert (
        "FOREIGN KEY(ticket_id) REFERENCES tickets (id) ON DELETE RESTRICT"
        in notifications_section
    )
    assert (
        "FOREIGN KEY(sla_event_id) REFERENCES sla_events (id) ON DELETE RESTRICT"
        in notifications_section
    )


def test_upgrade_notifications_title_not_blank_check_constraint(
    upgrade_section: str,
) -> None:
    assert "ck_notifications_title_not_blank" in upgrade_section
    assert "btrim(title) <> ''" in upgrade_section


def test_upgrade_notifications_ordinary_indexes(upgrade_section: str) -> None:
    assert (
        "CREATE INDEX ix_notifications_organization_id ON notifications (organization_id)"
        in upgrade_section
    )
    assert (
        "CREATE INDEX ix_notifications_ticket_id ON notifications (ticket_id)"
        in upgrade_section
    )


def test_upgrade_notifications_ordered_composite_index(upgrade_section: str) -> None:
    assert (
        "CREATE INDEX ix_notifications_user_id_sent_at_id ON notifications "
        "(user_id, sent_at DESC, id DESC)" in upgrade_section
    )


def test_upgrade_notifications_created_after_sla_events(upgrade_section: str) -> None:
    # notifications.sla_event_id references sla_events.id — creation order
    # matters for a forward (non---sql) run.
    sla_events_pos = upgrade_section.index("CREATE TABLE sla_events")
    notifications_pos = upgrade_section.index("CREATE TABLE notifications")
    assert sla_events_pos < notifications_pos


# ---------------------------------------------------------------------------
# downgrade(): exact inverse
# ---------------------------------------------------------------------------


def test_downgrade_drops_notifications_before_sla_events(downgrade_sql: str) -> None:
    assert "DROP INDEX ix_notifications_user_id_sent_at_id" in downgrade_sql
    assert "DROP TABLE notifications" in downgrade_sql
    assert "DROP INDEX ux_sla_events_ticket_event_once" in downgrade_sql
    assert "DROP TABLE sla_events" in downgrade_sql

    notifications_drop = downgrade_sql.index("DROP TABLE notifications")
    partial_index_drop = downgrade_sql.index("DROP INDEX ux_sla_events_ticket_event_once")
    sla_events_drop = downgrade_sql.index("DROP TABLE sla_events")

    # notifications (and its FK to sla_events) must be gone before the
    # partial unique index drop and before sla_events itself is dropped.
    assert notifications_drop < partial_index_drop
    assert partial_index_drop < sla_events_drop


def test_downgrade_does_not_touch_the_enum_types(downgrade_sql: str) -> None:
    # The enum types belong to migration 0008 — 0009 downgrade must not
    # attempt to drop them (0008's own downgrade already does).
    assert "DROP TYPE sla_event_type" not in downgrade_sql
    assert "DROP TYPE notification_kind" not in downgrade_sql

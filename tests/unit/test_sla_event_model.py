"""Unit tests for the `SlaEvent` ORM model (stage 6 — queues + SLA).

Like `test_ticket_model.py`/`test_models_metadata.py`, these inspect
`Base.metadata` directly (pure Python object graph) — no database
connection required. The actual live-Postgres enforcement of the
`ux_sla_events_ticket_event_once` partial unique index (proving a
duplicate `(ticket_id, event='warning')` insert really raises
`IntegrityError`) is covered by
`tests/integration/test_sla_event_constraints.py`, which — like
`tests/integration/test_channel_identity_constraints.py` — is skipped in
this sandbox (no live Postgres reachable here).
"""

from app.models import Base


def test_sla_events_table_registered() -> None:
    assert "sla_events" in Base.metadata.tables


def test_sla_events_id_is_the_primary_key() -> None:
    sla_events = Base.metadata.tables["sla_events"]
    pk_columns = list(sla_events.primary_key.columns)
    assert len(pk_columns) == 1
    assert pk_columns[0].name == "id"


def test_sla_events_organization_id_and_ticket_id_are_not_null_restrict_fks() -> None:
    sla_events = Base.metadata.tables["sla_events"]

    assert sla_events.columns["organization_id"].nullable is False
    org_fk_targets = {
        fk.target_fullname for fk in sla_events.columns["organization_id"].foreign_keys
    }
    assert org_fk_targets == {"organizations.id"}
    org_fk = next(iter(sla_events.columns["organization_id"].foreign_keys))
    assert org_fk.ondelete == "RESTRICT"

    assert sla_events.columns["ticket_id"].nullable is False
    ticket_fk_targets = {
        fk.target_fullname for fk in sla_events.columns["ticket_id"].foreign_keys
    }
    assert ticket_fk_targets == {"tickets.id"}
    ticket_fk = next(iter(sla_events.columns["ticket_id"].foreign_keys))
    assert ticket_fk.ondelete == "RESTRICT"


def test_sla_events_event_column_matches_sla_event_type_enum() -> None:
    sla_events = Base.metadata.tables["sla_events"]
    column = sla_events.columns["event"]

    assert column.nullable is False
    assert column.type.name == "sla_event_type"
    assert set(column.type.enums) == {"warning", "breached", "resolved"}


def test_sla_events_occurred_at_not_null_with_server_default() -> None:
    sla_events = Base.metadata.tables["sla_events"]
    column = sla_events.columns["occurred_at"]

    assert column.nullable is False
    assert column.server_default is not None


def test_sla_events_sla_due_at_is_nullable() -> None:
    sla_events = Base.metadata.tables["sla_events"]
    assert sla_events.columns["sla_due_at"].nullable is True


def test_sla_events_ordinary_indexes_present() -> None:
    sla_events = Base.metadata.tables["sla_events"]
    index_names_and_columns = [
        ({c.name for c in ix.columns}, ix.name) for ix in sla_events.indexes
    ]
    assert (
        {"ticket_id", "occurred_at"},
        "ix_sla_events_ticket_id_occurred_at",
    ) in index_names_and_columns
    assert (
        {"organization_id", "event"},
        "ix_sla_events_organization_id_event",
    ) in index_names_and_columns


def test_sla_events_partial_unique_index_declared_and_excludes_resolved() -> None:
    """The raw-SQL DDL (migration 0009) is the actual source of truth for
    this index; this only proves the ORM-level `Index()` documents the same
    shape, matching `Ticket`'s `ix_tickets_channel_metadata_message_ids`
    precedent."""
    sla_events = Base.metadata.tables["sla_events"]
    partial_unique = next(
        ix for ix in sla_events.indexes if ix.name == "ux_sla_events_ticket_event_once"
    )

    assert partial_unique.unique is True
    assert {c.name for c in partial_unique.columns} == {"ticket_id", "event"}
    where_sql = str(partial_unique.dialect_options["postgresql"]["where"])
    assert "warning" in where_sql
    assert "breached" in where_sql
    assert "resolved" not in where_sql

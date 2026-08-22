"""Unit tests verifying the SQLAlchemy ORM models map to the expected schema.

These tests never open a database connection: they inspect `Base.metadata`
(pure Python object graph) to confirm table names, columns, nullability,
foreign keys, and UUID primary key defaults match the approved design.
"""

import uuid

from app.models import Base


EXPECTED_TABLE_NAMES = {
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
    "knowledge_base",
    "knowledge_chunks",
    "ticket_embeddings",
    "chat_sessions",
    "chat_messages",
    "sla_events",
    "notifications",
    "daily_metrics",
}


def test_metadata_contains_exactly_the_nineteen_expected_tables() -> None:
    assert set(Base.metadata.tables.keys()) == EXPECTED_TABLE_NAMES


def test_all_primary_keys_are_uuid_with_python_side_uuid4_default() -> None:
    for table_name in EXPECTED_TABLE_NAMES:
        table = Base.metadata.tables[table_name]
        pk_columns = list(table.primary_key.columns)
        assert len(pk_columns) == 1, f"{table_name} must have exactly one PK column"
        pk = pk_columns[0]
        assert pk.name == "id"
        assert pk.default is not None, f"{table_name}.id must have an app-side default"
        assert pk.default.is_callable, f"{table_name}.id default must be a callable (app-side)"
        generated_value = pk.default.arg(None)
        assert isinstance(generated_value, uuid.UUID)
        assert generated_value != pk.default.arg(None), "each call must yield a fresh uuid4"


def test_users_table_columns_nullability_and_constraints() -> None:
    users = Base.metadata.tables["users"]

    assert users.columns["organization_id"].nullable is True
    assert users.columns["email"].nullable is False
    assert users.columns["role"].nullable is False
    assert users.columns["password_hash"].nullable is True
    assert users.columns["status"].nullable is False

    fk_targets = {fk.target_fullname for fk in users.columns["organization_id"].foreign_keys}
    assert fk_targets == {"organizations.id"}

    check_sqltexts = {
        str(c.sqltext) for c in users.constraints if c.__class__.__name__ == "CheckConstraint"
    }
    assert any("password_hash" in text for text in check_sqltexts)
    assert any("role" in text for text in check_sqltexts)


def test_contracts_table_has_no_organization_id_column() -> None:
    contracts = Base.metadata.tables["contracts"]
    assert "organization_id" not in contracts.columns


def test_tickets_table_foreign_keys_and_optional_columns() -> None:
    tickets = Base.metadata.tables["tickets"]

    assert tickets.columns["property_id"].nullable is True
    assert tickets.columns["contract_id"].nullable is True
    # Nullable as of migration 0003: a ticket may exist before
    # classification assigns taxonomy (see test_ticket_model.py).
    assert tickets.columns["category_id"].nullable is True
    assert tickets.columns["urgency_id"].nullable is True
    assert tickets.columns["sla_due_at"].nullable is True

    category_fk_targets = {
        fk.target_fullname for fk in tickets.columns["category_id"].foreign_keys
    }
    assert category_fk_targets == {"categories.id"}

    urgency_fk_targets = {
        fk.target_fullname for fk in tickets.columns["urgency_id"].foreign_keys
    }
    assert urgency_fk_targets == {"urgency_levels.id"}


def test_classifications_ticket_id_is_unique() -> None:
    classifications = Base.metadata.tables["classifications"]
    unique_indexes = [ix for ix in classifications.indexes if ix.unique]
    assert any(
        {c.name for c in ix.columns} == {"ticket_id"} for ix in unique_indexes
    ), "classifications must have a unique index on ticket_id"


def test_tickets_classification_status_column_matches_migration_0002() -> None:
    tickets = Base.metadata.tables["tickets"]
    column = tickets.columns["classification_status"]

    assert column.nullable is False
    assert column.type.enums == ["pending", "classified"]
    assert column.type.name == "classification_status"

    index_names_and_columns = [
        ({c.name for c in ix.columns}, ix.name) for ix in tickets.indexes
    ]
    assert (
        {"organization_id", "classification_status"},
        "ix_tickets_organization_id_classification_status",
    ) in index_names_and_columns


def test_classifications_model_used_column_matches_migration_0002() -> None:
    classifications = Base.metadata.tables["classifications"]
    column = classifications.columns["model_used"]

    assert column.nullable is True
    assert isinstance(column.type, type(classifications.columns["predicted_category"].type))

    check_sqltexts = {
        str(c.sqltext)
        for c in classifications.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    assert any("model_used" in text for text in check_sqltexts)
    assert any("sklearn_v1" in text for text in check_sqltexts)
    assert any("llm_fallback" in text for text in check_sqltexts)

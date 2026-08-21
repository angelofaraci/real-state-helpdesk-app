"""Unit tests for the `Notification` ORM model (stage 6 — queues + SLA).

Same pure-`Base.metadata`-inspection strategy as `test_sla_event_model.py`
— no database connection required. See that file's module docstring for
why no live-Postgres enforcement test lives here.
"""

from app.models import Base


def test_notifications_table_registered() -> None:
    assert "notifications" in Base.metadata.tables


def test_notifications_id_is_the_primary_key() -> None:
    notifications = Base.metadata.tables["notifications"]
    pk_columns = list(notifications.primary_key.columns)
    assert len(pk_columns) == 1
    assert pk_columns[0].name == "id"


def test_notifications_organization_id_not_null_restrict_fk() -> None:
    notifications = Base.metadata.tables["notifications"]
    column = notifications.columns["organization_id"]

    assert column.nullable is False
    assert {fk.target_fullname for fk in column.foreign_keys} == {"organizations.id"}
    assert next(iter(column.foreign_keys)).ondelete == "RESTRICT"


def test_notifications_user_id_is_the_recipient_not_null_restrict_fk() -> None:
    notifications = Base.metadata.tables["notifications"]
    column = notifications.columns["user_id"]

    assert column.nullable is False
    assert {fk.target_fullname for fk in column.foreign_keys} == {"users.id"}
    assert next(iter(column.foreign_keys)).ondelete == "RESTRICT"


def test_notifications_ticket_id_is_nullable_restrict_fk() -> None:
    notifications = Base.metadata.tables["notifications"]
    column = notifications.columns["ticket_id"]

    assert column.nullable is True
    assert {fk.target_fullname for fk in column.foreign_keys} == {"tickets.id"}
    assert next(iter(column.foreign_keys)).ondelete == "RESTRICT"


def test_notifications_sla_event_id_is_nullable_provenance_restrict_fk() -> None:
    notifications = Base.metadata.tables["notifications"]
    column = notifications.columns["sla_event_id"]

    assert column.nullable is True
    assert {fk.target_fullname for fk in column.foreign_keys} == {"sla_events.id"}
    assert next(iter(column.foreign_keys)).ondelete == "RESTRICT"


def test_notifications_kind_column_matches_notification_kind_enum() -> None:
    notifications = Base.metadata.tables["notifications"]
    column = notifications.columns["kind"]

    assert column.nullable is False
    assert column.type.name == "notification_kind"
    assert set(column.type.enums) == {"sla_warning", "sla_breached"}


def test_notifications_title_not_null_body_nullable() -> None:
    notifications = Base.metadata.tables["notifications"]

    assert notifications.columns["title"].nullable is False
    assert notifications.columns["body"].nullable is True


def test_notifications_sent_at_not_null_with_server_default_read_at_nullable() -> None:
    notifications = Base.metadata.tables["notifications"]

    assert notifications.columns["sent_at"].nullable is False
    assert notifications.columns["sent_at"].server_default is not None
    assert notifications.columns["read_at"].nullable is True


def test_notifications_title_not_blank_check_constraint_present() -> None:
    notifications = Base.metadata.tables["notifications"]
    check_sqltexts = {
        str(c.sqltext)
        for c in notifications.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    assert any("title" in text for text in check_sqltexts)
    assert any("btrim" in text for text in check_sqltexts)


def test_notifications_ordinary_indexes_present() -> None:
    notifications = Base.metadata.tables["notifications"]
    index_names_and_columns = [
        ({c.name for c in ix.columns}, ix.name) for ix in notifications.indexes
    ]
    assert ({"organization_id"}, "ix_notifications_organization_id") in (
        index_names_and_columns
    )
    assert ({"ticket_id"}, "ix_notifications_ticket_id") in index_names_and_columns

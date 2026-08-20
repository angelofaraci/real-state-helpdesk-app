"""Unit tests for the `ChatSession` ORM model (stage 4 — chatbot).

Pure `Base.metadata` inspection — no database connection required, matching
the convention established in `test_knowledge_base_model.py` /
`test_ticket_model.py`. CHECK constraint *enforcement* by real Postgres is
out of scope for this sandbox (no live Postgres reachable).
"""

from app.models import Base


def _check_sqltexts(table) -> set[str]:
    return {
        str(c.sqltext)
        for c in table.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }


def test_chat_sessions_table_registered_on_metadata() -> None:
    assert "chat_sessions" in Base.metadata.tables


def test_chat_sessions_columns_nullability() -> None:
    chat_sessions = Base.metadata.tables["chat_sessions"]

    assert chat_sessions.columns["organization_id"].nullable is False
    assert chat_sessions.columns["user_id"].nullable is True
    assert chat_sessions.columns["ticket_id"].nullable is True
    assert chat_sessions.columns["status"].nullable is False
    assert chat_sessions.columns["low_confidence_streak"].nullable is False
    assert chat_sessions.columns["last_activity_at"].nullable is False


def test_chat_sessions_status_default_is_active() -> None:
    chat_sessions = Base.metadata.tables["chat_sessions"]

    assert chat_sessions.columns["status"].server_default is not None
    assert "active" in str(chat_sessions.columns["status"].server_default.arg)


def test_chat_sessions_low_confidence_streak_default_is_zero() -> None:
    chat_sessions = Base.metadata.tables["chat_sessions"]

    assert chat_sessions.columns["low_confidence_streak"].server_default is not None
    assert "0" in str(chat_sessions.columns["low_confidence_streak"].server_default.arg)


def test_chat_sessions_organization_id_foreign_key_cascades() -> None:
    chat_sessions = Base.metadata.tables["chat_sessions"]

    fks = list(chat_sessions.columns["organization_id"].foreign_keys)
    assert {fk.target_fullname for fk in fks} == {"organizations.id"}
    assert all(fk.ondelete == "CASCADE" for fk in fks)


def test_chat_sessions_user_id_foreign_key_sets_null() -> None:
    chat_sessions = Base.metadata.tables["chat_sessions"]

    fks = list(chat_sessions.columns["user_id"].foreign_keys)
    assert {fk.target_fullname for fk in fks} == {"users.id"}
    assert all(fk.ondelete == "SET NULL" for fk in fks)


def test_chat_sessions_ticket_id_foreign_key_sets_null() -> None:
    chat_sessions = Base.metadata.tables["chat_sessions"]

    fks = list(chat_sessions.columns["ticket_id"].foreign_keys)
    assert {fk.target_fullname for fk in fks} == {"tickets.id"}
    assert all(fk.ondelete == "SET NULL" for fk in fks)


def test_chat_sessions_ticket_requires_user_check_constraint_present() -> None:
    """The behavioral rule under test: a `ChatSession` can only be linked to
    a `ticket_id` if it also has a (non-anonymous) `user_id` — an escalated
    ticket must be traceable to a real reporter, never the anonymous chat
    sentinel actor."""
    chat_sessions = Base.metadata.tables["chat_sessions"]

    check_sqltexts = _check_sqltexts(chat_sessions)
    assert any(
        "ticket_id" in text and "user_id" in text for text in check_sqltexts
    ), "expected a CHECK constraint tying ticket_id to a non-null user_id"

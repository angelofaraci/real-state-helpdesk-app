"""Unit tests for the `ChatMessage` ORM model (stage 4 — chatbot).

Pure `Base.metadata` inspection — no database connection required, matching
the convention established in `test_knowledge_base_model.py`.
"""

from app.models import Base


def _check_sqltexts(table) -> set[str]:
    return {
        str(c.sqltext)
        for c in table.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }


def test_chat_messages_table_registered_on_metadata() -> None:
    assert "chat_messages" in Base.metadata.tables


def test_chat_messages_columns_nullability() -> None:
    chat_messages = Base.metadata.tables["chat_messages"]

    assert chat_messages.columns["chat_session_id"].nullable is False
    assert chat_messages.columns["role"].nullable is False
    assert chat_messages.columns["content"].nullable is False
    assert chat_messages.columns["tool_name"].nullable is True
    assert chat_messages.columns["tool_call_id"].nullable is True


def test_chat_messages_chat_session_id_foreign_key_cascades() -> None:
    chat_messages = Base.metadata.tables["chat_messages"]

    fks = list(chat_messages.columns["chat_session_id"].foreign_keys)
    assert {fk.target_fullname for fk in fks} == {"chat_sessions.id"}
    assert all(fk.ondelete == "CASCADE" for fk in fks)


def test_chat_messages_tool_name_required_iff_role_tool_check_constraint_present() -> None:
    chat_messages = Base.metadata.tables["chat_messages"]

    check_sqltexts = _check_sqltexts(chat_messages)
    assert any(
        "tool_name" in text and "role" in text and "tool" in text
        for text in check_sqltexts
    ), "expected a CHECK constraint tying tool_name to role='tool'"


def test_chat_messages_content_not_blank_unless_assistant_check_constraint_present() -> None:
    """An assistant message may have blank content while a tool call is
    pending (tool-call-only turn); user/tool messages must always have
    non-blank content."""
    chat_messages = Base.metadata.tables["chat_messages"]

    check_sqltexts = _check_sqltexts(chat_messages)
    assert any(
        "content" in text and "assistant" in text and "btrim" in text
        for text in check_sqltexts
    ), "expected a CHECK constraint requiring non-blank content unless role='assistant'"

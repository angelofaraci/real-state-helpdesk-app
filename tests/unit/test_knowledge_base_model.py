"""Unit tests for the `KnowledgeBase` ORM model.

Pure `Base.metadata` inspection — no database connection required. CHECK
constraint *enforcement* by real Postgres is covered separately in
`tests/integration/test_rag_constraints.py`.
"""

from app.models import Base


def _check_sqltexts(table) -> set[str]:
    return {
        str(c.sqltext)
        for c in table.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }


def test_knowledge_base_table_registered_on_metadata() -> None:
    assert "knowledge_base" in Base.metadata.tables


def test_knowledge_base_columns_nullability() -> None:
    knowledge_base = Base.metadata.tables["knowledge_base"]

    assert knowledge_base.columns["organization_id"].nullable is False
    assert knowledge_base.columns["title"].nullable is False
    assert knowledge_base.columns["content"].nullable is False
    assert knowledge_base.columns["source_type"].nullable is True
    assert knowledge_base.columns["status"].nullable is False
    assert knowledge_base.columns["embedding_error"].nullable is True


def test_knowledge_base_status_default_is_pending() -> None:
    knowledge_base = Base.metadata.tables["knowledge_base"]

    assert knowledge_base.columns["status"].server_default is not None
    assert "pending" in str(knowledge_base.columns["status"].server_default.arg)


def test_knowledge_base_title_and_content_not_blank_check_constraints_present() -> None:
    knowledge_base = Base.metadata.tables["knowledge_base"]

    check_sqltexts = _check_sqltexts(knowledge_base)
    assert any("title" in text and "btrim" in text for text in check_sqltexts)
    assert any("content" in text and "btrim" in text for text in check_sqltexts)


def test_knowledge_base_status_enum_check_constraint_present() -> None:
    knowledge_base = Base.metadata.tables["knowledge_base"]

    check_sqltexts = _check_sqltexts(knowledge_base)
    status_checks = [text for text in check_sqltexts if "status" in text and "IN" in text]
    assert status_checks, "expected a status enum CHECK constraint"
    assert any(
        all(value in text for value in ("pending", "processing", "ready", "failed"))
        for text in status_checks
    )


def test_knowledge_base_failed_requires_embedding_error_check_constraint_present() -> None:
    knowledge_base = Base.metadata.tables["knowledge_base"]

    check_sqltexts = _check_sqltexts(knowledge_base)
    assert any(
        "failed" in text and "embedding_error" in text for text in check_sqltexts
    )


def test_knowledge_base_organization_foreign_key() -> None:
    knowledge_base = Base.metadata.tables["knowledge_base"]

    fk_targets = {
        fk.target_fullname for fk in knowledge_base.columns["organization_id"].foreign_keys
    }
    assert fk_targets == {"organizations.id"}

"""Unit tests for the `Ticket` ORM model's stage-2 classification shape.

Like `test_models_metadata.py`, these inspect `Base.metadata` directly (pure
Python object graph) — no database connection required.
"""

from app.models import Base


def test_tickets_title_is_not_null() -> None:
    tickets = Base.metadata.tables["tickets"]

    assert tickets.columns["title"].nullable is False


def test_tickets_description_is_nullable() -> None:
    tickets = Base.metadata.tables["tickets"]

    assert tickets.columns["description"].nullable is True


def test_tickets_category_urgency_and_sla_due_at_are_nullable() -> None:
    tickets = Base.metadata.tables["tickets"]

    assert tickets.columns["category_id"].nullable is True
    assert tickets.columns["urgency_id"].nullable is True
    assert tickets.columns["sla_due_at"].nullable is True


def test_tickets_title_not_blank_check_constraint_present() -> None:
    tickets = Base.metadata.tables["tickets"]

    check_sqltexts = {
        str(c.sqltext)
        for c in tickets.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    assert any("title" in text for text in check_sqltexts)


def test_tickets_classified_has_taxonomy_check_constraint_present() -> None:
    tickets = Base.metadata.tables["tickets"]

    check_sqltexts = {
        str(c.sqltext)
        for c in tickets.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    assert any(
        "classification_status" in text and "category_id" in text
        for text in check_sqltexts
    )

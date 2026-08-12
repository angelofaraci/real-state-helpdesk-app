"""Unit tests for the stage-2 taxonomy model additions:
`UrgencyLevel.description` and `Classification.predicted_urgency` /
`Classification.urgency_confidence`.

Same pure-metadata inspection strategy as `test_models_metadata.py` — no
database connection required.
"""

from app.models import Base


def test_urgency_levels_description_is_nullable() -> None:
    urgency_levels = Base.metadata.tables["urgency_levels"]

    assert urgency_levels.columns["description"].nullable is True


def test_classifications_predicted_urgency_is_nullable_string() -> None:
    classifications = Base.metadata.tables["classifications"]

    column = classifications.columns["predicted_urgency"]
    assert column.nullable is True
    assert isinstance(
        column.type, type(classifications.columns["predicted_category"].type)
    )


def test_classifications_urgency_confidence_is_nullable_with_range_check() -> None:
    classifications = Base.metadata.tables["classifications"]

    column = classifications.columns["urgency_confidence"]
    assert column.nullable is True

    check_sqltexts = {
        str(c.sqltext)
        for c in classifications.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    assert any("urgency_confidence" in text for text in check_sqltexts)
    assert any("BETWEEN 0 AND 1" in text for text in check_sqltexts)

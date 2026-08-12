"""Unit tests for `scripts.evaluate_classifier.macro_f1` — the pure
macro-F1 computation used to gate CI (`scripts/evaluate_classifier.py`).

`macro_f1` takes two parallel lists of label names (`y_true`, `y_pred`)
and returns a float in `[0, 1]`. These tests use tiny hand-built fixtures
only — no real trained model, no `data/tickets_test.csv` I/O, no sklearn
import required to exercise this function.
"""

import pytest

from scripts.evaluate_classifier import macro_f1


def test_perfect_predictions_score_one() -> None:
    y_true = ["Billing", "Maintenance", "Billing", "Maintenance"]
    y_pred = ["Billing", "Maintenance", "Billing", "Maintenance"]

    score = macro_f1(y_true, y_pred)

    assert score == 1.0


def test_completely_wrong_predictions_score_zero() -> None:
    y_true = ["Billing", "Maintenance"]
    y_pred = ["Maintenance", "Billing"]

    score = macro_f1(y_true, y_pred)

    assert score == 0.0


def test_partial_mismatch_returns_float_in_unit_range() -> None:
    y_true = ["Billing", "Billing", "Maintenance", "Maintenance"]
    y_pred = ["Billing", "Maintenance", "Maintenance", "Maintenance"]

    score = macro_f1(y_true, y_pred)

    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0
    # Billing: TP=1, FP=0, FN=1 -> P=1.0, R=0.5 -> F1=2/3
    # Maintenance: TP=2, FP=1, FN=0 -> P=2/3, R=1.0 -> F1=0.8
    # macro average of (2/3, 0.8)
    assert score == pytest.approx((2 / 3 + 0.8) / 2)


def test_mismatched_lengths_raise_value_error() -> None:
    with pytest.raises(ValueError):
        macro_f1(["Billing"], ["Billing", "Maintenance"])

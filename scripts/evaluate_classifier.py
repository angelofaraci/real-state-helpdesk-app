"""Evaluate the trained stage-2 sklearn classifiers against
`data/tickets_test.csv` and gate CI on macro-F1.

Loads the held-out test CSV, embeds each row's `title + " " +
description` the same way `scripts/train_classifier.py` does, runs both
trained classifiers (`ml_artifacts/category_clf.joblib` /
`urgency_clf.joblib`) via `app.services.classifier_model.LabelledSklearnClassifier.predict`,
computes macro-F1 for category and urgency predictions independently via
`macro_f1` (a pure, I/O-free function — see
`tests/unit/test_evaluate_classifier.py`), prints both scores, and exits
non-zero if either falls below `MACRO_F1_THRESHOLD`.

Run from the repo root:

    python scripts/evaluate_classifier.py
"""

from __future__ import annotations

import asyncio
import csv
import sys
from pathlib import Path

import joblib

from app.services.embeddings import get_embedding_provider

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_CSV = REPO_ROOT / "data" / "tickets_test.csv"
ARTIFACTS_DIR = REPO_ROOT / "ml_artifacts"
MACRO_F1_THRESHOLD = 0.75


def load_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def row_text(row: dict[str, str]) -> str:
    return f"{row['title']} {row['description']}".strip()


async def embed_rows(rows: list[dict[str, str]]) -> list[list[float]]:
    embedder = get_embedding_provider(embedding_provider="local")
    texts = [row_text(row) for row in rows]
    return await embedder.embed(texts)


def macro_f1(y_true: list[str], y_pred: list[str]) -> float:
    """Compute macro-averaged F1 over `y_true`/`y_pred` (parallel lists of
    label names), with NO sklearn/numpy/I-O dependency — pure Python so it
    is testable against a tiny hand-built fixture without training a real
    model or touching any CSV.

    Macro-F1 = the unweighted mean of the per-class F1 score, computed
    over every label seen in `y_true` (a class with zero true positives,
    zero false positives, and zero false negatives is undefined and is
    excluded rather than treated as F1=0).
    """
    if len(y_true) != len(y_pred):
        raise ValueError("y_true and y_pred must be the same length")

    labels = sorted(set(y_true) | set(y_pred))
    if not labels:
        return 0.0

    f1_scores: list[float] = []
    for label in labels:
        true_positives = sum(
            1 for t, p in zip(y_true, y_pred) if t == label and p == label
        )
        false_positives = sum(
            1 for t, p in zip(y_true, y_pred) if t != label and p == label
        )
        false_negatives = sum(
            1 for t, p in zip(y_true, y_pred) if t == label and p != label
        )

        if true_positives == 0 and false_positives == 0 and false_negatives == 0:
            continue

        precision = (
            true_positives / (true_positives + false_positives)
            if (true_positives + false_positives) > 0
            else 0.0
        )
        recall = (
            true_positives / (true_positives + false_negatives)
            if (true_positives + false_negatives) > 0
            else 0.0
        )
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0.0
        )
        f1_scores.append(f1)

    if not f1_scores:
        return 0.0
    return sum(f1_scores) / len(f1_scores)


def evaluate() -> tuple[float, float]:
    """Load the test CSV, run both trained classifiers, and return
    `(category_macro_f1, urgency_macro_f1)`."""
    rows = load_rows(TEST_CSV)
    if not rows:
        raise SystemExit(f"no test rows found in {TEST_CSV}")

    embeddings = asyncio.run(embed_rows(rows))

    category_clf = joblib.load(ARTIFACTS_DIR / "category_clf.joblib")
    urgency_clf = joblib.load(ARTIFACTS_DIR / "urgency_clf.joblib")

    category_predictions = [category_clf.predict(embedding)[0] for embedding in embeddings]
    urgency_predictions = [urgency_clf.predict(embedding)[0] for embedding in embeddings]

    category_true = [row["category"] for row in rows]
    urgency_true = [row["urgency"] for row in rows]

    category_score = macro_f1(category_true, category_predictions)
    urgency_score = macro_f1(urgency_true, urgency_predictions)
    return category_score, urgency_score


def main() -> int:
    category_score, urgency_score = evaluate()

    print(f"category macro-F1: {category_score:.4f}")
    print(f"urgency macro-F1: {urgency_score:.4f}")

    if category_score < MACRO_F1_THRESHOLD or urgency_score < MACRO_F1_THRESHOLD:
        print(
            f"FAIL: macro-F1 below threshold ({MACRO_F1_THRESHOLD}) — "
            f"category={category_score:.4f}, urgency={urgency_score:.4f}"
        )
        return 1

    print(f"PASS: both macro-F1 scores meet threshold ({MACRO_F1_THRESHOLD})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

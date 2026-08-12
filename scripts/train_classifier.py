"""Train the stage-2 sklearn classifiers from `data/tickets_train.csv`.

Loads the hand-authored training CSV (columns: `title`, `description`,
`category`, `urgency`), embeds `title + " " + description` for every row
via `app.services.embeddings.get_embedding_provider()` (defaults to the
local `sentence-transformers` provider — no network/API key needed for
training), fits two independent `LogisticRegression` classifiers (one for
`category`, one for `urgency`), wraps each in
`app.services.classifier_model.LabelledSklearnClassifier`, and persists
them via `joblib.dump` to the exact filenames `app.services.classifier._default_predict_category`
/ `_default_predict_urgency` load, resolved relative to
`settings.ml_artifacts_dir` (default `"./ml_artifacts"`, i.e. relative to
the repo root / container `WORKDIR` — NOT the `app/` package directory):

- `ml_artifacts/category_clf.joblib`
- `ml_artifacts/urgency_clf.joblib`

Run from the repo root:

    python scripts/train_classifier.py
"""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path

import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import LabelEncoder

from app.services.classifier_model import LabelledSklearnClassifier
from app.services.embeddings import get_embedding_provider

REPO_ROOT = Path(__file__).resolve().parent.parent
TRAIN_CSV = REPO_ROOT / "data" / "tickets_train.csv"
# Matches `settings.ml_artifacts_dir` default ("./ml_artifacts", relative
# to the repo root / container WORKDIR) — see `app/core/config.py` and
# `tests/unit/test_config.py::test_settings_expose_ml_artifacts_dir_with_local_default`.
ARTIFACTS_DIR = REPO_ROOT / "ml_artifacts"


def load_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def row_text(row: dict[str, str]) -> str:
    return f"{row['title']} {row['description']}".strip()


async def embed_rows(rows: list[dict[str, str]]) -> list[list[float]]:
    embedder = get_embedding_provider(embedding_provider="local")
    texts = [row_text(row) for row in rows]
    return await embedder.embed(texts)


def train_one(embeddings: list[list[float]], labels: list[str]) -> LabelledSklearnClassifier:
    label_encoder = LabelEncoder()
    encoded_labels = label_encoder.fit_transform(labels)

    model = LogisticRegression(max_iter=1000)
    model.fit(embeddings, encoded_labels)

    return LabelledSklearnClassifier(model=model, label_encoder=label_encoder)


def main() -> None:
    rows = load_rows(TRAIN_CSV)
    if not rows:
        raise SystemExit(f"no training rows found in {TRAIN_CSV}")

    embeddings = asyncio.run(embed_rows(rows))

    category_clf = train_one(embeddings, [row["category"] for row in rows])
    urgency_clf = train_one(embeddings, [row["urgency"] for row in rows])

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(category_clf, ARTIFACTS_DIR / "category_clf.joblib")
    joblib.dump(urgency_clf, ARTIFACTS_DIR / "urgency_clf.joblib")

    print(f"trained on {len(rows)} rows")
    print(f"saved {ARTIFACTS_DIR / 'category_clf.joblib'}")
    print(f"saved {ARTIFACTS_DIR / 'urgency_clf.joblib'}")


if __name__ == "__main__":
    main()

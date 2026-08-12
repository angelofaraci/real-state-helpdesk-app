"""Sklearn model wrapper persisted as the stage-2 classification joblib
artifacts (`category_clf.joblib` / `urgency_clf.joblib`).

`app.services.classifier._default_predict_category` /
`_default_predict_urgency` call `model.predict(embedding)` with a single
embedding vector (a flat `list[float]`, not a batch) and expect back a
`(predicted_label_name, confidence)` tuple. Plain sklearn estimators do not
expose that exact signature (they take a 2D batch and return either a
label array or a probability matrix), so this thin wrapper adapts a
trained `sklearn` classifier + its `LabelEncoder` to the shape
`classifier.py` requires.

This class is what actually gets pickled by `joblib.dump` in
`scripts/train_classifier.py`, so it must stay importable from this exact
module path (`app.services.classifier_model.LabelledSklearnClassifier`) —
moving or renaming it would break every previously-trained `.joblib`
artifact on disk.
"""

from __future__ import annotations


class LabelledSklearnClassifier:
    """Adapts a trained sklearn classifier + `LabelEncoder` to the
    `(embedding: list[float]) -> (label_name: str, confidence: float)`
    signature `app.services.classifier` expects from a loaded artifact.
    """

    def __init__(self, *, model, label_encoder) -> None:
        self.model = model
        self.label_encoder = label_encoder

    def predict(self, embedding: list[float]) -> tuple[str, float]:
        probabilities = list(self.model.predict_proba([embedding])[0])
        best_index = max(range(len(probabilities)), key=probabilities.__getitem__)
        label_name = self.label_encoder.inverse_transform([best_index])[0]
        confidence = float(probabilities[best_index])
        return str(label_name), confidence

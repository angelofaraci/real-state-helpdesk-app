"""Unit tests for `app.services.classifier_model.LabelledSklearnClassifier`
— the joblib-persisted wrapper that adapts a trained sklearn classifier +
`LabelEncoder` to the `(embedding) -> (label_name, confidence)` signature
`app.services.classifier` expects from a loaded artifact.

Uses a tiny fake sklearn-shaped model (`predict_proba`) and a fake
label encoder so this test never needs a real trained model or the
`scikit-learn` import.
"""

from app.services.classifier_model import LabelledSklearnClassifier


class _FakeModel:
    """Fake sklearn-shaped model: `predict_proba([embedding]) -> [[p0, p1]]`."""

    def predict_proba(self, batch):
        assert batch == [[0.1, 0.2, 0.3]]
        return [[0.1, 0.9]]


class _FakeLabelEncoder:
    """Fake `LabelEncoder`: `inverse_transform([index]) -> [label_name]`."""

    def inverse_transform(self, indices):
        mapping = {0: "Billing", 1: "Maintenance"}
        return [mapping[index] for index in indices]


def test_predict_returns_highest_probability_label_and_confidence() -> None:
    wrapper = LabelledSklearnClassifier(model=_FakeModel(), label_encoder=_FakeLabelEncoder())

    label_name, confidence = wrapper.predict([0.1, 0.2, 0.3])

    assert label_name == "Maintenance"
    assert confidence == 0.9

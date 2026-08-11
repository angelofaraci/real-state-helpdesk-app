"""Security regression test: the sklearn artifact loader used by
`app.services.classifier` must refuse to resolve a path outside
`settings.ml_artifacts_dir` *before* ever calling `joblib.load`.

Guards against a traversal-style artifact name (e.g. `../../etc/passwd`)
being combined with a configured `ml_artifacts_dir` to escape the
intended directory. Path containment must be checked with
`Path.resolve()` + parent containment, not naive string-prefix matching
(which is bypassable by e.g. a sibling directory sharing a prefix).
"""

from unittest.mock import patch

import pytest

from app.services.classifier import (
    ClassificationUnavailableError,
    _load_joblib_artifact,
)


def test_classifier_rejects_artifact_path_outside_ml_artifacts_dir() -> None:
    with patch("joblib.load") as fake_joblib_load:
        with pytest.raises(ClassificationUnavailableError):
            _load_joblib_artifact("../../etc/passwd")

    fake_joblib_load.assert_not_called()


def test_classifier_rejects_artifact_name_with_embedded_traversal() -> None:
    with patch("joblib.load") as fake_joblib_load:
        with pytest.raises(ClassificationUnavailableError):
            _load_joblib_artifact("subdir/../../../etc/passwd")

    fake_joblib_load.assert_not_called()

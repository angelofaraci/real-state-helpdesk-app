"""Unit tests for `app.core.config.Settings`, the stage-2 classification
additions: Redis, embedding provider, OpenAI, classifier threshold, and the
ML artifacts directory.

Settings are pure `pydantic-settings` defaults — no environment or database
access is required to assert them.
"""

from app.core.config import Settings


def test_settings_expose_redis_url_with_local_dev_default() -> None:
    settings = Settings()

    assert settings.redis_url == "redis://localhost:6379/0"


def test_settings_expose_embedding_provider_defaulting_to_local() -> None:
    settings = Settings()

    assert settings.embedding_provider == "local"


def test_settings_expose_openai_api_key_defaulting_to_none() -> None:
    settings = Settings()

    assert settings.openai_api_key is None


def test_settings_expose_openai_model_defaulting_to_gpt_4o_mini() -> None:
    settings = Settings()

    assert settings.openai_model == "gpt-4o-mini"


def test_settings_expose_classifier_confidence_threshold_defaulting_to_0_6() -> None:
    settings = Settings()

    assert settings.classifier_confidence_threshold == 0.6


def test_settings_expose_ml_artifacts_dir_with_local_default() -> None:
    settings = Settings()

    assert settings.ml_artifacts_dir == "./ml_artifacts"

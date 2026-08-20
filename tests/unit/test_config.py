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


# ---------------------------------------------------------------------------
# Stage 5 — multichannel: WhatsApp/email/crypto settings. Every secret
# defaults to `None` (fails closed — see `app.core.crypto`/
# `app.core.webhook_signature`) until explicitly configured.
# ---------------------------------------------------------------------------


def test_settings_expose_secret_encryption_key_defaulting_to_none() -> None:
    assert Settings().secret_encryption_key is None


def test_settings_expose_whatsapp_verify_token_defaulting_to_none() -> None:
    assert Settings().whatsapp_verify_token is None


def test_settings_expose_whatsapp_app_secret_defaulting_to_none() -> None:
    assert Settings().whatsapp_app_secret is None


def test_settings_expose_whatsapp_api_base_url_with_default() -> None:
    assert Settings().whatsapp_api_base_url == "https://graph.facebook.com"


def test_settings_expose_whatsapp_api_version_with_default() -> None:
    assert Settings().whatsapp_api_version == "v21.0"


def test_settings_expose_whatsapp_send_timeout_seconds_with_default() -> None:
    assert Settings().whatsapp_send_timeout_seconds == 10.0


def test_settings_expose_whatsapp_session_idle_minutes_with_default() -> None:
    assert Settings().whatsapp_session_idle_minutes == 1440


def test_settings_expose_whatsapp_customer_window_hours_with_default() -> None:
    assert Settings().whatsapp_customer_window_hours == 24


def test_settings_expose_email_webhook_provider_defaulting_to_mailgun() -> None:
    assert Settings().email_webhook_provider == "mailgun"


def test_settings_expose_mailgun_signing_key_defaulting_to_none() -> None:
    assert Settings().mailgun_signing_key is None


def test_settings_expose_email_webhook_max_age_seconds_with_default() -> None:
    assert Settings().email_webhook_max_age_seconds == 300


def test_settings_expose_email_thread_subject_match_days_with_default() -> None:
    assert Settings().email_thread_subject_match_days == 30


def test_settings_expose_email_message_id_domain_with_default() -> None:
    assert Settings().email_message_id_domain == "helpdesk.local"


def test_settings_expose_email_from_fallback_address_with_default() -> None:
    assert Settings().email_from_fallback_address == "no-reply@real-estate-helpdesk.local"

"""Application settings, loaded from environment variables / .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the application.

    Values are read from environment variables (or a local `.env` file
    during development). Sensible defaults are provided for local dev via
    docker-compose; production deployments must override them explicitly.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Real Estate Helpdesk API"
    environment: str = "development"

    database_url: str = (
        "postgresql+asyncpg://helpdesk:helpdesk@localhost:5432/helpdesk"
    )

    jwt_secret: str = "dev-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"

    smtp_host: str = "localhost"
    smtp_port: int = 1025

    frontend_url: str = "http://localhost:3000"

    # Consumed once by `python -m app.core.seed` to bootstrap the single
    # platform-level super-admin (role=admin, organization_id=NULL). There
    # is no API endpoint that creates a super-admin; this is the only way.
    super_admin_email: str | None = None
    super_admin_password: str | None = None
    super_admin_name: str = "Super Admin"

    # Stage 2 — ticket classification.
    redis_url: str = "redis://localhost:6379/0"
    embedding_provider: str = "local"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    classifier_confidence_threshold: float = 0.6
    ml_artifacts_dir: str = "./ml_artifacts"

    # Stage 3 — RAG (knowledge base + ticket similarity search).
    rag_embedding_provider: str = "local"
    rag_embedding_model: str = "all-mpnet-base-v2"
    rag_openai_embedding_model: str = "text-embedding-3-small"
    rag_similarity_threshold: float = 0.5
    rag_kb_top_k: int = 5
    rag_ticket_top_k: int = 3
    rag_search_overfetch: int = 4
    rag_chunk_max_tokens: int = 512
    rag_chunk_overlap_tokens: int = 64

    # Stage 4 — chatbot.
    chat_session_token_ttl_minutes: int = 60
    chat_rate_limit_per_hour: int = 30
    chat_history_turns: int = 10
    chat_max_tool_rounds: int = 2

    # Stage 5 — multichannel (WhatsApp + email). Every secret defaults to
    # `None` — encryption/signature verification fails closed
    # (`app.core.crypto`/`app.core.webhook_signature`) rather than
    # silently no-op'ing when unconfigured.
    secret_encryption_key: str | None = None
    whatsapp_verify_token: str | None = None
    whatsapp_app_secret: str | None = None
    whatsapp_api_base_url: str = "https://graph.facebook.com"
    whatsapp_api_version: str = "v21.0"
    whatsapp_send_timeout_seconds: float = 10.0
    whatsapp_session_idle_minutes: int = 1440
    whatsapp_customer_window_hours: int = 24
    email_webhook_provider: str = "mailgun"
    mailgun_signing_key: str | None = None
    email_webhook_max_age_seconds: int = 300
    email_thread_subject_match_days: int = 30
    email_message_id_domain: str = "helpdesk.local"
    email_from_fallback_address: str = "no-reply@real-estate-helpdesk.local"

    # Stage 8 — devops (PR7). Worker metrics HTTP server
    # (`prometheus_client.start_http_server`, started once per worker OS
    # process in `app.workers.settings.on_startup`). Deliberately NOT
    # 9100: that's the conventional default port for Prometheus's own
    # `node_exporter`, and `deploy/prometheus/prometheus.yml` scrapes both
    # the arq worker and (from PR9/10/11 onward) `node_exporter` — reusing
    # 9100 for the worker would collide with that convention, so the arq
    # worker's metrics endpoint uses 9200 instead.
    metrics_enabled: bool = True
    worker_metrics_port: int = 9200

    # Stage 8 — devops (PR8). Nightly `pg_dump` backup upload target. `None`
    # (the default, e.g. local dev) means the backup job no-ops cleanly
    # rather than raising — not every environment needs backups configured.
    # Retention (14 most recent daily backups) is enforced by an S3
    # lifecycle rule on the `pg_dump/` key prefix (Terraform, PR9/10), NOT
    # application code.
    backup_s3_bucket: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return a cached `Settings` instance."""
    return Settings()

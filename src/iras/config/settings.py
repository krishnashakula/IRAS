"""IRAS configuration settings loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Anthropic ──────────────────────────────────────────────────────────────
    # Stored as SecretStr so the value is redacted in logs and repr() output.
    anthropic_api_key: SecretStr

    # ── Postgres ───────────────────────────────────────────────────────────────
    # Contains credentials — kept as SecretStr to prevent accidental logging.
    postgres_url: SecretStr

    # ── LangSmith ─────────────────────────────────────────────────────────────
    langsmith_api_key: str
    langsmith_project: str = "iras"

    # ── Logfire ───────────────────────────────────────────────────────────────
    logfire_token: str

    # ── Slack ─────────────────────────────────────────────────────────────────
    slack_bot_token: SecretStr
    slack_oncall_channel_id: str

    # ── PagerDuty ─────────────────────────────────────────────────────────────
    pagerduty_integration_key: SecretStr

    # ── Security ──────────────────────────────────────────────────────────────
    # Optional HMAC secret for validating incoming webhook payloads.
    # If set, POST /webhook/alert requires an X-IRAS-Signature header:
    #   X-IRAS-Signature: sha256=<hmac_hex>
    # Leave unset (or empty) to disable signature verification (dev only).
    webhook_secret: SecretStr | None = None

    # Optional bearer token protecting the approve/reject endpoints.
    # If set, requests must carry: Authorization: Bearer <token>
    # Leave unset to disable (dev only).
    approval_api_key: SecretStr | None = None

    # Comma-separated list of allowed CORS origins.
    # Use "*" only for development. Example: "https://app.example.com"
    cors_allowed_origins: str = "*"

    # ── Observability backends ─────────────────────────────────────────────────
    prometheus_base_url: str
    elasticsearch_base_url: str | None = None
    loki_base_url: str | None = None

    # ── Tuning ────────────────────────────────────────────────────────────────
    rca_confidence_threshold: float = 0.7
    rca_max_attempts: int = 3
    approval_timeout_p0_minutes: int = 15
    approval_timeout_default_minutes: int = 120

    # ── Application ───────────────────────────────────────────────────────────
    app_env: str = "development"
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()  # type: ignore[call-arg]

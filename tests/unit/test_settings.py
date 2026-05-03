"""Unit tests for iras.config.settings."""

# pylint: disable=missing-class-docstring,import-outside-toplevel,too-few-public-methods
from __future__ import annotations


class TestGetSettings:
    def test_get_settings_reads_env_vars(self, monkeypatch):
        """Covers settings.py line 55 — get_settings() returns a Settings singleton."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
        monkeypatch.setenv("POSTGRES_URL", "postgresql://localhost/testdb")
        monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test-key")
        monkeypatch.setenv("LOGFIRE_TOKEN", "lf-test-token")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-bot")
        monkeypatch.setenv("SLACK_ONCALL_CHANNEL_ID", "C12345TEST")
        monkeypatch.setenv("PAGERDUTY_INTEGRATION_KEY", "pd-integration-key")
        monkeypatch.setenv("PROMETHEUS_BASE_URL", "http://prometheus:9090")

        from iras.config.settings import get_settings

        get_settings.cache_clear()
        try:
            settings = get_settings()
            assert settings.anthropic_api_key.get_secret_value() == "sk-ant-test-key"
            assert settings.postgres_url.get_secret_value() == "postgresql://localhost/testdb"
            assert settings.slack_bot_token.get_secret_value() == "xoxb-test-bot"
            assert settings.app_env == "development"  # default
            assert settings.rca_confidence_threshold == 0.7  # default
        finally:
            get_settings.cache_clear()

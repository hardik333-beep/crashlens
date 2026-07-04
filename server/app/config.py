"""Application configuration loaded from the environment.

Secrets have no defaults on purpose: the application must be given a
DATABASE_URL, REDIS_URL, and SECRET_KEY or it will refuse to start.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings sourced from environment variables (or a local .env)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Required, no defaults. Absence is a startup error, never a silent fallback.
    database_url: str
    redis_url: str
    secret_key: str

    # Non-secret operational settings may carry safe defaults.
    environment: str = "development"

    # --- Email alerts (all OPTIONAL) -----------------------------------------
    # Email alerting is off unless BOTH smtp_host and smtp_from are configured.
    # When unset, the alert engine logs a single warning once per process and
    # skips email channels; Slack and generic webhook channels are unaffected.
    # smtp_username / smtp_password are omitted for relays that do not require
    # authentication (e.g. an internal MTA on localhost). These are read from
    # SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM,
    # SMTP_STARTTLS by pydantic-settings (case-insensitive env names).
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True

    # Optional public base URL (e.g. https://crashlens.example.com) prefixed to
    # the relative issue link in alert bodies. When unset, alerts carry the
    # relative path only.
    public_base_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()

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


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()

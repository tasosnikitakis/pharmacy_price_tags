"""Core configuration. Everything comes from environment variables (or a local .env).

Secrets are required and have no defaults: the app refuses to start without them
(docs/decisions/0010). Generate them with ``python -m core.gen_secrets``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_LENGTH = 32


class ConfigError(RuntimeError):
    """Configuration is missing or invalid. The message names fields, never values."""


def config_error(exc: ValidationError) -> ConfigError:
    """Turn a settings ``ValidationError`` into a message without any input values.

    Pydantic's own message echoes the raw input, which would print the other secrets
    to the log of a process that fails to start.
    """
    problems = "; ".join(
        f"{'.'.join(str(p) for p in err['loc']).upper()}: {err['msg']}" for err in exc.errors()
    )
    return ConfigError(f"invalid configuration: {problems}")


def check_secret(value: SecretStr) -> SecretStr:
    """Reject secrets that are too short to be generated ones."""
    if len(value.get_secret_value()) < MIN_SECRET_LENGTH:
        raise ValueError(f"must be at least {MIN_SECRET_LENGTH} characters; generate one")
    return value


class DatabaseSettings(BaseSettings):
    """Only what migrations and database tools need."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: SecretStr = Field(
        description="SQLAlchemy URL: postgresql+psycopg://<user>:<password>@<host>:5432/<db>"
    )


class Settings(DatabaseSettings):
    """Everything the core app needs. All secrets are required."""

    session_secret: SecretStr = Field(description="Signs admin UI session cookies")
    gateway_token: SecretStr = Field(
        description="Bearer token the gateway presents; the core stores only its hash"
    )
    store_id: int = 1
    display_timezone: str = "Europe/Athens"
    stale_import_days: int = 7

    _check_secrets = field_validator("session_secret", "gateway_token")(check_secret)


def get_database_settings() -> DatabaseSettings:
    try:
        return DatabaseSettings()
    except ValidationError as exc:
        raise config_error(exc) from None


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()  # fields are filled from the environment
    except ValidationError as exc:
        raise config_error(exc) from None

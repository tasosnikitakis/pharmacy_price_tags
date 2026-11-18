"""Core configuration. Everything comes from environment variables (or a local .env).

Secrets are required and have no defaults: the app refuses to start without them
(docs/decisions/0010). Generate them with ``python -m core.gen_secrets``.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

MIN_SECRET_LENGTH = 32

#: Where Docker Compose mounts secret files; each file is named after a field.
#: Environment variables take precedence over these files. Read once, at import time.
_SECRETS_DIR = os.environ.get("SECRETS_DIR", "/run/secrets")
SECRETS_DIR = _SECRETS_DIR if os.path.isdir(_SECRETS_DIR) else None


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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", secrets_dir=SECRETS_DIR)

    database_url: SecretStr = Field(
        description="SQLAlchemy URL, e.g. postgresql+psycopg://<user>@<host>:5432/<db>. "
        "May embed the password, or take it from DATABASE_PASSWORD."
    )
    database_password: SecretStr | None = Field(
        default=None,
        description="Password added to DATABASE_URL when the URL has none "
        "(the Compose secret file database_password)",
    )

    @model_validator(mode="after")
    def _merge_password(self) -> DatabaseSettings:
        """Add the secret-file password to a URL that has none.

        Outside Docker (cloud sessions, CI, Windows development) there is no secret
        file, and ``DATABASE_URL`` is used exactly as given. With a secret file, only
        the trailing newline is removed (``echo`` and editors add one); the password is
        then URL-escaped by SQLAlchemy when the URL is rendered, so ``@ / : %`` and
        other reserved characters are safe.
        """
        if self.database_password is None:
            return self
        password = self.database_password.get_secret_value().rstrip("\r\n")
        if not password:
            raise ValueError("database_password is empty")
        url = make_url(self.database_url.get_secret_value())
        if url.password is not None:
            raise ValueError(
                "database password given twice: in DATABASE_URL and as database_password"
            )
        merged = url.set(password=password).render_as_string(hide_password=False)
        self.database_url = SecretStr(merged)
        return self


class Settings(DatabaseSettings):
    """Everything the core app needs. All secrets are required."""

    session_secret: SecretStr = Field(description="Signs admin UI session cookies")
    gateway_token: SecretStr = Field(
        description="Bearer token the gateway presents; the core stores only its hash"
    )
    store_id: int = 1
    display_timezone: str = "Europe/Athens"
    stale_import_days: int = 7

    @field_validator("session_secret", "gateway_token")
    @classmethod
    def _check_secrets(cls, value: SecretStr) -> SecretStr:
        return check_secret(SecretStr(value.get_secret_value().strip()))


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

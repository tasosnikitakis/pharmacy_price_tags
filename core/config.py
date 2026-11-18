"""Core configuration. Everything comes from environment variables (or a local .env).

Secrets are required and have no defaults: the app refuses to start without them
(docs/decisions/0010). Generate them with ``python -m core.gen_secrets``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SecretsSettingsSource,
    SettingsConfigDict,
)
from sqlalchemy.engine import make_url

MIN_SECRET_LENGTH = 32

#: Where Docker Compose mounts secret files; each file is named after a field.
#: Environment variables take precedence over these files. Read once, at import time.
_SECRETS_DIR = os.environ.get("SECRETS_DIR", "/run/secrets")
SECRETS_DIR = _SECRETS_DIR if os.path.isdir(_SECRETS_DIR) else None


#: Settings that may come from a secret file of the same name.
SECRET_FILE_FIELDS = ("database_password", "session_secret", "gateway_token")


def read_secret_file(path: Path) -> str:
    """A secret file's value: its text minus trailing newlines, nothing else.

    This matches the Postgres image (``$(< file)`` drops only trailing newlines), so the
    core and the database always see the same password. Other surrounding whitespace,
    including a ``\\r`` from a Windows editor, is kept here and refused by validation.
    """
    # Bytes, not read_text(): universal-newline decoding would turn CRLF into LF
    # and hide a carriage return that Postgres keeps.
    return path.read_bytes().decode("utf-8").rstrip("\n")


class SecretFilesSource(PydanticBaseSettingsSource):
    """Reads ``SECRET_FILE_FIELDS`` from files with ``read_secret_file``.

    Replaces pydantic-settings' own secrets source, which strips all whitespace and
    would hide a password that Postgres reads differently.
    """

    def __init__(self, settings_cls: type[BaseSettings], secrets_dir: Any) -> None:
        super().__init__(settings_cls)
        self.secrets_dir = Path(secrets_dir) if secrets_dir else None

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False  # unused; __call__ does the work

    def __call__(self) -> dict[str, Any]:
        if self.secrets_dir is None or not self.secrets_dir.is_dir():
            return {}
        values: dict[str, Any] = {}
        for name in SECRET_FILE_FIELDS:
            path = self.secrets_dir / name
            if name in self.settings_cls.model_fields and path.is_file():
                values[name] = read_secret_file(path)
        return values


def check_no_surrounding_whitespace(value: SecretStr) -> SecretStr:
    """Refuse secrets with leading or trailing whitespace (spaces, tabs, ``\\r``)."""
    raw = value.get_secret_value()
    if raw != raw.strip():
        raise ValueError("has leading or trailing whitespace; remove it")
    return value


class ConfigError(RuntimeError):
    """Configuration is missing or invalid. The message names fields, never values."""


def config_error(exc: ValidationError) -> ConfigError:
    """Turn a settings ``ValidationError`` into a message without any input values.

    Pydantic's own message echoes the raw input, which would print the other secrets
    to the log of a process that fails to start.
    """
    problems = "; ".join(f"{_field_label(err['loc'])}: {err['msg']}" for err in exc.errors())
    return ConfigError(f"invalid configuration: {problems}")


#: Where an operator sets each value, when that differs from the field name.
_SET_AS = {"DATABASE_PASSWORD": "DATABASE_PASSWORD (in Compose: POSTGRES_PASSWORD)"}


def _field_label(loc: tuple[int | str, ...]) -> str:
    name = ".".join(str(p) for p in loc).upper()
    return _SET_AS.get(name, name)


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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        secrets_dir = (
            file_secret_settings.secrets_dir
            if isinstance(file_secret_settings, SecretsSettingsSource)
            else None
        )
        files = SecretFilesSource(settings_cls, secrets_dir)
        return (init_settings, env_settings, dotenv_settings, files)

    @field_validator("database_password")
    @classmethod
    def _password_clean(cls, value: SecretStr | None) -> SecretStr | None:
        return None if value is None else check_no_surrounding_whitespace(value)

    @model_validator(mode="after")
    def _merge_password(self) -> DatabaseSettings:
        """Add the secret-file password to a URL that has none.

        Outside Docker (cloud sessions, CI, Windows development) there is no secret
        file, and ``DATABASE_URL`` is used exactly as given. A secret file loses only its
        trailing newlines (``read_secret_file``); a password with any other surrounding
        whitespace is refused, because it is almost always a mistake. The password is
        then URL-escaped by SQLAlchemy when the URL is rendered, so ``@ / : %`` and other
        reserved characters are safe.
        """
        if self.database_password is None:
            return self
        password = self.database_password.get_secret_value()
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
        return check_secret(check_no_surrounding_whitespace(value))


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

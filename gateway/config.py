"""Gateway configuration from environment variables. Secrets are required."""

from __future__ import annotations

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_LENGTH = 32


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    core_url: AnyHttpUrl = Field(description="Base URL of the core, e.g. https://esl.local")
    gateway_token: SecretStr = Field(description="Same value as the core's GATEWAY_TOKEN")
    gateway_driver: str = "fake"

    @field_validator("gateway_token")
    @classmethod
    def _long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_SECRET_LENGTH:
            raise ValueError(f"must be at least {MIN_SECRET_LENGTH} characters; generate one")
        return value


class GatewayConfigError(RuntimeError):
    """Configuration is missing or invalid. The message names fields, never values."""


def load_gateway_settings() -> GatewaySettings:
    """Load settings, failing with a message that contains no input values."""
    try:
        return GatewaySettings()
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']).upper()}: {e['msg']}" for e in exc.errors()
        )
        raise GatewayConfigError(f"invalid configuration: {problems}") from None

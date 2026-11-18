"""Core configuration. Everything comes from environment variables (or a local .env)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(
        description="SQLAlchemy URL, e.g. postgresql+psycopg://esl:esl@localhost:5432/esl"
    )
    store_id: int = 1
    display_timezone: str = "Europe/Athens"
    stale_import_days: int = 7


@lru_cache
def get_settings() -> Settings:
    return Settings()  # fields are filled from the environment

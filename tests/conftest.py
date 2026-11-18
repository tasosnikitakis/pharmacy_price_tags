"""Shared fixtures. Database tests use DATABASE_URL (started by the SessionStart hook)."""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from core.config import get_settings

# Throwaway secrets for the test run only, generated fresh each time.
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
os.environ.setdefault("GATEWAY_TOKEN", secrets.token_urlsafe(48))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


#: Fixtures that need Postgres. Any test using one is marked ``db`` automatically, so
#: the Windows CI job (``-m "not db"``) never needs a database.
DB_FIXTURES = frozenset({"database_url", "migrated_engine", "db"})


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if DB_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.db)


def alembic_config(database_url: str) -> Config:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.attributes["database_url"] = database_url
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def database_url() -> str:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is not set")
    return get_settings().database_url.get_secret_value()


@pytest.fixture(scope="session")
def migrated_engine(database_url: str) -> Iterator[Engine]:
    """Engine on the development database, upgraded to the latest migration."""
    command.upgrade(alembic_config(database_url), "head")
    engine = create_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator[Session]:
    """A session whose work is rolled back after the test."""
    with migrated_engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()

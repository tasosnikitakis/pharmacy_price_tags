"""The core starts while Postgres is down (M0 behaviour), with the gateway router in.

No database is needed, so this runs in the database-free (Windows) CI job too.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL

from core.app.main import create_app
from core.config import get_settings
from core.db.session import get_engine, get_sessionmaker
from shared.gateway_api import API_PREFIX


def _clear_caches() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture
def database_down(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Port 1 on localhost: nothing listens, so connecting fails at once.
    url = URL.create(
        "postgresql+psycopg", "esl", None, "127.0.0.1", 1, "esl", query={"connect_timeout": "2"}
    )
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    monkeypatch.delenv("DATABASE_PASSWORD", raising=False)
    _clear_caches()
    yield
    _clear_caches()


def test_core_starts_and_reports_unavailable_database(
    database_down: None, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        caplog.at_level(logging.WARNING),
        TestClient(create_app(), raise_server_exceptions=False) as client,
    ):  # runs the lifespan
        health = client.get("/health")
        assert health.status_code == 503
        assert health.json()["database"] == "unavailable"
        # Gateway requests fail cleanly too, and retry provisioning lazily.
        token = os.environ["GATEWAY_TOKEN"]
        r = client.post(
            f"{API_PREFIX}/heartbeat",
            json={"version": "1", "driver": "fake"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 500  # the database error, not a crash or a false 401
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("gateway row not provisioned" in m for m in messages)
    assert not any(token in m for m in messages)

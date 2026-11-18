from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from core.app.main import create_app
from core.config import ConfigError, get_settings
from core.db.session import get_session


def test_health_ok_with_database(db: Session) -> None:
    app = create_app()

    def _session() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_session] = _session
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["database"] == "ok"


class _BrokenSession:
    def execute(self, *_: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("down"))


def test_health_reports_unavailable_database() -> None:
    app = create_app()

    def _session() -> Iterator[_BrokenSession]:
        yield _BrokenSession()

    app.dependency_overrides[get_session] = _session
    response = TestClient(app).get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": "unavailable", "version": "0.1.0"}


@pytest.mark.parametrize("missing", ["SESSION_SECRET", "GATEWAY_TOKEN", "DATABASE_URL"])
def test_app_refuses_to_start_without_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env to fall back on
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.delenv(missing)
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigError, match=missing) as exc:
            create_app()
    finally:
        get_settings.cache_clear()
    for name in ("SESSION_SECRET", "GATEWAY_TOKEN"):
        if name != missing:
            assert os.environ[name] not in str(exc.value)  # never echo other secrets
    assert "u:p@" not in str(exc.value)


def test_short_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_SECRET", "too-short")
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigError, match="at least 32"):
            create_app()
    finally:
        get_settings.cache_clear()


def test_gateway_settings_require_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from gateway.config import GatewayConfigError, load_gateway_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CORE_URL", "http://localhost:8000")
    monkeypatch.delenv("GATEWAY_TOKEN")
    with pytest.raises(GatewayConfigError, match="GATEWAY_TOKEN"):
        load_gateway_settings()


def test_generated_secrets_pass_validation() -> None:
    from core.gen_secrets import generate

    assert all(len(v) >= 32 for v in generate().values())


def test_database_password_from_secret_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core.config import DatabaseSettings

    (tmp_path / "database_password").write_text("s3cret-from-file\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://esl@db:5432/esl")
    monkeypatch.delenv("DATABASE_PASSWORD", raising=False)
    settings = DatabaseSettings(_secrets_dir=tmp_path)
    assert settings.database_url.get_secret_value() == (
        "postgresql+psycopg://esl:s3cret-from-file@db:5432/esl"
    )


def test_database_password_given_twice_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core.config import DatabaseSettings

    (tmp_path / "database_password").write_text("from-file", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://esl:other@db:5432/esl")
    with pytest.raises(ValueError, match="twice"):
        DatabaseSettings(_secrets_dir=tmp_path)


def test_empty_database_password_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core.config import DatabaseSettings

    (tmp_path / "database_password").write_text("", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://esl@db:5432/esl")
    with pytest.raises(ValueError, match="empty"):
        DatabaseSettings(_secrets_dir=tmp_path)


@pytest.mark.filterwarnings("ignore:directory .* does not exist:UserWarning")
def test_database_url_with_password_used_as_is_without_secret_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cloud sessions, CI and Windows: no secret file, DATABASE_URL taken verbatim."""
    from core.config import DatabaseSettings

    url = "postgresql+psycopg://esl:dev-pass@localhost:5432/esl"
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("DATABASE_PASSWORD", raising=False)
    for secrets_dir in (None, tmp_path / "does-not-exist", tmp_path):  # none, missing, empty
        settings = DatabaseSettings(_secrets_dir=secrets_dir)
        assert settings.database_password is None
        assert settings.database_url.get_secret_value() == url


@pytest.mark.parametrize(
    "password", ["p@ss/w:rd%+#?x", "a%40b", "colon:only", "slash/only", "x@y", "spa ce"]
)
def test_secret_file_password_is_escaped_and_newline_stripped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, password: str
) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from core.config import DatabaseSettings

    (tmp_path / "database_password").write_text(password + "\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://esl@db:5432/esl")
    monkeypatch.delenv("DATABASE_PASSWORD", raising=False)
    rendered = DatabaseSettings(_secrets_dir=tmp_path).database_url.get_secret_value()

    parsed = make_url(rendered)
    assert parsed.password == password  # exact, newline gone, nothing else changed
    assert (parsed.username, parsed.host, parsed.port, parsed.database) == (
        "esl", "db", 5432, "esl",
    )  # fmt: skip
    # In the URL text the password is escaped: no raw reserved characters, every "%"
    # starts a %XX escape, and unescaping gives the original back.
    segment = rendered.split("://", 1)[1].rsplit("@db", 1)[0].split(":", 1)[1]
    assert not set("@/:") & set(segment)
    assert re.fullmatch(r"(?:[^%]|%[0-9A-Fa-f]{2})*", segment)
    assert unquote(segment) == password
    # What the driver will receive: the raw password as its own argument.
    engine = create_engine(rendered)
    _, kwargs = engine.dialect.create_connect_args(engine.url)
    assert kwargs["password"] == password
    assert kwargs["host"] == "db"

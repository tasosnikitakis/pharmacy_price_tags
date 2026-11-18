from __future__ import annotations

import os
import re
import secrets
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from core.app.main import create_app
from core.config import ConfigError, get_settings
from core.db.session import get_session


def _fake_url(password: str | None = None, host: str = "db") -> str:
    """A database URL for config tests. Passwords are generated per run, so the
    repository contains no password-shaped literals for secret scanners to flag."""
    from sqlalchemy import URL

    url = URL.create("postgresql+psycopg", "esl", password, host, 5432, "esl")
    return url.render_as_string(hide_password=False)


def _fake_password() -> str:
    return secrets.token_hex(12)


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
    db_password = _fake_password()
    monkeypatch.setenv("DATABASE_URL", _fake_url(db_password))
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
    assert db_password not in str(exc.value)


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

    password = _fake_password()
    (tmp_path / "database_password").write_text(password + "\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", _fake_url())
    monkeypatch.delenv("DATABASE_PASSWORD", raising=False)
    settings = DatabaseSettings(_secrets_dir=tmp_path)
    assert settings.database_url.get_secret_value() == _fake_url(password)


def test_database_password_given_twice_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core.config import DatabaseSettings

    (tmp_path / "database_password").write_text(_fake_password(), encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", _fake_url(_fake_password()))
    with pytest.raises(ValueError, match="twice"):
        DatabaseSettings(_secrets_dir=tmp_path)


def test_empty_database_password_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core.config import DatabaseSettings

    (tmp_path / "database_password").write_text("", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", _fake_url())
    with pytest.raises(ValueError, match="empty"):
        DatabaseSettings(_secrets_dir=tmp_path)


@pytest.mark.filterwarnings("ignore:directory .* does not exist:UserWarning")
def test_database_url_with_password_used_as_is_without_secret_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cloud sessions, CI and Windows: no secret file, DATABASE_URL taken verbatim."""
    from core.config import DatabaseSettings

    url = _fake_url(_fake_password(), host="localhost")
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("DATABASE_PASSWORD", raising=False)
    for secrets_dir in (None, tmp_path / "does-not-exist", tmp_path):  # none, missing, empty
        settings = DatabaseSettings(_secrets_dir=secrets_dir)
        assert settings.database_password is None
        assert settings.database_url.get_secret_value() == url


#: Characters that must be escaped in a URL's password, and a pre-escaped look-alike
#: ("%40" must stay the three characters %, 4, 0 and not become "@").
_RESERVED = ["@", "/", ":", "%", "+", "#", "?", " ", "@/:%+#?", "%40"]


@pytest.mark.parametrize("reserved", _RESERVED)
def test_secret_file_password_is_escaped_and_newline_stripped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reserved: str
) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from core.config import DatabaseSettings

    # Generated per run, so the repository holds no password-shaped literals.
    password = f"{secrets.token_hex(4)}{reserved}{secrets.token_hex(4)}"
    (tmp_path / "database_password").write_text(password + "\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", _fake_url())
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


@pytest.mark.parametrize("suffix", ["\n", "\n\n", ""])
def test_secret_file_loses_only_trailing_newlines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, suffix: str
) -> None:
    """Same rule as the Postgres image: trailing newlines go, nothing else changes."""
    from core.config import DatabaseSettings, read_secret_file

    password = _fake_password()
    (tmp_path / "database_password").write_text(password + suffix, encoding="utf-8")
    assert read_secret_file(tmp_path / "database_password") == password
    monkeypatch.setenv("DATABASE_URL", _fake_url())
    monkeypatch.delenv("DATABASE_PASSWORD", raising=False)
    settings = DatabaseSettings(_secrets_dir=tmp_path)
    assert settings.database_url.get_secret_value() == _fake_url(password)


@pytest.mark.parametrize("wrap", [" {}", "{} ", "\t{}", "{}\r\n", "{}\r", " {}\n"])
def test_secret_file_password_with_surrounding_whitespace_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wrap: str
) -> None:
    """Postgres would keep the whitespace and the core would not (or the reverse),
    so the passwords would differ; refuse at startup instead."""
    from core.config import DatabaseSettings

    (tmp_path / "database_password").write_text(wrap.format(_fake_password()), encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", _fake_url())
    monkeypatch.delenv("DATABASE_PASSWORD", raising=False)
    with pytest.raises(ValidationError, match="whitespace"):
        DatabaseSettings(_secrets_dir=tmp_path)


@pytest.mark.parametrize("name", ["SESSION_SECRET", "GATEWAY_TOKEN"])
def test_app_secret_with_surrounding_whitespace_is_refused(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, secrets.token_hex(32) + " ")
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigError, match=f"{name}: .*whitespace"):
            create_app()
    finally:
        get_settings.cache_clear()


def test_app_secrets_read_from_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from core.config import Settings

    session, token = secrets.token_hex(32), secrets.token_hex(32)
    (tmp_path / "session_secret").write_text(session + "\n", encoding="utf-8")
    (tmp_path / "gateway_token").write_text(token + "\n", encoding="utf-8")
    monkeypatch.delenv("SESSION_SECRET")
    monkeypatch.delenv("GATEWAY_TOKEN")
    settings = Settings(_secrets_dir=tmp_path)
    assert settings.session_secret.get_secret_value() == session
    assert settings.gateway_token.get_secret_value() == token

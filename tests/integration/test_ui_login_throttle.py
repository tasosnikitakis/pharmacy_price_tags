"""Login throttling through the app (decision 0019): 429 with ``Retry-After`` and a
Greek message, per account and per IP, logged as ``auth.throttled``."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.app.i18n import error_message, t
from core.app.throttle import account_hash
from core.app.uploads import UploadStore
from core.config import Settings
from core.db.models import AppUser, EventLog
from core.services.users import authenticate
from tests.integration.test_ui_helpers import (
    PASSWORD,
    Clock,
    add_user,
    build_ui_app,
    csrf_of,
    suffix,
)

pytestmark = pytest.mark.db

# The defaults, read without building Settings: collecting this module must not need
# DATABASE_URL (the Windows CI job collects every test, then deselects db ones).
ACCOUNT_FREE: int = Settings.model_fields["login_throttle_account_free"].default
IP_FREE: int = Settings.model_fields["login_throttle_ip_free"].default
WRONG = "not the password at all"


@dataclass
class Env:
    app: FastAPI
    db: Session
    clock: Clock
    user: AppUser
    other: AppUser
    client: Callable[[str], TestClient]


@pytest.fixture(name="env")
def env_fixture(db: Session, tmp_path: Path) -> Iterator[Env]:
    clock = Clock()
    # A fresh app, so a fresh throttle: no state leaks between tests.
    app = build_ui_app(db, clock, UploadStore(tmp_path / "uploads"))
    user = add_user(db, "staff")
    other = add_user(db, "staff")

    def client(ip: str) -> TestClient:
        return TestClient(app, client=(ip, 50000))

    yield Env(app, db, clock, user, other, client)


def post(client: TestClient, email: str, password: str) -> tuple[int, dict[str, str], str]:
    """One login attempt from a fresh login page: status, headers and page text."""
    token = csrf_of(client.get("/login").text)
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": token, "next": "/products"},
        follow_redirects=False,
    )
    return response.status_code, dict(response.headers), response.text


def fail_until_throttled(client: TestClient, email: str, free: int = ACCOUNT_FREE) -> None:
    """``free`` failures cost nothing; the one after them sets the first delay."""
    for _ in range(free + 1):
        status, _, _ = post(client, email, WRONG)
        assert status == 401


def throttle_rows(db: Session) -> list[EventLog]:
    return list(
        db.scalars(select(EventLog).where(EventLog.kind == "auth.throttled").order_by(EventLog.id))
    )


def test_throttled_after_repeated_failures(env: Env) -> None:
    c = env.client("10.0.0.1")
    fail_until_throttled(c, env.user.email)
    status, headers, text = post(c, env.user.email, PASSWORD)  # the right password
    assert status == 429
    assert headers["retry-after"] == "2"
    assert t("login.throttled.seconds", n=2) in text
    assert "Δοκιμάστε ξανά σε 2 δευτερόλεπτα" in text
    assert c.get("/products", follow_redirects=False).status_code == 303  # not logged in


def test_correct_password_works_after_the_delay(env: Env) -> None:
    c = env.client("10.0.0.1")
    fail_until_throttled(c, env.user.email)
    env.clock.now += timedelta(seconds=1)
    assert post(c, env.user.email, PASSWORD)[0] == 429
    env.clock.now += timedelta(seconds=1)
    status, headers, _ = post(c, env.user.email, PASSWORD)
    assert status == 303
    assert headers["location"] == "/products"


def test_success_clears_the_account_but_not_the_ip(env: Env) -> None:
    c = env.client("10.0.0.1")
    for _ in range(ACCOUNT_FREE):
        assert post(c, env.user.email, WRONG)[0] == 401
    assert post(c, env.user.email, PASSWORD)[0] == 303
    throttle = env.app.state.login_throttle
    assert env.user.email not in throttle.account
    assert throttle.ip._entries["10.0.0.1"].failures == ACCOUNT_FREE


def test_minutes_in_the_message(env: Env) -> None:
    c = env.client("10.0.0.1")
    fail_until_throttled(c, env.user.email)
    # Keep failing after each delay until the delay passes a minute (2, 4 ... 32, 64).
    for delay in (2, 4, 8, 16, 32):
        env.clock.now += timedelta(seconds=delay)
        assert post(c, env.user.email, WRONG)[0] == 401
    status, headers, text = post(c, env.user.email, WRONG)
    assert status == 429
    assert headers["retry-after"] == "64"
    assert t("login.throttled.minutes", n=2) in text


def test_unknown_email_is_throttled_the_same_way(env: Env) -> None:
    c = env.client("10.0.0.1")
    unknown = f"nobody-{suffix()}@example.test"
    fail_until_throttled(c, unknown)
    status, headers, text = post(c, unknown, WRONG)
    assert status == 429
    assert headers["retry-after"] == "2"
    assert t("login.throttled.seconds", n=2) in text


def test_email_is_normalised_for_the_key(env: Env) -> None:
    c = env.client("10.0.0.1")
    fail_until_throttled(c, env.user.email)
    assert post(c, f"  {env.user.email.upper()} ", PASSWORD)[0] == 429


def test_other_account_from_other_ip_is_not_affected(env: Env) -> None:
    fail_until_throttled(env.client("10.0.0.1"), env.user.email)
    status, _, _ = post(env.client("10.0.0.2"), env.other.email, PASSWORD)
    assert status == 303


def test_other_account_from_the_same_ip_is_not_affected_below_the_ip_threshold(
    env: Env,
) -> None:
    c = env.client("10.0.0.1")
    fail_until_throttled(c, env.user.email)
    assert post(env.client("10.0.0.1"), env.other.email, PASSWORD)[0] == 303


def test_many_accounts_from_one_ip_trip_the_ip_key(env: Env) -> None:
    assert IP_FREE > ACCOUNT_FREE
    c = env.client("192.168.1.10")
    # One failure for each of many emails: no account key gets near its threshold.
    for _ in range(IP_FREE + 1):
        assert post(c, f"guess-{suffix()}@example.test", WRONG)[0] == 401
    status, headers, _ = post(c, env.user.email, PASSWORD)
    assert status == 429
    assert headers["retry-after"] == "2"
    # The same account from another address still logs in.
    assert post(env.client("192.168.1.11"), env.user.email, PASSWORD)[0] == 303
    kinds = [(r.payload["event"], r.payload["key"]) for r in throttle_rows(env.db)]
    assert ("throttled", "ip") in kinds
    assert ("refused", "ip") in kinds
    assert all(key == "ip" for _, key in kinds)


def test_authenticate_is_not_called_while_throttled(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def counting(session: Session, email: str, password: str) -> AppUser | None:
        calls.append(email)
        return authenticate(session, email, password)

    monkeypatch.setattr("core.app.routers.ui_auth.authenticate", counting)
    c = env.client("10.0.0.1")
    fail_until_throttled(c, env.user.email)
    assert len(calls) == ACCOUNT_FREE + 1
    for _ in range(3):
        assert post(c, env.user.email, PASSWORD)[0] == 429
    assert len(calls) == ACCOUNT_FREE + 1


def test_throttle_events_are_logged_without_email_or_password(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    c = env.client("10.0.0.7")
    before = {r.id for r in throttle_rows(env.db)}
    with caplog.at_level(logging.DEBUG):
        fail_until_throttled(c, env.user.email)
        for _ in range(5):  # a flood of refusals in one throttle period
            assert post(c, env.user.email, PASSWORD)[0] == 429
    rows = [r for r in throttle_rows(env.db) if r.id not in before]
    # One row when the key became throttled, one for the first refusal: the other
    # refusals of the same period are not logged again.
    assert [r.payload["event"] for r in rows] == ["throttled", "refused"]
    throttled, refused = rows
    assert throttled.payload == {
        "event": "throttled",
        "key": "account",
        "failures": ACCOUNT_FREE + 1,
        "delay_seconds": 2,
        "ip": "10.0.0.7",
        "account": account_hash(env.user.email),
    }
    assert refused.payload["delay_seconds"] == 2
    for row in rows:
        assert row.actor == "system"
        assert row.at == env.clock.now
        assert row.at.tzinfo is not None and row.at.utcoffset() == timedelta(0)
        dumped = json.dumps(row.payload) + row.actor
        assert env.user.email not in dumped
        assert env.user.email.split("@")[0] not in dumped
        assert PASSWORD not in dumped and WRONG not in dumped
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "login" in r.message]
    assert len(warnings) == 2
    assert env.user.email not in caplog.text
    assert PASSWORD not in caplog.text and WRONG not in caplog.text


def test_a_new_throttle_period_logs_its_first_refusal_again(env: Env) -> None:
    c = env.client("10.0.0.8")
    before = {r.id for r in throttle_rows(env.db)}
    fail_until_throttled(c, env.user.email)
    assert post(c, env.user.email, WRONG)[0] == 429
    env.clock.now += timedelta(seconds=2)
    assert post(c, env.user.email, WRONG)[0] == 401  # next failure: a 4 s delay
    assert post(c, env.user.email, WRONG)[0] == 429
    assert post(c, env.user.email, WRONG)[0] == 429
    rows = [r for r in throttle_rows(env.db) if r.id not in before]
    assert [(r.payload["event"], r.payload["delay_seconds"]) for r in rows] == [
        ("throttled", 2),
        ("refused", 2),
        ("throttled", 4),
        ("refused", 4),
    ]


def test_each_app_has_its_own_throttle(env: Env, db: Session, tmp_path: Path) -> None:
    fail_until_throttled(env.client("10.0.0.1"), env.user.email)
    fresh = build_ui_app(db, Clock(), UploadStore(tmp_path / "other"))
    assert fresh.state.login_throttle is not env.app.state.login_throttle
    status, _, _ = post(TestClient(fresh, client=("10.0.0.1", 50000)), env.user.email, PASSWORD)
    assert status == 303


def test_wrong_password_below_threshold_is_still_401(env: Env) -> None:
    c = env.client("10.0.0.1")
    status, headers, text = post(c, env.user.email, WRONG)
    assert status == 401
    assert "retry-after" not in headers
    assert error_message("auth.invalid_credentials") in text

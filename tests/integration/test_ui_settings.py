"""Settings page (section 9): users, gateway status, thresholds (read-only)."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select

from core.app.i18n import error_message, t
from core.db.models import AppUser, EventLog, GatewayRow
from core.services.users import verify_password
from tests.integration.test_ui_helpers import (
    PASSWORD,
    T0,
    UI,
    login,
    suffix,
    ui_fixture,  # noqa: F401  (the "ui" fixture)
)

pytestmark = pytest.mark.db


def _user(ui: UI, email: str) -> AppUser:
    user = ui.db.scalars(select(AppUser).where(AppUser.email == email)).one()
    ui.db.refresh(user)
    return user


def test_settings_shows_gateway_and_thresholds_never_the_token(ui: UI) -> None:
    ui.db.add(GatewayRow(name=f"gw-{suffix()}", token_hash="scrypt$x", last_heartbeat_at=T0,
                         version="0.1.0", driver="fake"))  # fmt: skip
    ui.db.commit()
    ui.as_admin()
    page = ui.client.get("/settings").text
    assert t("settings.gateway.token_configured") in page
    assert os.environ["GATEWAY_TOKEN"] not in page
    assert os.environ["SESSION_SECRET"] not in page
    assert "scrypt$" not in page
    assert "01/12/2026 10:00" in page and "fake" in page and "0.1.0" in page
    assert t("settings.thresholds.days", days=7) in page
    assert t("settings.thresholds.percent", value=50) in page
    assert t("settings.thresholds.percent", value=20) in page
    assert t("settings.thresholds.env_hint") in page


def test_admin_adds_user_who_can_log_in(ui: UI) -> None:
    token = ui.as_admin()
    email = f"New-{suffix()}@Example.test"
    data = {"email": email, "role": "staff", "password": "a long password",
            "password_again": "a long password", "csrf_token": token}  # fmt: skip
    page = ui.client.post("/settings/users", data=data).text
    assert t("settings.users.added", email=email.lower()) in page
    user = _user(ui, email.lower())
    assert user.role == "staff" and user.active
    assert user.password_hash.startswith("scrypt$") and "a long password" not in user.password_hash
    event = ui.db.scalars(select(EventLog).where(EventLog.kind == "user.added")).all()[-1]
    assert "a long password" not in str(event.payload)
    ui.client.cookies.clear()
    login(ui.client, email, "a long password")
    assert ui.client.get("/settings").status_code == 403  # staff


@pytest.mark.parametrize(
    ("email", "password", "again", "code"),
    [("not-an-email", "a long password", "a long password", "user.email_invalid"),
     ("x@example.test", "short", "short", "user.password_too_short"),
     ("x@example.test", "a long password", "another password", "user.password_mismatch")],
)  # fmt: skip
def test_add_user_refusals(ui: UI, email: str, password: str, again: str, code: str) -> None:
    token = ui.as_admin()
    data = {"email": email, "role": "staff", "password": password, "password_again": again,
            "csrf_token": token}  # fmt: skip
    r = ui.client.post("/settings/users", data=data)
    assert r.status_code == 422
    assert error_message(code) in r.text


def test_duplicate_user(ui: UI) -> None:
    token = ui.as_admin()
    data = {"email": ui.staff.email, "role": "staff", "password": PASSWORD,
            "password_again": PASSWORD, "csrf_token": token}  # fmt: skip
    r = ui.client.post("/settings/users", data=data)
    assert error_message("user.exists") in r.text


def test_deactivate_and_reactivate(ui: UI) -> None:
    token = ui.as_admin()
    url = f"/settings/users/{ui.staff.id}/active"
    page = ui.client.post(url, data={"csrf_token": token}).text
    assert t("settings.users.deactivated", email=ui.staff.email) in page
    assert not _user(ui, ui.staff.email).active
    page = ui.client.post(url, data={"csrf_token": token, "active": "true"}).text
    assert t("settings.users.activated", email=ui.staff.email) in page
    assert _user(ui, ui.staff.email).active


def test_admin_cannot_deactivate_self(ui: UI) -> None:
    token = ui.as_admin()
    page = ui.client.post(f"/settings/users/{ui.admin.id}/active", data={"csrf_token": token}).text
    assert error_message("user.self_deactivate") in page
    assert _user(ui, ui.admin.email).active


def test_reset_password(ui: UI) -> None:
    token = ui.as_admin()
    data = {"password": "brand new password", "password_again": "brand new password",
            "csrf_token": token}  # fmt: skip
    page = ui.client.post(f"/settings/users/{ui.staff.id}/password", data=data).text
    assert t("settings.users.password_reset", email=ui.staff.email) in page
    assert verify_password("brand new password", _user(ui, ui.staff.email).password_hash)
    data["password_again"] = "different"
    page = ui.client.post(f"/settings/users/{ui.staff.id}/password", data=data).text
    assert error_message("user.password_mismatch") in page

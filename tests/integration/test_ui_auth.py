"""Login, roles, sessions and CSRF of the admin UI (section 9)."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from core.app.i18n import error_message, t
from core.services.users import reset_password
from tests.integration.test_ui_helpers import (
    PASSWORD,
    T0,
    UI,
    add_user,
    build_ui_app,
    csrf_of,
    login,
    ui_fixture,  # noqa: F401  (the "ui" fixture)
)

pytestmark = pytest.mark.db


def test_pages_need_login(ui: UI) -> None:
    for path in ("/", "/products", "/import", "/tags", "/settings"):
        response = ui.client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login?next=")


def test_htmx_request_without_login_gets_hx_redirect(ui: UI) -> None:
    response = ui.client.get("/products?q=x", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login?next=%2Fproducts%3Fq%3Dx"


def test_login_page_is_greek(ui: UI) -> None:
    page = ui.client.get("/login")
    assert page.status_code == 200
    assert t("login.title") in page.text
    assert '<html lang="el">' in page.text


def test_login_and_logout(ui: UI) -> None:
    token = ui.as_admin()
    page = ui.client.get("/products")
    assert page.status_code == 200
    assert ui.admin.email in page.text
    response = ui.client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert ui.client.get("/products", follow_redirects=False).status_code == 303


def test_login_returns_to_next_page_but_only_local(ui: UI) -> None:
    token = csrf_of(ui.client.get("/login").text)
    data = {"email": ui.staff.email, "password": PASSWORD, "csrf_token": token}
    r = ui.client.post("/login", data={**data, "next": "/tags"}, follow_redirects=False)
    assert r.headers["location"] == "/tags"
    ui.client.post("/logout", data={"csrf_token": csrf_of(ui.client.get("/tags").text)})
    token = csrf_of(ui.client.get("/login").text)
    data["csrf_token"] = token
    r = ui.client.post("/login", data={**data, "next": "//evil.example/x"}, follow_redirects=False)
    assert r.headers["location"] == "/"


@pytest.mark.parametrize("password", ["wrong password!", ""])
def test_wrong_password_is_refused_in_greek(
    ui: UI, password: str, caplog: pytest.LogCaptureFixture
) -> None:
    token = csrf_of(ui.client.get("/login").text)
    with caplog.at_level(logging.DEBUG):
        r = ui.client.post(
            "/login", data={"email": ui.admin.email, "password": password, "csrf_token": token}
        )
    assert r.status_code == 401
    assert error_message("auth.invalid_credentials") in r.text
    assert ui.client.get("/products", follow_redirects=False).status_code == 303
    # Neither the password nor the email reaches the log.
    assert PASSWORD not in caplog.text and ui.admin.email not in caplog.text
    if password:
        assert password not in caplog.text


def test_inactive_user_cannot_log_in(ui: UI) -> None:
    inactive = add_user(ui.db, "staff", active=False)
    token = csrf_of(ui.client.get("/login").text)
    r = ui.client.post(
        "/login", data={"email": inactive.email, "password": PASSWORD, "csrf_token": token}
    )
    assert r.status_code == 401


def test_deactivated_user_is_logged_out_at_once(ui: UI) -> None:
    ui.as_staff()
    assert ui.client.get("/products").status_code == 200
    ui.staff.active = False
    ui.db.commit()
    assert ui.client.get("/products", follow_redirects=False).status_code == 303


def test_password_reset_ends_other_sessions(ui: UI) -> None:
    ui.as_staff()
    reset_password(ui.db, ui.staff.id, "another long password", actor="test", now=T0)
    ui.db.commit()
    assert ui.client.get("/products", follow_redirects=False).status_code == 303
    login(ui.client, ui.staff.email, "another long password")
    assert ui.client.get("/products").status_code == 200


def test_login_needs_csrf_token(ui: UI) -> None:
    ui.client.get("/login")
    r = ui.client.post("/login", data={"email": ui.admin.email, "password": PASSWORD})
    assert r.status_code == 403
    assert error_message("csrf.invalid") in r.text


def test_state_changing_requests_need_csrf(ui: UI) -> None:
    token = ui.as_admin()
    r = ui.client.post("/settings/users", data={"email": "x@example.test"})
    assert r.status_code == 403
    r = ui.client.post("/settings/users", data={"email": "x@example.test", "csrf_token": "nope"})
    assert r.status_code == 403
    # HTMX sends the token as a header instead of a form field.
    r = ui.client.post(
        "/settings/users",
        data={"email": "bad", "password": "x", "password_again": "x"},
        headers={"X-CSRF-Token": token},
    )
    assert r.status_code == 422  # past the CSRF check, refused by validation


def test_staff_cannot_open_admin_pages_or_write(ui: UI) -> None:
    token = ui.as_staff()
    assert ui.client.get("/products").status_code == 200
    assert ui.client.get("/tags").status_code == 200
    assert ui.client.get("/import").status_code == 200
    r = ui.client.get("/settings")
    assert r.status_code == 403
    assert error_message("auth.forbidden") in r.text
    for path in ("/products/1/price", "/products/1/promo", "/products/1/promo/end",
                 "/products/1/fields", "/import/preview", "/import/apply",
                 "/tags/1/unbind", "/tags/1/jobs/1/retry", "/settings/users"):  # fmt: skip
        assert ui.client.post(path, data={"csrf_token": token}).status_code == 403, path


def test_staff_sees_no_settings_link(ui: UI) -> None:
    ui.as_staff()
    assert 'href="/settings"' not in ui.client.get("/products").text
    ui.client.cookies.clear()
    ui.as_admin()
    assert 'href="/settings"' in ui.client.get("/products").text


def test_session_cookie_flags_over_http(ui: UI) -> None:
    page = ui.client.get("/login")
    cookie = page.headers["set-cookie"]
    assert cookie.startswith("esl_session=")
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert "secure" not in cookie.lower().split(";", 1)[1]


def test_session_cookie_is_secure_over_https(db: Session, tmp_path: object) -> None:
    from pathlib import Path

    from core.app.uploads import UploadStore
    from tests.integration.test_ui_helpers import Clock

    app = build_ui_app(db, Clock(), UploadStore(Path(str(tmp_path))))
    client = TestClient(app, base_url="https://testserver")
    cookie = client.get("/login").headers["set-cookie"]
    flags = [part.strip().lower() for part in cookie.split(";")[1:]]
    assert "secure" in flags and "httponly" in flags and "samesite=lax" in flags


def test_session_cookie_holds_no_password(ui: UI) -> None:
    ui.as_admin()
    import base64
    import json

    raw = ui.client.cookies["esl_session"].split(".")[0]
    data = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
    assert set(data) <= {"uid", "pwf", "csrf", "flash"}
    assert PASSWORD not in json.dumps(data)


def test_unknown_page_is_greek_404(ui: UI) -> None:
    ui.as_staff()
    r = ui.client.get("/products/999999999")
    assert r.status_code == 404
    assert error_message("not_found") in r.text


def test_gateway_and_health_untouched(ui: UI) -> None:
    assert ui.client.get("/health").status_code in (200, 503)
    r = ui.client.post("/gateway/v1/heartbeat", json={"version": "1", "driver": "fake"})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid gateway token"}

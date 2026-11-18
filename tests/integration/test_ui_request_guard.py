"""Early refusal of state-changing requests, the body size cap, frame headers, and the
message after an admin resets their own password."""

from __future__ import annotations

import json
from base64 import b64encode
from collections.abc import Iterator

import pytest

from core.app.i18n import error_message, t
from core.app.web import MAX_REQUEST_BYTES, SESSION_COOKIE
from tests.integration.test_ui_helpers import (
    PASSWORD,
    UI,
    csrf_of,
    login,
    ui_fixture,  # noqa: F401  (the "ui" fixture)
)

pytestmark = pytest.mark.db

UPLOAD = {"file": ("a.csv", b"Barcode;Name;Price\n", "text/csv")}
MAPPING = {"map_ean": "Barcode", "map_name": "Name", "map_price": "Price"}


def _stored(ui: UI) -> list[object]:
    return list(ui.store.directory.glob("*")) if ui.store.directory.exists() else []


def test_anonymous_upload_is_sent_to_login_unread(ui: UI) -> None:
    # No session at all (never logged in, or the cookie expired).
    r = ui.client.post("/import/preview", data=MAPPING, files=UPLOAD, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    # A session from the login page, with its token, but nobody logged in.
    token = csrf_of(ui.client.get("/login").text)
    r = ui.client.post(
        "/import/preview", data={"csrf_token": token, **MAPPING}, files=UPLOAD,
        follow_redirects=False,
    )  # fmt: skip
    assert r.status_code == 303
    assert _stored(ui) == []


def test_expired_session_form_submit_returns_to_its_page_after_login(ui: UI) -> None:
    """Review of part 1: a form sent after the session expired (or after logging out in
    another tab) goes to the login page, and back to the form's page after login."""
    token = ui.as_staff()
    ui.client.cookies.clear()  # the session cookie expired
    page = {"referer": "http://testserver/bind?ean=2900000000015"}
    r = ui.client.post(
        "/bind", data={"csrf_token": token, "product_id": "1", "tag_id": "1"},
        headers=page, follow_redirects=False,
    )  # fmt: skip
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fbind%3Fean%3D2900000000015"
    # The same from HTMX (every boosted form): HX-Redirect, which HTMX follows itself.
    r = ui.client.post(
        "/bind", data={"csrf_token": token}, headers={**page, "HX-Request": "true"},
        follow_redirects=False,
    )  # fmt: skip
    assert r.status_code == 401
    assert r.headers["hx-redirect"] == "/login?next=%2Fbind%3Fean%3D2900000000015"
    # Logging in returns to the page.
    login_page = ui.client.get(r.headers["hx-redirect"])
    assert 'name="next" value="/bind?ean=2900000000015"' in login_page.text
    r = ui.client.post(
        "/login",
        data={"email": ui.staff.email, "password": PASSWORD, "csrf_token": csrf_of(login_page.text),
              "next": "/bind?ean=2900000000015"},
        follow_redirects=False,
    )  # fmt: skip
    assert r.headers["location"] == "/bind?ean=2900000000015"


def _session_cookie(data: dict[str, object]) -> str:
    """A session cookie as Starlette's ``SessionMiddleware`` signs it."""
    from itsdangerous import TimestampSigner

    from core.config import get_settings

    signer = TimestampSigner(get_settings().session_secret.get_secret_value())
    return signer.sign(b64encode(json.dumps(data).encode())).decode()


def test_logged_in_session_without_csrf_token_is_still_refused(ui: UI) -> None:
    """A logged-in session always has a token; one without cannot come from our pages."""
    ui.client.cookies.set(SESSION_COOKIE, _session_cookie({"uid": ui.staff.id}))
    r = ui.client.post("/logout", follow_redirects=False)
    assert r.status_code == 403
    assert error_message("csrf.invalid") in r.text


def test_login_post_is_still_allowed_without_a_user(ui: UI) -> None:
    login(ui.client, ui.admin.email)
    assert ui.client.get("/products").status_code == 200


def test_oversized_upload_is_refused_by_content_length(ui: UI) -> None:
    token = ui.as_admin()
    big = {"file": ("big.csv", b"x" * (MAX_REQUEST_BYTES + 1), "text/csv")}
    r = ui.client.post("/import/preview", data={"csrf_token": token, **MAPPING}, files=big)
    assert r.status_code == 413
    assert error_message("request.too_large") in r.text
    assert _stored(ui) == []


def test_oversized_streamed_body_is_cut_off(ui: UI) -> None:
    ui.as_admin()
    chunk = b"x" * (1024 * 1024)

    def body() -> Iterator[bytes]:  # no Content-Length: sent chunked
        for _ in range(MAX_REQUEST_BYTES // len(chunk) + 2):
            yield chunk

    r = ui.client.post(
        "/import/preview",
        content=body(),
        headers={"content-type": "multipart/form-data; boundary=zzz"},
    )
    assert r.status_code == 413
    assert error_message("request.too_large") in r.text
    assert _stored(ui) == []


@pytest.mark.parametrize("path", ["/login", "/products", "/static/app.css", "/nope"])
def test_ui_responses_cannot_be_framed(ui: UI, path: str) -> None:
    if path != "/login":
        ui.as_staff()
    r = ui.client.get(path)
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-security-policy"] == "frame-ancestors 'none'"


def test_early_refusals_cannot_be_framed_either(ui: UI) -> None:
    r = ui.client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["x-frame-options"] == "DENY"


def test_gateway_api_is_left_alone(ui: UI) -> None:
    r = ui.client.post("/gateway/v1/heartbeat", json={"version": "1", "driver": "fake"})
    assert r.status_code == 401
    assert "x-frame-options" not in r.headers


def test_own_password_reset_says_so_on_the_login_page(ui: UI) -> None:
    token = ui.as_admin()
    data = {"password": "a brand new password", "password_again": "a brand new password",
            "csrf_token": token}  # fmt: skip
    r = ui.client.post(f"/settings/users/{ui.admin.id}/password", data=data)
    assert r.url.path == "/login"
    assert t("login.password_changed") in r.text
    login(ui.client, ui.admin.email, "a brand new password")
    assert ui.client.get("/settings").status_code == 200

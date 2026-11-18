"""Early refusal of state-changing requests, the body size cap, frame headers, and the
message after an admin resets their own password."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from core.app.i18n import error_message, t
from core.app.web import MAX_REQUEST_BYTES
from tests.integration.test_ui_helpers import (
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


def test_anonymous_upload_is_refused_unread(ui: UI) -> None:
    # No session at all: no CSRF token.
    r = ui.client.post("/import/preview", data=MAPPING, files=UPLOAD)
    assert r.status_code == 403
    assert error_message("csrf.invalid") in r.text
    # A session from the login page, with its token, but nobody logged in.
    token = csrf_of(ui.client.get("/login").text)
    r = ui.client.post("/import/preview", data={"csrf_token": token, **MAPPING}, files=UPLOAD)
    assert r.status_code == 403
    assert error_message("auth.login_required") in r.text
    assert _stored(ui) == []


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
    r = ui.client.post("/logout")
    assert r.status_code == 403
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

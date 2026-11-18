"""Password hashing, the upload store, the Secure-cookie middleware and safe redirects."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from starlette.types import Message, Receive, Scope, Send

from core.app.uploads import UploadStore
from core.app.web import SecureCookieOnHttps, safe_next
from core.services.users import PASSWORD_MAX_LEN, hash_password, verify_password


def test_password_hash_is_salted_scrypt() -> None:
    first, second = hash_password("a long password"), hash_password("a long password")
    assert first != second  # per-user salt
    assert first.startswith("scrypt$16384$8$1$")
    assert "a long password" not in first
    assert verify_password("a long password", first)
    assert verify_password("a long password", second)
    assert not verify_password("a long passworD", first)


@pytest.mark.parametrize("stored", ["", "plain", "bcrypt$1$2$3$aa$bb", "scrypt$x$8$1$aa$bb",
                                    "scrypt$16384$8$1$zz$bb"])  # fmt: skip
def test_bad_stored_hash_never_verifies(stored: str) -> None:
    assert not verify_password("anything", stored)


def test_huge_password_is_refused_without_hashing() -> None:
    assert not verify_password("x" * (PASSWORD_MAX_LEN + 1), hash_password("short one!"))


def test_upload_store_round_trip(tmp_path: Path) -> None:
    store = UploadStore(tmp_path / "up")
    token = store.save(b"EAN;Name\n")
    assert store.load(token) == b"EAN;Name\n"
    assert str(tmp_path) in str(next((tmp_path / "up").iterdir()))
    store.delete(token)
    assert store.load(token) is None
    store.delete(token)  # idempotent


@pytest.mark.parametrize("token", ["../../etc/passwd", "a" * 10, "", "x/" + "a" * 40])
def test_upload_store_refuses_odd_tokens(tmp_path: Path, token: str) -> None:
    store = UploadStore(tmp_path)
    assert store.load(token) is None


def test_upload_store_purges_old_files(tmp_path: Path) -> None:
    store = UploadStore(tmp_path)
    old = store.save(b"old")
    path = next(tmp_path.glob("*.csv"))
    stale = time.time() - 2 * 60 * 60
    os.utime(path, (stale, stale))
    fresh = store.save(b"new")  # saving purges
    assert store.load(old) is None
    assert store.load(fresh) == b"new"


def test_default_upload_dir_is_outside_the_repository() -> None:
    repo = Path(__file__).resolve().parents[2]
    assert repo not in UploadStore().directory.resolve().parents


async def _run(scheme: str, cookies: list[bytes]) -> list[bytes]:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        headers = [(b"set-cookie", c) for c in cookies]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    async def receive() -> Message:
        return {"type": "http.request"}

    scope: Scope = {"type": "http", "scheme": scheme, "headers": []}
    await SecureCookieOnHttps(app)(scope, receive, send)
    return [v for k, v in sent[0]["headers"] if k == b"set-cookie"]


async def test_secure_flag_only_over_https() -> None:
    cookies = [b"esl_session=abc; path=/; httponly; samesite=lax", b"other=1; path=/"]
    assert await _run("http", cookies) == cookies
    secured = await _run("https", cookies)
    assert secured[0].endswith(b"; secure") and secured[1] == b"other=1; path=/"


async def _guard(
    method: str,
    session: dict[str, object],
    chunks: list[bytes],
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, list[tuple[bytes, bytes]], int]:
    """Run ``UiGuard`` (limit 100 bytes) around an app that reads the whole body.
    Returns the status, the response headers and how many body bytes the app read."""
    from fastapi import HTTPException

    from core.app.web import UiGuard

    read = 0

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal read
        try:
            while True:
                message = await receive()
                read += len(message.get("body", b""))
                if not message.get("more_body"):
                    break
            status = 200
        except HTTPException as exc:
            status = exc.status_code
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    pending = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]

    async def receive() -> Message:
        return pending.pop(0)

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "http", "method": method, "path": "/import/preview",
                    "headers": headers or [], "session": session}  # fmt: skip
    await UiGuard(app, max_bytes=100)(scope, receive, send)
    return sent[0]["status"], sent[0]["headers"], read


LOGGED_IN: dict[str, object] = {"csrf": "token", "uid": 1}


async def test_guard_refuses_before_reading_the_body() -> None:
    status, headers, read = await _guard("POST", {}, [b"x" * 50])
    assert (status, read) == (403, 0)  # no CSRF token in the session
    assert (b"x-frame-options", b"DENY") in headers
    status, _, read = await _guard("POST", {"csrf": "token"}, [b"x" * 50])
    assert (status, read) == (403, 0)  # nobody logged in
    status, _, read = await _guard("POST", LOGGED_IN, [b"x"], [(b"content-length", b"101")])
    assert (status, read) == (413, 0)


async def test_guard_cuts_off_a_streamed_body() -> None:
    status, _, read = await _guard("POST", LOGGED_IN, [b"x" * 60, b"x" * 60, b"x" * 60])
    assert status == 413 and read == 60  # stopped at the chunk that went over
    status, _, read = await _guard("POST", LOGGED_IN, [b"x" * 60, b"x" * 40])
    assert (status, read) == (200, 100)


async def test_guard_passes_reads_with_frame_headers() -> None:
    status, headers, _ = await _guard("GET", {}, [b""])
    assert status == 200
    assert (b"content-security-policy", b"frame-ancestors 'none'") in headers


@pytest.mark.parametrize(
    ("target", "expected"),
    [("/tags", "/tags"), ("/products?q=a", "/products?q=a"), ("//evil.example", "/"),
     ("https://evil.example", "/"), ("/\\evil", "/"), ("", "/"), (None, "/")],
)  # fmt: skip
def test_safe_next(target: str | None, expected: str) -> None:
    assert safe_next(target) == expected

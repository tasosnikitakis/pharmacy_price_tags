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


@pytest.mark.parametrize(
    ("target", "expected"),
    [("/tags", "/tags"), ("/products?q=a", "/products?q=a"), ("//evil.example", "/"),
     ("https://evil.example", "/"), ("/\\evil", "/"), ("", "/"), (None, "/")],
)  # fmt: skip
def test_safe_next(target: str | None, expected: str) -> None:
    assert safe_next(target) == expected

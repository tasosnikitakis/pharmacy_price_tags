"""Shared pieces of the admin UI: templates, session login, roles, CSRF and messages.

**Sessions.** Starlette's ``SessionMiddleware`` keeps the session in a cookie signed
with ``SESSION_SECRET`` (``HttpOnly``, ``SameSite=Lax``, and ``Secure`` when the
request came over HTTPS, see ``SecureCookieOnHttps``). The cookie is signed, not
encrypted, so it holds only ids and tokens: the user id, a fingerprint of the
password hash (so a password reset ends the user's other sessions), the CSRF token,
pending flash messages and the pending import upload's token. Never a password.

**Roles** (section 9): ``staff`` can view and, later, bind and audit; ``admin`` can do
everything. Each route declares what it needs with ``CurrentUser`` or ``AdminUser``.
The user row is read on every request, so a deactivated user is logged out at once.

**CSRF.** Every state-changing request (``POST`` and friends) must carry the session's
token, as the ``csrf_token`` form field or, from HTMX, the ``X-CSRF-Token`` header
(set on ``<body hx-headers>``). Checked by ``check_csrf``, a dependency of every UI
router.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.app import formatting, import_messages
from core.app.i18n import LANGUAGE, error_message, t
from core.app.routers.gateway import get_label_pipeline, get_now
from core.config import get_settings
from core.db.models import AppUser
from core.db.session import get_session
from core.services.tags import LabelPipeline
from core.services.users import get_user

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).parent
SESSION_COOKIE = "esl_session"
SESSION_MAX_AGE = 12 * 60 * 60  # a working day; then log in again
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

SessionDep = Annotated[Session, Depends(get_session)]
NowDep = Annotated[datetime, Depends(get_now)]
PipelineDep = Annotated[LabelPipeline, Depends(get_label_pipeline)]


# --- Templates ------------------------------------------------------------------------

templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
_env = templates.env
_env.globals["t"] = t
_env.globals["error_message"] = error_message
_env.globals["LANGUAGE"] = LANGUAGE
_env.filters["money"] = formatting.money
_env.filters["signed_money"] = formatting.signed_money
_env.filters["cents_input"] = formatting.cents_input
_env.filters["date_el"] = formatting.date_el
_env.filters["time_el"] = formatting.time_el
_env.filters["datetime_el"] = formatting.datetime_el
_env.filters["date_input"] = formatting.date_input
_env.filters["athens"] = formatting.local
_env.filters["pack_size"] = lambda p: formatting.pack_size(p.pack_qty_milli, p.pack_unit)
_env.filters["milli_input"] = formatting.milli_input
_env.filters["percent"] = formatting.percent
_env.filters["row_reason"] = import_messages.row_reason
_env.globals["field_value"] = formatting.field_value
_env.trim_blocks = True
_env.lstrip_blocks = True


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    """Render a page with the common context: the user, the CSRF token and the
    flash messages waiting in the session."""
    ctx = dict(context or {})
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx["csrf_token"] = csrf_token(request)
    ctx["flashes"] = pop_flashes(request)
    response = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def redirect(url: str) -> RedirectResponse:
    """303 See Other, so the browser (and HTMX) follows with a GET."""
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


# --- Flash messages -------------------------------------------------------------------


def flash(request: Request, level: str, key: str, **params: object) -> None:
    """Queue a message for the next page. ``params`` must be JSON values."""
    messages = list(request.session.get("flash", []))
    messages.append([level, key, params])
    request.session["flash"] = messages


def flash_error(request: Request, code: str, **params: object) -> None:
    """Queue the message for an error code; the code itself goes to the log."""
    log.info("ui error %s on %s %s", code, request.method, request.url.path)
    flash(request, "error", f"error.{code}", **params)


def pop_flashes(request: Request) -> list[tuple[str, str]]:
    raw = request.session.pop("flash", [])
    return [(level, t(key, **params)) for level, key, params in raw]


# --- CSRF -----------------------------------------------------------------------------


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


class CsrfError(Exception):
    """The request's CSRF token is missing or wrong."""


async def check_csrf(request: Request) -> None:
    """Router dependency: refuse a state-changing request without the session token."""
    if request.method in _SAFE_METHODS:
        return
    expected = request.session.get("csrf")
    sent = request.headers.get(CSRF_HEADER)
    if sent is None:
        form = await request.form()  # cached by Starlette; the endpoint reads it again
        value = form.get(CSRF_FIELD)
        sent = value if isinstance(value, str) else None
    if not isinstance(expected, str) or sent is None or not hmac.compare_digest(sent, expected):
        log.warning("csrf check failed for %s %s", request.method, request.url.path)
        raise CsrfError


# --- Login ----------------------------------------------------------------------------


def _fingerprint(password_hash: str) -> str:
    """Ties a session to the password it was opened with, without storing the hash."""
    key = get_settings().session_secret.get_secret_value().encode()
    return hmac.new(key, password_hash.encode(), hashlib.sha256).hexdigest()[:32]


def log_in(request: Request, user: AppUser) -> None:
    """Start a fresh session for ``user`` (a new CSRF token too)."""
    request.session.clear()
    request.session["uid"] = user.id
    request.session["pwf"] = _fingerprint(user.password_hash)
    request.session["csrf"] = secrets.token_urlsafe(32)


def log_out(request: Request) -> None:
    request.session.clear()


def session_user(request: Request, session: Session) -> AppUser | None:
    """The logged-in, active user, or ``None`` (and the session is cleared)."""
    uid = request.session.get("uid")
    if not isinstance(uid, int):
        return None
    user = get_user(session, uid)
    fingerprint = request.session.get("pwf")
    if (
        user is None
        or not user.active
        or not isinstance(fingerprint, str)
        or not hmac.compare_digest(fingerprint, _fingerprint(user.password_hash))
    ):
        request.session.clear()
        return None
    return user


class LoginRequired(Exception):
    """No logged-in user; the handler redirects to the login page."""


class Forbidden(Exception):
    """The user's role does not allow this."""


def require_user(request: Request, session: SessionDep) -> AppUser:
    user = session_user(request, session)
    if user is None:
        raise LoginRequired
    request.state.user = user
    return user


def require_admin(user: Annotated[AppUser, Depends(require_user)]) -> AppUser:
    if user.role != "admin":
        log.warning("user %s (staff) refused an admin page", user.id)
        raise Forbidden
    return user


CurrentUser = Annotated[AppUser, Depends(require_user)]
AdminUser = Annotated[AppUser, Depends(require_admin)]


def actor(user: AppUser) -> str:
    """``event_log.actor`` for a UI user."""
    return user.email


def safe_next(target: str | None) -> str:
    """A local path to return to after login; anything else goes to the start page."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def login_url(request: Request) -> str:
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"
    return f"/login?next={quote(path, safe='')}"


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def utc_now() -> datetime:
    return datetime.now(UTC)


# --- Secure cookie over HTTPS ---------------------------------------------------------


class SecureCookieOnHttps:
    """ASGI middleware around ``SessionMiddleware``: adds ``Secure`` to the session
    cookie when the request came over HTTPS (behind a TLS proxy, run uvicorn with
    ``--proxy-headers`` so the scheme is known). Plain HTTP during development keeps
    working; the shop box serves HTTPS (section 9)."""

    def __init__(self, app: ASGIApp, cookie_name: str = SESSION_COOKIE) -> None:
        self.app = app
        self.prefix = f"{cookie_name}="

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("scheme") != "https":
            await self.app(scope, receive, send)
            return

        async def send_secure(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                cookies = headers.getlist("set-cookie")
                if any(c.startswith(self.prefix) for c in cookies):
                    del headers["set-cookie"]
                    for cookie in cookies:
                        secure = cookie.startswith(self.prefix)
                        headers.append("set-cookie", f"{cookie}; secure" if secure else cookie)
            await send(message)

        await self.app(scope, receive, send_secure)

"""Login and logout. The start page, ``/``, is the Dashboard (``ui_dashboard``).

Failed logins are throttled per account and per client IP (``core.app.throttle``,
decision 0019). A throttled attempt is refused with 429 before the password is
checked; each throttle event goes to the log and to ``event_log`` as
``auth.throttled``, with the account as a short hash, never the email or password.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from core.app.i18n import t
from core.app.throttle import ThrottleDep, ThrottleEvent, account_hash
from core.app.web import (
    NowDep,
    SessionDep,
    check_csrf,
    log_in,
    log_out,
    redirect,
    render,
    safe_next,
    session_user,
)
from core.db.models import EventLog
from core.services.users import authenticate, normalise_email

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(check_csrf)], include_in_schema=False)


@router.get("/login")
def login_page(
    request: Request, session: SessionDep, next: str = "/", changed: str = ""
) -> Response:
    if session_user(request, session) is not None:
        return redirect(safe_next(next))
    # ?changed=1: the user just reset their own password, which ended their session
    # (and its flash messages), so the page says so itself.
    context = {"next": safe_next(next), "email": "", "password_changed": changed == "1"}
    return render(request, "login.html", context)


@router.post("/login")
def login(
    request: Request,
    session: SessionDep,
    now: NowDep,
    throttle: ThrottleDep,
    email: Annotated[str, Form(max_length=320)] = "",
    password: Annotated[str, Form(max_length=1024)] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    account = normalise_email(email)
    ip = request.client.host if request.client is not None else "unknown"
    refusal = throttle.check(account, ip, now)
    if refusal is not None:
        # Refused without checking the password, so nothing is hashed.
        _record(session, refusal.events, account, ip, now)
        context = {
            "next": safe_next(next),
            "email": email,
            "throttled": retry_message(refusal.retry_after_seconds),
        }
        response = render(
            request, "login.html", context, status_code=status.HTTP_429_TOO_MANY_REQUESTS
        )
        response.headers["Retry-After"] = str(refusal.retry_after_seconds)
        return response
    user = authenticate(session, email, password)
    if user is None:
        log.info("failed login")  # never the email or password
        _record(session, throttle.failure(account, ip, now), account, ip, now)
        context = {"next": safe_next(next), "email": email, "error": "auth.invalid_credentials"}
        return render(request, "login.html", context, status_code=401)
    throttle.success(account)
    log_in(request, user)
    log.info("user %s logged in", user.id)
    return redirect(safe_next(next))


def retry_message(seconds: int) -> str:
    """The Greek "try again in ..." message: seconds under a minute, else whole
    minutes, rounded up."""
    if seconds < 60:
        if seconds == 1:
            return t("login.throttled.second")
        return t("login.throttled.seconds", n=seconds)
    minutes = math.ceil(seconds / 60)
    if minutes == 1:
        return t("login.throttled.minute")
    return t("login.throttled.minutes", n=minutes)


def _record(
    session: Session, events: Iterable[ThrottleEvent], account: str, ip: str, now: datetime
) -> None:
    """Log each throttle event and append it to ``event_log`` (``auth.throttled``).

    The request has no logged-in user, so the actor is ``system``. The payload holds
    the account only as a short hash: never the raw email, never the password."""
    rows = []
    for event in events:
        payload = {
            "event": event.kind,
            "key": event.key_type,
            "failures": event.failures,
            "delay_seconds": event.delay_seconds,
            "ip": ip,
            "account": account_hash(account),
        }
        log.warning(
            "login %s: key=%s failures=%d delay=%ds ip=%s account=%s",
            event.kind,
            event.key_type,
            event.failures,
            event.delay_seconds,
            ip,
            payload["account"],
        )
        rows.append(EventLog(at=now, actor="system", kind="auth.throttled", payload=payload))
    if rows:
        session.add_all(rows)
        session.commit()


@router.post("/logout")
def logout(request: Request) -> Response:
    log_out(request)
    return redirect("/login")

"""Login and logout. The start page, ``/``, is the Dashboard (``ui_dashboard``)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response

from core.app.web import (
    SessionDep,
    check_csrf,
    log_in,
    log_out,
    redirect,
    render,
    safe_next,
    session_user,
)
from core.services.users import authenticate

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
    email: Annotated[str, Form(max_length=320)] = "",
    password: Annotated[str, Form(max_length=1024)] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    user = authenticate(session, email, password)
    if user is None:
        log.info("failed login")  # never the email or password
        context = {"next": safe_next(next), "email": email, "error": "auth.invalid_credentials"}
        return render(request, "login.html", context, status_code=401)
    log_in(request, user)
    log.info("user %s logged in", user.id)
    return redirect(safe_next(next))


@router.post("/logout")
def logout(request: Request) -> Response:
    log_out(request)
    return redirect("/login")

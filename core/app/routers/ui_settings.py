"""Settings page (section 9: users, gateway token, staleness threshold). Admin only.

Users are managed through ``core.services.users``. The gateway token and all
thresholds come from the environment (``core.config``) and are shown read-only; the
token itself is never shown, only that it is configured.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response

from core.app.i18n import N_
from core.app.web import (
    AdminUser,
    NowDep,
    SessionDep,
    actor,
    check_csrf,
    flash,
    flash_error,
    redirect,
    render,
)
from core.config import get_settings
from core.db.models import ROLES
from core.services import admin_queries
from core.services.catalogue import last_applied_import_at
from core.services.users import (
    UserError,
    create_user,
    list_users,
    reset_password,
    set_active,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", dependencies=[Depends(check_csrf)], include_in_schema=False)


def _page(
    request: Request,
    session: SessionDep,
    *,
    error: str | None = None,
    values: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    settings = get_settings()
    context = {
        "users": list_users(session),
        "roles": ROLES,
        "gateways": admin_queries.gateways(session),
        "token_configured": bool(settings.gateway_token.get_secret_value()),
        "stale_import_days": settings.stale_import_days,
        "last_import_at": last_applied_import_at(session),
        "warn_percent": settings.price_jump_warn_percent,
        "banner_percent": settings.price_jump_banner_percent,
        "timezone": settings.display_timezone,
        "error": error,
        "values": values or {},
    }
    return render(request, "settings/index.html", context, status_code=status_code)


@router.get("")
def settings_page(request: Request, session: SessionDep, user: AdminUser) -> Response:
    return _page(request, session)


@router.post("/users")
def add_user(
    request: Request,
    session: SessionDep,
    user: AdminUser,
    now: NowDep,
    email: Annotated[str, Form(max_length=320)] = "",
    role: Annotated[str, Form(max_length=16)] = "staff",
    password: Annotated[str, Form(max_length=1024)] = "",
    password_again: Annotated[str, Form(max_length=1024)] = "",
) -> Response:
    values = {"email": email, "role": role}
    try:
        if password != password_again:
            raise UserError("user.password_mismatch")
        added = create_user(session, email, password, role, actor=actor(user), now=now)
    except UserError as exc:
        return _page(request, session, error=exc.code, values=values, status_code=422)
    session.commit()
    log.info("user %s added user %s", user.id, added.id)
    flash(request, "success", N_("settings.users.added"), email=added.email)
    return redirect("/settings")


@router.post("/users/{user_id}/active")
def change_active(
    request: Request,
    user_id: int,
    session: SessionDep,
    user: AdminUser,
    now: NowDep,
    active: Annotated[bool, Form()] = False,
) -> Response:
    if user_id == user.id and not active:
        flash_error(request, "user.self_deactivate")
        return redirect("/settings")
    try:
        changed = set_active(session, user_id, active, actor=actor(user), now=now)
    except UserError as exc:
        flash_error(request, exc.code)
        return redirect("/settings")
    session.commit()
    if changed.active:
        flash(request, "success", N_("settings.users.activated"), email=changed.email)
    else:
        flash(request, "success", N_("settings.users.deactivated"), email=changed.email)
    return redirect("/settings")


@router.post("/users/{user_id}/password")
def change_password(
    request: Request,
    user_id: int,
    session: SessionDep,
    user: AdminUser,
    now: NowDep,
    password: Annotated[str, Form(max_length=1024)] = "",
    password_again: Annotated[str, Form(max_length=1024)] = "",
) -> Response:
    try:
        if password != password_again:
            raise UserError("user.password_mismatch")
        changed = reset_password(session, user_id, password, actor=actor(user), now=now)
    except UserError as exc:
        flash_error(request, exc.code)
        return redirect("/settings")
    session.commit()
    flash(request, "success", N_("settings.users.password_reset"), email=changed.email)
    if changed.id == user.id:
        # The session is tied to the old password; the admin logs in again.
        return redirect("/login")
    return redirect("/settings")

"""FastAPI application for the core.

There is no module-level ``app``: run it with ``uvicorn --factory
core.app.main:create_app``. Importing this module then needs no configuration, so tests
and tools can import it on any machine; settings are checked when the app is created.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from core.app.routers import gateway as gateway_router
from core.config import get_settings
from core.db.session import get_session
from shared.gateway_api import API_PREFIX

VERSION = "0.1.0"


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["ok", "unavailable"]
    version: str


def create_app() -> FastAPI:
    # Fail fast: a missing or weak secret stops the process before it serves anything.
    settings = get_settings()
    app = FastAPI(title="Pharmacy ESL core", version=VERSION)
    app.include_router(gateway_router.router)

    @app.get("/health", response_model=Health)
    def health(response: Response, session: Annotated[Session, Depends(get_session)]) -> Health:
        try:
            session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return Health(status="degraded", database="unavailable", version=VERSION)
        return Health(status="ok", database="ok", version=VERSION)

    _add_admin_ui(app, settings.session_secret.get_secret_value())
    return app


def _add_admin_ui(app: FastAPI, session_secret: str) -> None:
    """The admin UI (section 9): its routers, static files, session and error pages."""
    from core.app import web
    from core.app.routers import ui_auth, ui_import, ui_products, ui_settings, ui_tags

    for module in (ui_auth, ui_products, ui_import, ui_tags, ui_settings):
        app.include_router(module.router)
    app.mount("/static", StaticFiles(directory=web.APP_DIR / "static"), name="static")
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie=web.SESSION_COOKIE,
        max_age=web.SESSION_MAX_AGE,
        same_site="lax",
        https_only=False,  # Secure is added per request over HTTPS, outside this one
    )
    app.add_middleware(web.SecureCookieOnHttps)

    def _error_page(request: Request, code: str, status_code: int) -> Response:
        web.log.info("ui error %s on %s %s", code, request.method, request.url.path)
        return web.render(request, "error.html", {"code": code}, status_code=status_code)

    @app.exception_handler(web.LoginRequired)
    async def login_required(request: Request, exc: web.LoginRequired) -> Response:
        target = web.login_url(request)
        if request.headers.get("HX-Request"):
            return Response(
                status_code=status.HTTP_401_UNAUTHORIZED, headers={"HX-Redirect": target}
            )
        return web.redirect(target)

    @app.exception_handler(web.Forbidden)
    async def forbidden(request: Request, exc: web.Forbidden) -> Response:
        return _error_page(request, "auth.forbidden", status.HTTP_403_FORBIDDEN)

    @app.exception_handler(web.CsrfError)
    async def csrf_failed(request: Request, exc: web.CsrfError) -> Response:
        return _error_page(request, "csrf.invalid", status.HTTP_403_FORBIDDEN)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        path = request.url.path
        ui = not (path.startswith(API_PREFIX) or path == "/health" or path.startswith("/static"))
        if ui and exc.status_code == status.HTTP_404_NOT_FOUND:
            return _error_page(request, "not_found", exc.status_code)
        return await http_exception_handler(request, exc)

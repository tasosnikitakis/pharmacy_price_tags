"""FastAPI application for the core.

There is no module-level ``app``: run it with ``uvicorn --factory
core.app.main:create_app``. Importing this module then needs no configuration, so tests
and tools can import it on any machine; settings are checked when the app is created.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from core.app.routers import gateway as gateway_router
from core.config import get_settings
from core.db.session import get_session

VERSION = "0.1.0"


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["ok", "unavailable"]
    version: str


def create_app() -> FastAPI:
    # Fail fast: a missing or weak secret stops the process before it serves anything.
    get_settings()
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

    return app

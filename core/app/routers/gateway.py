"""Gateway API (PLAN.md section 3), under ``shared.gateway_api.API_PREFIX``.

Every endpoint needs ``Authorization: Bearer <GATEWAY_TOKEN>``; a missing or wrong
token gets 401. Each request is one transaction with one UTC ``now`` (``get_now``).
The core never calls the gateway; the gateway dials in here.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import get_settings
from core.db.models import RenderRow
from core.db.session import get_session, get_sessionmaker
from core.domain.reconciler import StaleResult
from core.services.gateway_auth import authenticate, ensure_gateway
from core.services.queue import JobNotFound, claim_jobs, report_result
from core.services.tags import (
    LabelPipeline,
    real_label_pipeline,
    record_heartbeat,
    record_seen_tags,
)
from shared.gateway_api import (
    API_PREFIX,
    RENDER_MEDIA_TYPE,
    ClaimRequest,
    ClaimResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    JobResultRequest,
    JobResultResponse,
    TagsSeenRequest,
    TagsSeenResponse,
)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """On startup, make the configured ``GATEWAY_TOKEN`` the v1 gateway's token."""
    token = get_settings().gateway_token.get_secret_value()
    with get_sessionmaker()() as session:
        ensure_gateway(session, token)
        session.commit()
    yield


router = APIRouter(prefix=API_PREFIX, tags=["gateway"], lifespan=lifespan)

_bearer = HTTPBearer(auto_error=False)


def get_now() -> datetime:
    """The request's single clock reading. Tests override it to control time."""
    return datetime.now(UTC)


def get_label_pipeline() -> LabelPipeline:
    """The label pipeline. Tests override it with a fake renderer."""
    return real_label_pipeline


SessionDep = Annotated[Session, Depends(get_session)]
NowDep = Annotated[datetime, Depends(get_now)]


def require_gateway(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> int:
    """The calling gateway's id; 401 without a valid bearer token."""
    gateway_id = None
    if credentials is not None and credentials.scheme.lower() == "bearer":
        gateway_id = authenticate(session, credentials.credentials)
    if gateway_id is None:
        log.warning("gateway request with a missing or invalid token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid gateway token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return gateway_id


GatewayDep = Annotated[int, Depends(require_gateway)]


@router.post("/jobs/claim", response_model=ClaimResponse)
def claim(
    body: ClaimRequest, gateway_id: GatewayDep, session: SessionDep, now: NowDep
) -> ClaimResponse:
    jobs = claim_jobs(session, gateway_id, body.max_jobs, now)
    session.commit()
    return ClaimResponse(jobs=jobs)


@router.get("/renders/{render_hash}", response_class=Response)
def get_render(
    render_hash: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")],
    _: GatewayDep,
    session: SessionDep,
) -> Response:
    png = session.scalar(select(RenderRow.png).where(RenderRow.hash == render_hash))
    if png is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown render")
    return Response(
        content=png,
        media_type=RENDER_MEDIA_TYPE,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.post(
    "/jobs/{job_id}/result",
    response_model=JobResultResponse,
    responses={404: {"description": "Unknown job"}, 409: {"description": "Stale result"}},
)
def job_result(
    job_id: int, body: JobResultRequest, gateway_id: GatewayDep, session: SessionDep, now: NowDep
) -> JobResultResponse:
    try:
        applied = report_result(
            session,
            gateway_id,
            job_id,
            body.attempt,
            body.outcome,
            body.error_code,
            now,
            error=body.error_detail,
        )
    except JobNotFound:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown job") from None
    except StaleResult as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    session.commit()
    return JobResultResponse(job_id=applied.job_id, state=applied.state.value)


@router.post("/tags/seen", response_model=TagsSeenResponse)
def tags_seen(
    body: TagsSeenRequest,
    _: GatewayDep,
    session: SessionDep,
    now: NowDep,
    pipeline: Annotated[LabelPipeline, Depends(get_label_pipeline)],
) -> TagsSeenResponse:
    response = record_seen_tags(session, body.tags, pipeline, now)
    session.commit()
    return response


@router.post("/heartbeat", response_model=HeartbeatResponse)
def heartbeat(
    body: HeartbeatRequest, gateway_id: GatewayDep, session: SessionDep, now: NowDep
) -> HeartbeatResponse:
    record_heartbeat(session, gateway_id, body.version, body.driver, now)
    session.commit()
    return HeartbeatResponse(server_time=now)

"""Tags page (section 9): every tag with its product, last seen, RSSI, battery and
displayed versus desired label; retry of a dead job and unbind (admin only)."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from core.app.i18n import N_
from core.app.previews import enlarge
from core.app.web import (
    AdminUser,
    CurrentUser,
    NowDep,
    PipelineDep,
    SessionDep,
    actor,
    check_csrf,
    flash,
    flash_error,
    not_found,
    redirect,
    render,
)
from core.services import admin_queries
from core.services.queue import JobNotFound, retry_dead_job
from core.services.render_store import load_png
from core.services.tags import BindingError, unbind_tag

log = logging.getLogger(__name__)

router = APIRouter(prefix="/tags", dependencies=[Depends(check_csrf)], include_in_schema=False)


@router.get("")
def tag_list(
    request: Request, session: SessionDep, user: CurrentUser, status: str = ""
) -> Response:
    rows = admin_queries.tag_rows(session)
    counts = {s.value: 0 for s in admin_queries.TagStatus}
    for row in rows:
        counts[row.status.value] += 1
    if status in counts:
        rows = [row for row in rows if row.status.value == status]
    else:
        status = ""
    context = {"rows": rows, "counts": counts, "status": status}
    return render(request, "tags/list.html", context)


@router.get("/{tag_id}")
def tag_detail(request: Request, tag_id: int, session: SessionDep, user: CurrentUser) -> Response:
    row = admin_queries.tag_row(session, tag_id)
    if row is None:
        raise not_found()
    jobs = admin_queries.tag_jobs(session, tag_id)
    return render(request, "tags/detail.html", {"row": row, "jobs": jobs})


@router.get("/{tag_id}/label/{which}.png")
def tag_label(
    tag_id: int,
    which: Literal["desired", "displayed"],
    session: SessionDep,
    user: CurrentUser,
) -> Response:
    """The tag's desired or displayed label from the render store, enlarged."""
    row = admin_queries.tag_row(session, tag_id)
    digest = None
    if row is not None:
        digest = row.tag.desired_hash if which == "desired" else row.tag.displayed_hash
    png = load_png(session, digest) if digest else None
    if png is None:
        raise not_found()
    # Renders are content-addressed and the page links them with ?v=<hash>.
    return Response(enlarge(png), media_type="image/png", headers={"Cache-Control": "no-cache"})


@router.post("/{tag_id}/unbind")
def unbind(
    request: Request,
    tag_id: int,
    session: SessionDep,
    user: AdminUser,
    now: NowDep,
    pipeline: PipelineDep,
) -> Response:
    try:
        result = unbind_tag(session, tag_id, actor=actor(user), now=now, pipeline=pipeline)
    except BindingError:
        raise not_found() from None
    session.commit()
    if result is None:
        flash_error(request, "tag.not_bound")
    else:
        flash(request, "success", N_("tags.unbound_done"))
    return redirect(f"/tags/{tag_id}")


@router.post("/{tag_id}/jobs/{job_id}/retry")
def retry(
    request: Request,
    tag_id: int,
    job_id: int,
    session: SessionDep,
    user: AdminUser,
    now: NowDep,
) -> Response:
    row = admin_queries.tag_row(session, tag_id)
    if row is None or row.dead_job_id != job_id:
        flash_error(request, "job.not_retryable")
        return redirect(f"/tags/{tag_id}")
    try:
        actions = retry_dead_job(session, job_id, actor(user), now)
    except JobNotFound:
        raise not_found() from None
    session.commit()
    if actions:
        flash(request, "success", N_("tags.retried"))
    else:
        flash_error(request, "job.not_retryable")
    return redirect(f"/tags/{tag_id}")

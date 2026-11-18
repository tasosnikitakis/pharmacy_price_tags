"""Update-job queue on the database: reconcile, claim, results and admin retry.

Each function loads snapshots, calls the pure rules in ``core.domain.reconciler`` and
writes every field of the returned transitions, all in the caller's transaction. The
caller passes one UTC ``now`` per transaction and commits.

Locking (so concurrent passes cannot deadlock or cancel a job being pushed):

- Lock order is always ``tag`` rows first, then their ``update_job`` rows. Tags are
  locked ``FOR NO KEY UPDATE`` in id order, which does not block foreign-key checks
  from new jobs.
- ``reconcile`` and ``report_result`` wait for locks. Claiming never waits: every lock
  it takes is ``SKIP LOCKED``, so two gateways (or two claim requests) never get the
  same job and a claim is never part of a lock cycle.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from core.db.models import EventLog, Tag, TagTypeRow, UpdateJob
from core.domain.reconciler import (
    ACTIVE_STATES,
    CLAIM_TIMEOUT,
    Action,
    CancelJob,
    CreateJob,
    JobSnapshot,
    JobState,
    JobTransition,
    StaleResult,
    TagSnapshot,
    admin_retry,
    apply_result,
    claim,
    due_retries,
    plan_updates,
    reclaim_stale,
)
from shared.gateway_api import ClaimedJob, JobOutcome, PushErrorCode
from shared.tag_types import Palette, TagType

_ACTIVE = tuple(s.value for s in ACTIVE_STATES)


class JobNotFound(LookupError):
    """No update job with that id. The API answers 404."""


def job_snapshot(row: UpdateJob) -> JobSnapshot:
    return JobSnapshot(
        job_id=row.id,
        tag_id=row.tag_id,
        render_hash=row.render_hash,
        state=JobState(row.state),
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        claimed_at=row.claimed_at,
        created_at=row.created_at,
        gateway_id=row.gateway_id,
        error_code=PushErrorCode(row.error_code) if row.error_code is not None else None,
        error=row.error,
    )


def tag_snapshot(row: Tag) -> TagSnapshot:
    return TagSnapshot(
        tag_id=row.id,
        desired_hash=row.desired_hash,
        displayed_hash=row.displayed_hash,
        desired_hash_changed_at=row.desired_hash_changed_at,
    )


def tag_type_of(row: TagTypeRow) -> TagType:
    return TagType(
        key=row.key,
        vendor=row.vendor,
        model=row.model,
        width=row.width,
        height=row.height,
        palette=Palette(row.palette),
    )


def _write(job: UpdateJob, t: JobTransition, now: datetime) -> None:
    """Write every field of ``t``; ``None`` means NULL (see ``JobTransition``)."""
    assert job.id == t.job_id
    job.state = t.state.value
    job.attempts = t.attempts
    job.next_attempt_at = t.next_attempt_at
    job.claimed_at = t.claimed_at
    job.gateway_id = t.gateway_id
    job.error_code = t.error_code.value if t.error_code is not None else None
    job.error = t.error
    job.updated_at = now


def _apply_tag_effect(tag: Tag, t: JobTransition) -> None:
    if t.set_displayed_hash is not None:
        tag.displayed_hash = t.set_displayed_hash
    elif t.displayed_hash_unknown:
        tag.displayed_hash = None


def _lock_tags(
    session: Session, tag_ids: Collection[int] | None, *, skip_locked: bool
) -> list[Tag]:
    query = select(Tag).order_by(Tag.id).with_for_update(key_share=True, skip_locked=skip_locked)
    if tag_ids is not None:
        if not tag_ids:
            return []
        query = query.where(Tag.id.in_(tag_ids))
    return list(session.scalars(query))


def reconcile(
    session: Session, now: datetime, tag_ids: Collection[int] | None = None
) -> list[Action]:
    """Bring the queue in line with ``desired_hash`` for ``tag_ids`` (all tags if ``None``).

    Cancels outdated queued or failed jobs and creates new ones (``plan_updates``).
    """
    tags = _lock_tags(session, tag_ids, skip_locked=False)
    if not tags:
        return []
    ids = [t.id for t in tags]
    active = {
        row.id: row
        for row in session.scalars(
            select(UpdateJob)
            .where(UpdateJob.tag_id.in_(ids), UpdateJob.state.in_(_ACTIVE))
            .order_by(UpdateJob.id)
            .with_for_update()
        )
    }
    latest_dead = session.scalars(
        select(UpdateJob)
        .where(UpdateJob.tag_id.in_(ids), UpdateJob.state == JobState.DEAD.value)
        .distinct(UpdateJob.tag_id)
        .order_by(UpdateJob.tag_id, UpdateJob.created_at.desc(), UpdateJob.id.desc())
    ).all()
    actions = plan_updates(
        [tag_snapshot(t) for t in tags],
        [job_snapshot(j) for j in active.values()],
        [job_snapshot(j) for j in latest_dead],
    )
    _apply_actions(session, actions, active, now)
    return actions


def _apply_actions(
    session: Session, actions: Sequence[Action], active: dict[int, UpdateJob], now: datetime
) -> None:
    for action in actions:
        if isinstance(action, CancelJob):
            job = active[action.job_id]
            job.state = JobState.CANCELLED.value
            job.updated_at = now
    # Cancels must reach the database before the inserts: one active job per tag.
    session.flush()
    for action in actions:
        if isinstance(action, CreateJob):
            session.add(_new_job(action, now))
    session.flush()


def _new_job(action: CreateJob, now: datetime) -> UpdateJob:
    return UpdateJob(
        tag_id=action.tag_id,
        render_hash=action.render_hash,
        state=JobState.QUEUED.value,
        attempts=0,
        created_at=now,
        updated_at=now,
    )


def _reclaim_stale_jobs(session: Session, now: datetime) -> None:
    stale_tag_ids = list(
        session.scalars(
            select(UpdateJob.tag_id).where(
                UpdateJob.state == JobState.CLAIMED.value,
                UpdateJob.claimed_at <= now - CLAIM_TIMEOUT,
            )
        )
    )
    tags = {t.id: t for t in _lock_tags(session, stale_tag_ids, skip_locked=True)}
    if not tags:
        return
    jobs = {
        j.id: j
        for j in session.scalars(
            select(UpdateJob)
            .where(UpdateJob.tag_id.in_(tags), UpdateJob.state == JobState.CLAIMED.value)
            .with_for_update(skip_locked=True)
        )
    }
    transitions = reclaim_stale([job_snapshot(j) for j in jobs.values()], now)
    for t in transitions:
        job = jobs[t.job_id]
        _write(job, t, now)
        _apply_tag_effect(tags[job.tag_id], t)
    session.flush()
    # The label may have changed while the job was claimed: requeue the current one.
    # Every tag and active job here is already locked by this transaction.
    reconcile(session, now, [jobs[t.job_id].tag_id for t in transitions])


def _due_retry_jobs(session: Session, now: datetime) -> None:
    jobs = {
        j.id: j
        for j in session.scalars(
            select(UpdateJob)
            .where(UpdateJob.state == JobState.FAILED.value, UpdateJob.next_attempt_at <= now)
            .with_for_update(skip_locked=True)
        )
    }
    for t in due_retries([job_snapshot(j) for j in jobs.values()], now):
        _write(jobs[t.job_id], t, now)
    session.flush()


def claim_jobs(session: Session, gateway_id: int, max_jobs: int, now: datetime) -> list[ClaimedJob]:
    """Hand up to ``max_jobs`` due ``queued`` jobs to ``gateway_id``.

    First returns stale claims to the queue and moves due retries back to ``queued``,
    in the same transaction. Rows locked by another transaction are skipped.
    """
    _reclaim_stale_jobs(session, now)
    _due_retry_jobs(session, now)
    rows = list(
        session.scalars(
            select(UpdateJob)
            .where(
                UpdateJob.state == JobState.QUEUED.value,
                or_(UpdateJob.next_attempt_at.is_(None), UpdateJob.next_attempt_at <= now),
            )
            .order_by(UpdateJob.created_at, UpdateJob.id)
            .limit(max_jobs)
            .with_for_update(skip_locked=True)
        )
    )
    if not rows:
        return []
    targets = {
        tag.id: (tag.vendor_id, tag_type_of(tag_type))
        for tag, tag_type in session.execute(
            select(Tag, TagTypeRow)
            .join(TagTypeRow, Tag.tag_type_id == TagTypeRow.id)
            .where(Tag.id.in_([r.tag_id for r in rows]))
        ).tuples()
    }
    claimed = []
    for row in rows:
        t = claim(job_snapshot(row), gateway_id, now)
        _write(row, t, now)
        vendor_id, tag_type = targets[row.tag_id]
        claimed.append(
            ClaimedJob(
                job_id=row.id,
                vendor_id=vendor_id,
                tag_type=tag_type,
                render_hash=row.render_hash,
                attempt=t.attempts,
            )
        )
    session.flush()
    return claimed


@dataclass(frozen=True, slots=True)
class ResultApplied:
    job_id: int
    state: JobState


def report_result(
    session: Session,
    gateway_id: int,
    job_id: int,
    attempt: int,
    outcome: JobOutcome,
    error_code: PushErrorCode | None,
    now: datetime,
    error: str | None = None,
) -> ResultApplied:
    """Apply a gateway's result for claim ``attempt`` of ``job_id``.

    Raises ``JobNotFound``, or ``StaleResult`` (changing nothing) for a late or
    duplicate result, or one from a gateway that does not hold the claim.
    """
    tag_id = session.scalar(select(UpdateJob.tag_id).where(UpdateJob.id == job_id))
    if tag_id is None:
        raise JobNotFound(job_id)
    [tag] = _lock_tags(session, [tag_id], skip_locked=False)
    job = session.scalars(select(UpdateJob).where(UpdateJob.id == job_id).with_for_update()).one()
    snapshot = job_snapshot(job)
    if snapshot.state is JobState.CLAIMED and snapshot.gateway_id not in (None, gateway_id):
        raise StaleResult(f"job {job_id} is claimed by another gateway")
    t = apply_result(snapshot, attempt, outcome, error_code, now, error=error)
    _write(job, t, now)
    _apply_tag_effect(tag, t)
    session.flush()
    # An ack may reveal a newer label waiting; a failure may need replacing.
    reconcile(session, now, [tag_id])
    return ResultApplied(job_id, t.state)


def retry_dead_job(session: Session, job_id: int, actor: str, now: datetime) -> list[Action]:
    """An admin's retry of a dead job (Tags page): a fresh job if the tag still needs one.

    Logs ``job.retry`` in ``event_log`` when a job is created.
    """
    tag_id = session.scalar(select(UpdateJob.tag_id).where(UpdateJob.id == job_id))
    if tag_id is None:
        raise JobNotFound(job_id)
    [tag] = _lock_tags(session, [tag_id], skip_locked=False)
    dead = session.get_one(UpdateJob, job_id)
    active = session.scalars(
        select(UpdateJob)
        .where(UpdateJob.tag_id == tag_id, UpdateJob.state.in_(_ACTIVE))
        .with_for_update()
    ).first()
    actions = admin_retry(
        tag_snapshot(tag),
        job_snapshot(dead),
        job_snapshot(active) if active is not None else None,
        now,
    )
    _apply_actions(session, actions, {}, now)
    if actions:
        session.add(
            EventLog(
                at=now,
                actor=actor,
                kind="job.retry",
                payload={"job_id": job_id, "tag_id": tag_id, "render_hash": tag.desired_hash},
            )
        )
        session.flush()
    return actions

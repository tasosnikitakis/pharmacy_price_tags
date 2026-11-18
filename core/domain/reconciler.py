"""Desired-state reconciler and update-job state machine (PLAN.md section 5).

Owned by workstream C. Pure functions over snapshots; the caller loads the rows,
applies the returned actions and transitions in one transaction, and commits.

States::

    queued -> claimed -> acked
                 |
                 +-> failed -> queued   (after the backoff delay)
                          |
                          +-> dead      (after MAX_ATTEMPTS attempts)
    queued | failed -> cancelled        (replaced by a newer desired_hash)

Rules:

- ``attempts`` is incremented when a job is claimed, so it counts pushes started.
- A failure with ``attempts < MAX_ATTEMPTS`` moves to ``failed`` with
  ``next_attempt_at = now + BACKOFF[attempts - 1]``; at ``MAX_ATTEMPTS`` it is ``dead``.
- A ``claimed`` job is never cancelled: the gateway may be pushing it. A newer
  ``desired_hash`` waits for its result.
- On ``acked`` the tag's ``displayed_hash`` becomes the job's ``render_hash`` (not the
  tag's current ``desired_hash``), so a newer label is queued on the next pass.
- A claim with no result after ``CLAIM_TIMEOUT`` returns to ``queued``, or to ``dead``
  if it had already used ``MAX_ATTEMPTS``. The lost push counts as a used attempt.
- At most one job per tag is active (``queued``, ``claimed`` or ``failed``); the
  database enforces this with a partial unique index.
- The caller sets ``tag.desired_hash_changed_at`` whenever it changes
  ``tag.desired_hash``; ``plan_updates`` uses it to decide whether a dead job is stale.
- One clock: every timestamp these rules compare (``created_at`` and the job times,
  ``desired_hash_changed_at``) is the ``now`` the caller passes in, a timezone-aware
  UTC ``datetime`` taken once per transaction in Python, and the caller writes it
  explicitly. Database ``now()`` defaults are only a fallback for audit columns.
- A result is matched to a claim by ``attempt``: a late result from an earlier claim
  of the same job raises ``StaleResult`` and changes nothing.
- Unknown outcome: e-paper can update even when its confirmation is lost. When a push
  may have landed but was not confirmed (a stale claim, or a ``timeout`` or
  ``protocol`` failure), the transition sets ``displayed_hash_unknown`` and the caller
  sets the tag's ``displayed_hash`` to NULL. The next ``plan_updates`` pass then
  pushes the current ``desired_hash`` even if it equals the old displayed one, so a
  tag never keeps an outdated price unnoticed. ``not_found`` and ``rejected`` mean the
  tag did not take the image; ``displayed_hash`` is kept.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from shared.gateway_api import JobOutcome, PushErrorCode

BACKOFF: tuple[timedelta, ...] = tuple(timedelta(minutes=m) for m in (1, 5, 15, 60, 240))
MAX_ATTEMPTS = 6  # 1 initial push + 5 retries, one per BACKOFF step
CLAIM_TIMEOUT = timedelta(minutes=5)

#: ``error`` written when a claim times out without a result.
STALE_CLAIM_ERROR = "no result within the claim timeout; the push may have landed"

#: Failure codes after which the tag may show the new image anyway.
_MAY_HAVE_LANDED = frozenset({PushErrorCode.TIMEOUT, PushErrorCode.PROTOCOL})


class JobState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    ACKED = "acked"
    FAILED = "failed"
    DEAD = "dead"
    CANCELLED = "cancelled"


ACTIVE_STATES: frozenset[JobState] = frozenset({JobState.QUEUED, JobState.CLAIMED, JobState.FAILED})


@dataclass(frozen=True, slots=True)
class TagSnapshot:
    tag_id: int
    desired_hash: str | None
    displayed_hash: str | None
    desired_hash_changed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    job_id: int
    tag_id: int
    render_hash: str
    state: JobState
    attempts: int
    next_attempt_at: datetime | None
    claimed_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CreateJob:
    tag_id: int
    render_hash: str


@dataclass(frozen=True, slots=True)
class CancelJob:
    job_id: int


Action = CreateJob | CancelJob


class StaleResult(ValueError):
    """A result for a claim that is no longer current. The API answers 409."""


@dataclass(frozen=True, slots=True)
class JobTransition:
    """The complete new values of a job's mutable fields after one event.

    The caller writes every field to the ``update_job`` row, including ``None``
    values: ``None`` means "set to NULL", never "unchanged". For the tag: if
    ``set_displayed_hash`` is not ``None`` the caller sets ``displayed_hash`` to it; if
    ``displayed_hash_unknown`` is true it sets ``displayed_hash`` to NULL. Never both.
    """

    job_id: int
    state: JobState
    attempts: int
    next_attempt_at: datetime | None
    claimed_at: datetime | None
    gateway_id: int | None
    error_code: PushErrorCode | None
    error: str | None
    set_displayed_hash: str | None = None
    displayed_hash_unknown: bool = False

    def __post_init__(self) -> None:
        if self.set_displayed_hash is not None and self.displayed_hash_unknown:
            raise ValueError("a transition cannot both set and clear displayed_hash")


def plan_updates(
    tags: Sequence[TagSnapshot],
    active_jobs: Sequence[JobSnapshot],
    latest_dead: Sequence[JobSnapshot] = (),
) -> list[Action]:
    """Actions that bring every tag's queue in line with its ``desired_hash``.

    Per tag, with at most one active job:

    - ``desired_hash`` is ``None`` or equals ``displayed_hash``: cancel a queued or
      failed job; leave a claimed one alone.
    - Otherwise, no active job: create one for ``desired_hash``, unless suppressed by
      a dead job (below).
    - Active job for ``desired_hash`` already: nothing.
    - Queued or failed job for another hash: cancel it and create a new one.
    - Claimed job for another hash: nothing; wait for its result.

    Dead-job suppression: if the tag's most recent ``dead`` job (in ``latest_dead``)
    is for ``desired_hash`` and was created at or after ``desired_hash_changed_at``
    (or ``desired_hash_changed_at`` is ``None``: unchanged since before any job),
    nothing is created. It stays visible in monitoring until an admin retries it (``admin_retry``).
    If ``desired_hash`` changed after that job was created (even back to the same
    hash), the dead job is stale and a new job is created.

    ``active_jobs`` must only contain jobs in ``ACTIVE_STATES``; ``latest_dead`` at
    most one ``dead`` job per tag. Every cancel comes before the create for the same
    tag in the returned list.
    """
    active_by_tag: dict[int, JobSnapshot] = {}
    for job in active_jobs:
        if job.state not in ACTIVE_STATES:
            raise ValueError(f"job {job.job_id} is {job.state}, not active")
        if job.tag_id in active_by_tag:
            raise ValueError(f"tag {job.tag_id} has more than one active job")
        active_by_tag[job.tag_id] = job
    dead_by_tag: dict[int, JobSnapshot] = {}
    for dead in latest_dead:
        if dead.state is not JobState.DEAD:
            raise ValueError(f"job {dead.job_id} in latest_dead is {dead.state}")
        if dead.tag_id in dead_by_tag:
            raise ValueError(f"tag {dead.tag_id} has more than one latest dead job")
        dead_by_tag[dead.tag_id] = dead

    actions: list[Action] = []
    for tag in tags:
        active = active_by_tag.get(tag.tag_id)
        desired = tag.desired_hash
        if desired is None or desired == tag.displayed_hash:
            if active is not None and active.state is not JobState.CLAIMED:
                actions.append(CancelJob(active.job_id))
            continue
        if active is None:
            if not _suppressed_by_dead(tag, desired, dead_by_tag.get(tag.tag_id)):
                actions.append(CreateJob(tag.tag_id, desired))
        elif active.render_hash == desired or active.state is JobState.CLAIMED:
            continue
        else:
            actions.append(CancelJob(active.job_id))
            actions.append(CreateJob(tag.tag_id, desired))
    return actions


def _suppressed_by_dead(tag: TagSnapshot, desired: str, dead: JobSnapshot | None) -> bool:
    """True while the tag's latest dead job is for ``desired`` and not stale."""
    if dead is None or dead.render_hash != desired:
        return False
    changed_at = tag.desired_hash_changed_at
    return changed_at is None or dead.created_at >= changed_at


def claim(job: JobSnapshot, gateway_id: int, now: datetime) -> JobTransition:
    """A job handed to a gateway: ``claimed``, ``attempts + 1``, ``claimed_at = now``,
    ``gateway_id`` set, ``next_attempt_at``, ``error_code`` and ``error`` cleared.

    Raises ``ValueError`` unless the job is ``queued``.
    """
    if job.state is not JobState.QUEUED:
        raise ValueError(f"job {job.job_id} is {job.state}, only queued jobs can be claimed")
    return JobTransition(
        job_id=job.job_id,
        state=JobState.CLAIMED,
        attempts=job.attempts + 1,
        next_attempt_at=None,
        claimed_at=now,
        gateway_id=gateway_id,
        error_code=None,
        error=None,
    )


def apply_result(
    job: JobSnapshot,
    attempt: int,
    outcome: JobOutcome,
    error_code: PushErrorCode | None,
    now: datetime,
    error: str | None = None,
) -> JobTransition:
    """Transition for a result reported by the gateway for claim ``attempt``.

    ``acked`` -> state ``acked``, errors cleared, ``set_displayed_hash = job.render_hash``.
    ``failed`` -> ``failed`` with ``next_attempt_at`` from ``next_attempt_at()``, or
    ``dead`` (``next_attempt_at = None``) at ``MAX_ATTEMPTS``; ``error_code`` and
    ``error`` recorded. ``claimed_at`` is kept for the record. A ``timeout`` or
    ``protocol`` failure also sets ``displayed_hash_unknown``.

    Raises ``StaleResult`` unless the job is ``claimed`` with ``attempts == attempt``
    (a late result after a reclaim, or a duplicate). Raises ``ValueError`` if a failure
    has no code or an ack has one.
    """
    if job.state is not JobState.CLAIMED or job.attempts != attempt:
        raise StaleResult(
            f"job {job.job_id} is {job.state} at attempt {job.attempts}; "
            f"result for attempt {attempt} is not current"
        )
    if outcome is JobOutcome.ACKED:
        if error_code is not None:
            raise ValueError("an acked result has no error_code")
        return JobTransition(
            job_id=job.job_id,
            state=JobState.ACKED,
            attempts=job.attempts,
            next_attempt_at=None,
            claimed_at=job.claimed_at,
            gateway_id=None,
            error_code=None,
            error=None,
            set_displayed_hash=job.render_hash,
        )
    if error_code is None:
        raise ValueError("a failed result needs an error_code")
    retry_at = next_attempt_at(job.attempts, now)
    return JobTransition(
        job_id=job.job_id,
        state=JobState.FAILED if retry_at is not None else JobState.DEAD,
        attempts=job.attempts,
        next_attempt_at=retry_at,
        claimed_at=job.claimed_at,
        gateway_id=None,
        error_code=error_code,
        error=error,
        displayed_hash_unknown=error_code in _MAY_HAVE_LANDED,
    )


def next_attempt_at(attempts: int, now: datetime) -> datetime | None:
    """When a job that has failed after ``attempts`` pushes may run again.

    ``now + BACKOFF[attempts - 1]`` while ``attempts < MAX_ATTEMPTS``; ``None`` (dead)
    otherwise. Raises ``ValueError`` if ``attempts < 1``.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if attempts >= MAX_ATTEMPTS:
        return None
    return now + BACKOFF[attempts - 1]


def due_retries(jobs: Sequence[JobSnapshot], now: datetime) -> list[JobTransition]:
    """``failed`` jobs whose ``next_attempt_at <= now``, moved back to ``queued``.

    ``next_attempt_at`` and ``claimed_at`` are cleared; ``attempts`` and the last
    error are kept so monitoring can show why the job is being retried.
    """
    return [
        JobTransition(
            job_id=job.job_id,
            state=JobState.QUEUED,
            attempts=job.attempts,
            next_attempt_at=None,
            claimed_at=None,
            gateway_id=None,
            error_code=None,
            error=None,
        )
        for job in jobs
        if job.state is JobState.FAILED
        and job.next_attempt_at is not None
        and job.next_attempt_at <= now
    ]


def reclaim_stale(jobs: Sequence[JobSnapshot], now: datetime) -> list[JobTransition]:
    """``claimed`` jobs with ``claimed_at <= now - CLAIM_TIMEOUT``.

    Back to ``queued`` (``claimed_at`` and ``gateway_id`` cleared) or, if
    ``attempts >= MAX_ATTEMPTS``, to ``dead``. ``error_code`` becomes ``timeout`` and
    ``displayed_hash_unknown`` is set: the lost push may have landed.
    """
    cutoff = now - CLAIM_TIMEOUT
    return [
        JobTransition(
            job_id=job.job_id,
            state=JobState.DEAD if job.attempts >= MAX_ATTEMPTS else JobState.QUEUED,
            attempts=job.attempts,
            next_attempt_at=None,
            claimed_at=None,
            gateway_id=None,
            error_code=PushErrorCode.TIMEOUT,
            error=STALE_CLAIM_ERROR,
            displayed_hash_unknown=True,
        )
        for job in jobs
        if job.state is JobState.CLAIMED and job.claimed_at is not None and job.claimed_at <= cutoff
    ]


def admin_retry(
    tag: TagSnapshot, dead_job: JobSnapshot, active_job: JobSnapshot | None, now: datetime
) -> list[Action]:
    """An admin's "retry" on a dead job (Tags page, section 9).

    Returns ``[CreateJob(tag.tag_id, tag.desired_hash)]`` when the tag still needs an
    update; the new job starts with ``attempts = 0`` and ``created_at = now``, so dead-job
    suppression no longer applies. Returns ``[]`` when the tag is up to date,
    ``desired_hash`` is ``None``, or the tag already has an ``active_job``. The dead job
    stays as it is, for the record. Raises
    ``ValueError`` if ``dead_job`` is not ``dead`` or belongs to another tag. The caller
    logs ``job.retry`` in ``event_log``.
    """
    if dead_job.state is not JobState.DEAD:
        raise ValueError(f"job {dead_job.job_id} is {dead_job.state}, not dead")
    if dead_job.tag_id != tag.tag_id:
        raise ValueError(f"job {dead_job.job_id} belongs to tag {dead_job.tag_id}")
    if active_job is not None or tag.desired_hash is None:
        return []
    if tag.desired_hash == tag.displayed_hash:
        return []
    return [CreateJob(tag.tag_id, tag.desired_hash)]

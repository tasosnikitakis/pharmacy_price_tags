"""Desired-state reconciler and update-job state machine (PLAN.md section 5).

Owned by workstream C. Pure functions over snapshots; the caller loads the rows,
applies the returned actions in one transaction, and commits.

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
  if it had already used ``MAX_ATTEMPTS``.
- At most one job per tag is active (``queued``, ``claimed`` or ``failed``); the
  database enforces this with a partial unique index.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from shared.gateway_api import JobOutcome, PushErrorCode

BACKOFF: tuple[timedelta, ...] = tuple(timedelta(minutes=m) for m in (1, 5, 15, 60, 240))
MAX_ATTEMPTS = 5
CLAIM_TIMEOUT = timedelta(minutes=5)


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


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    job_id: int
    tag_id: int
    render_hash: str
    state: JobState
    attempts: int
    next_attempt_at: datetime | None
    claimed_at: datetime | None


@dataclass(frozen=True, slots=True)
class CreateJob:
    tag_id: int
    render_hash: str


@dataclass(frozen=True, slots=True)
class CancelJob:
    job_id: int


Action = CreateJob | CancelJob


@dataclass(frozen=True, slots=True)
class JobTransition:
    """New field values for one job after an event. ``None`` fields are unchanged."""

    job_id: int
    state: JobState
    attempts: int
    next_attempt_at: datetime | None
    error: str | None
    set_displayed_hash: str | None = None  # set tag.displayed_hash to this on ack


def plan_updates(
    tags: Sequence[TagSnapshot],
    active_jobs: Sequence[JobSnapshot],
    latest_dead: Sequence[JobSnapshot] = (),
) -> list[Action]:
    """Actions that bring every tag's queue in line with its ``desired_hash``.

    Per tag, with at most one active job:

    - ``desired_hash`` is ``None`` or equals ``displayed_hash``: cancel a queued or
      failed job; leave a claimed one alone.
    - Otherwise, no active job: create one for ``desired_hash``.
    - Active job for ``desired_hash`` already: nothing.
    - Queued or failed job for another hash: cancel it and create a new one.
    - Claimed job for another hash: nothing; wait for its result.
    - No active job, and the tag's most recent finished job is ``dead`` for
      ``desired_hash`` (given in ``latest_dead``): nothing. It stays visible in
      monitoring until an admin retries it; a new ``desired_hash`` queues normally.

    ``active_jobs`` must only contain jobs in ``ACTIVE_STATES``; ``latest_dead`` at
    most one ``dead`` job per tag. The returned actions
    are ordered so that every cancel comes before the create for the same tag.
    """
    raise NotImplementedError


def claim(job: JobSnapshot, now: datetime) -> JobTransition:
    """Transition for a job handed to a gateway: ``claimed`` and ``attempts + 1``.

    Raises ``ValueError`` unless the job is ``queued``.
    """
    raise NotImplementedError


def apply_result(
    job: JobSnapshot,
    outcome: JobOutcome,
    error_code: PushErrorCode | None,
    now: datetime,
) -> JobTransition:
    """Transition for a result reported by the gateway.

    ``acked`` -> state ``acked`` and ``set_displayed_hash = job.render_hash``.
    ``failed`` -> ``failed`` with backoff, or ``dead`` at ``MAX_ATTEMPTS``.
    Raises ``ValueError`` unless the job is ``claimed``, or if a failure has no code.
    """
    raise NotImplementedError


def next_attempt_at(attempts: int, now: datetime) -> datetime | None:
    """When a job that has failed after ``attempts`` pushes may run again.

    ``now + BACKOFF[attempts - 1]`` while ``attempts < MAX_ATTEMPTS``; ``None`` (dead)
    otherwise. Raises ``ValueError`` if ``attempts < 1``.
    """
    raise NotImplementedError


def due_retries(jobs: Sequence[JobSnapshot], now: datetime) -> list[JobTransition]:
    """``failed`` jobs whose ``next_attempt_at`` has passed, moved back to ``queued``."""
    raise NotImplementedError


def reclaim_stale(jobs: Sequence[JobSnapshot], now: datetime) -> list[JobTransition]:
    """``claimed`` jobs older than ``CLAIM_TIMEOUT``, back to ``queued`` (or ``dead``)."""
    raise NotImplementedError

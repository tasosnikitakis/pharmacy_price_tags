"""Reconciler and update-job state machine (workstream C). Placeholders until
implemented: remove the ``xfail`` marker as functions land."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.domain.reconciler import (
    BACKOFF,
    MAX_ATTEMPTS,
    CancelJob,
    CreateJob,
    JobSnapshot,
    JobState,
    TagSnapshot,
    apply_result,
    claim,
    next_attempt_at,
    plan_updates,
    reclaim_stale,
)
from shared.gateway_api import JobOutcome, PushErrorCode

pytestmark = pytest.mark.xfail(raises=NotImplementedError, reason="workstream C")

NOW = datetime(2026, 12, 1, 8, 0, tzinfo=UTC)
OLD, NEW = "a" * 64, "b" * 64


def _job(state: JobState, render_hash: str = OLD, attempts: int = 0, **kw: object) -> JobSnapshot:
    fields: dict[str, object] = dict(next_attempt_at=None, claimed_at=None)
    fields.update(kw)
    return JobSnapshot(1, 10, render_hash, state, attempts, **fields)  # type: ignore[arg-type]


def test_new_desired_hash_creates_job() -> None:
    assert plan_updates([TagSnapshot(10, NEW, OLD)], []) == [CreateJob(10, NEW)]


def test_queued_job_for_old_hash_is_cancelled_and_replaced() -> None:
    actions = plan_updates([TagSnapshot(10, NEW, None)], [_job(JobState.QUEUED, OLD)])
    assert actions == [CancelJob(1), CreateJob(10, NEW)]


def test_claimed_job_is_never_cancelled_and_ack_sets_its_own_hash() -> None:
    """A newer desired_hash waits for the claimed job; on ack, displayed_hash becomes
    the job's render_hash, and the next pass queues the newer label."""
    claimed = _job(JobState.CLAIMED, OLD, attempts=1, claimed_at=NOW)
    tag = TagSnapshot(10, desired_hash=NEW, displayed_hash=None)

    assert plan_updates([tag], [claimed]) == []

    transition = apply_result(claimed, JobOutcome.ACKED, None, NOW)
    assert transition.state is JobState.ACKED
    assert transition.set_displayed_hash == OLD

    after = TagSnapshot(10, desired_hash=NEW, displayed_hash=OLD)
    assert plan_updates([after], []) == [CreateJob(10, NEW)]


def test_up_to_date_tag_needs_nothing() -> None:
    assert plan_updates([TagSnapshot(10, OLD, OLD)], []) == []


def test_dead_job_is_not_recreated_for_same_hash() -> None:
    dead = _job(JobState.DEAD, NEW, attempts=MAX_ATTEMPTS)
    assert plan_updates([TagSnapshot(10, NEW, OLD)], [], latest_dead=[dead]) == []


def test_claim_counts_attempt() -> None:
    t = claim(_job(JobState.QUEUED), NOW)
    assert (t.state, t.attempts) == (JobState.CLAIMED, 1)


def test_backoff_schedule() -> None:
    for attempts in range(1, MAX_ATTEMPTS):
        assert next_attempt_at(attempts, NOW) == NOW + BACKOFF[attempts - 1]
    assert next_attempt_at(MAX_ATTEMPTS, NOW) is None


def test_failures_end_in_dead_after_max_attempts() -> None:
    job = _job(JobState.QUEUED)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        c = claim(job, NOW)
        job = _job(JobState.CLAIMED, attempts=c.attempts, claimed_at=NOW)
        r = apply_result(job, JobOutcome.FAILED, PushErrorCode.TIMEOUT, NOW)
        expected = JobState.DEAD if attempt == MAX_ATTEMPTS else JobState.FAILED
        assert r.state is expected
        job = _job(JobState.QUEUED, attempts=r.attempts)


def test_stale_claim_returns_to_queued() -> None:
    stale = _job(JobState.CLAIMED, attempts=1, claimed_at=NOW - timedelta(minutes=6))
    fresh = _job(JobState.CLAIMED, attempts=1, claimed_at=NOW - timedelta(minutes=1))
    transitions = reclaim_stale([stale, fresh], NOW)
    assert [t.state for t in transitions] == [JobState.QUEUED]

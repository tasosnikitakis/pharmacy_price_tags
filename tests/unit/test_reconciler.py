"""Reconciler and update-job state machine (workstream C)."""

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
    StaleResult,
    TagSnapshot,
    admin_retry,
    apply_result,
    claim,
    next_attempt_at,
    plan_updates,
    reclaim_stale,
)
from shared.gateway_api import JobOutcome, PushErrorCode

NOW = datetime(2026, 12, 1, 8, 0, tzinfo=UTC)
OLD, NEW = "a" * 64, "b" * 64


def _job(state: JobState, render_hash: str = OLD, attempts: int = 0, **kw: object) -> JobSnapshot:
    fields: dict[str, object] = dict(next_attempt_at=None, claimed_at=None, created_at=NOW)
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

    transition = apply_result(claimed, 1, JobOutcome.ACKED, None, NOW)
    assert transition.state is JobState.ACKED
    assert transition.set_displayed_hash == OLD

    after = TagSnapshot(10, desired_hash=NEW, displayed_hash=OLD)
    assert plan_updates([after], []) == [CreateJob(10, NEW)]


def test_up_to_date_tag_needs_nothing() -> None:
    assert plan_updates([TagSnapshot(10, OLD, OLD)], []) == []


def test_dead_job_is_not_recreated_for_same_hash() -> None:
    dead = _job(JobState.DEAD, NEW, attempts=MAX_ATTEMPTS, created_at=NOW)
    tag = TagSnapshot(10, NEW, OLD, desired_hash_changed_at=NOW - timedelta(minutes=1))
    assert plan_updates([tag], [], latest_dead=[dead]) == []


def test_stale_dead_job_does_not_block_same_hash_later() -> None:
    """Price goes to X (dead), back to the displayed price, then to X again: the
    old dead job must not stop the new push."""
    dead = _job(JobState.DEAD, NEW, attempts=MAX_ATTEMPTS, created_at=NOW - timedelta(hours=2))
    tag = TagSnapshot(10, NEW, OLD, desired_hash_changed_at=NOW)
    assert plan_updates([tag], [], latest_dead=[dead]) == [CreateJob(10, NEW)]


def test_claim_counts_attempt_and_sets_claim_fields() -> None:
    t = claim(_job(JobState.QUEUED, attempts=0), gateway_id=3, now=NOW)
    assert (t.state, t.attempts, t.claimed_at, t.gateway_id) == (JobState.CLAIMED, 1, NOW, 3)
    assert (t.error_code, t.error, t.next_attempt_at) == (None, None, None)


def test_backoff_schedule() -> None:
    for attempts in range(1, MAX_ATTEMPTS):
        assert next_attempt_at(attempts, NOW) == NOW + BACKOFF[attempts - 1]
    assert next_attempt_at(MAX_ATTEMPTS, NOW) is None


def test_failures_end_in_dead_after_max_attempts() -> None:
    job = _job(JobState.QUEUED)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        c = claim(job, 1, NOW)
        job = _job(JobState.CLAIMED, attempts=c.attempts, claimed_at=NOW)
        r = apply_result(job, c.attempts, JobOutcome.FAILED, PushErrorCode.TIMEOUT, NOW)
        expected = JobState.DEAD if attempt == MAX_ATTEMPTS else JobState.FAILED
        assert r.state is expected
        job = _job(JobState.QUEUED, attempts=r.attempts)


def test_stale_claim_returns_to_queued() -> None:
    stale = _job(JobState.CLAIMED, attempts=1, claimed_at=NOW - timedelta(minutes=6))
    fresh = _job(JobState.CLAIMED, attempts=1, claimed_at=NOW - timedelta(minutes=1))
    transitions = reclaim_stale([stale, fresh], NOW)
    assert [(t.state, t.claimed_at) for t in transitions] == [(JobState.QUEUED, None)]


def test_late_result_from_earlier_claim_is_rejected() -> None:
    """Claim 1 timed out and the job was claimed again (attempts=2); claim 1's
    delayed result must not touch the job."""
    reclaimed = _job(JobState.CLAIMED, attempts=2, claimed_at=NOW)
    with pytest.raises(StaleResult):
        apply_result(reclaimed, 1, JobOutcome.FAILED, PushErrorCode.TIMEOUT, NOW)
    with pytest.raises(StaleResult):
        apply_result(reclaimed, 1, JobOutcome.ACKED, None, NOW)
    assert apply_result(reclaimed, 2, JobOutcome.ACKED, None, NOW).state is JobState.ACKED


def test_admin_retry_creates_fresh_job() -> None:
    dead = _job(JobState.DEAD, NEW, attempts=MAX_ATTEMPTS, created_at=NOW - timedelta(hours=1))
    tag = TagSnapshot(10, NEW, OLD, desired_hash_changed_at=NOW - timedelta(hours=2))
    assert admin_retry(tag, dead, None, NOW) == [CreateJob(10, NEW)]
    assert admin_retry(TagSnapshot(10, NEW, NEW), dead, None, NOW) == []
    queued = _job(JobState.QUEUED, NEW)
    assert admin_retry(tag, dead, queued, NOW) == []


def test_lost_outcome_makes_displayed_hash_unknown_and_reverted_price_is_pushed() -> None:
    """Tag shows A. Job B is pushed, but the result is lost and the claim goes stale.
    The price is reverted to A. The tag may physically show B, so A must be pushed."""
    stale = _job(JobState.CLAIMED, NEW, attempts=1, claimed_at=NOW - timedelta(minutes=6))
    [t] = reclaim_stale([stale], NOW)
    assert t.displayed_hash_unknown and t.set_displayed_hash is None

    reverted = TagSnapshot(10, desired_hash=OLD, displayed_hash=None)  # NULLed by caller
    requeued = _job(JobState.QUEUED, NEW, attempts=1)
    assert plan_updates([reverted], [requeued]) == [CancelJob(1), CreateJob(10, OLD)]


@pytest.mark.parametrize(
    ("code", "unknown"),
    [(PushErrorCode.TIMEOUT, True), (PushErrorCode.PROTOCOL, True),
     (PushErrorCode.NOT_FOUND, False), (PushErrorCode.REJECTED, False)],
)  # fmt: skip
def test_failure_codes_that_may_have_landed(code: PushErrorCode, unknown: bool) -> None:
    job = _job(JobState.CLAIMED, attempts=1, claimed_at=NOW)
    assert apply_result(job, 1, JobOutcome.FAILED, code, NOW).displayed_hash_unknown is unknown


def test_stale_claim_at_max_attempts_is_dead() -> None:
    stale = _job(JobState.CLAIMED, attempts=MAX_ATTEMPTS, claimed_at=NOW - timedelta(minutes=6))
    assert [t.state for t in reclaim_stale([stale], NOW)] == [JobState.DEAD]

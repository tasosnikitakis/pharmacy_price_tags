"""Reconciler and update-job state machine (workstream C)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

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


# Beyond the placeholders: edge cases of each rule.


def test_up_to_date_or_unset_tag_cancels_queued_and_failed_but_not_claimed() -> None:
    for state in (JobState.QUEUED, JobState.FAILED):
        assert plan_updates([TagSnapshot(10, OLD, OLD)], [_job(state, NEW)]) == [CancelJob(1)]
        assert plan_updates([TagSnapshot(10, None, OLD)], [_job(state, NEW)]) == [CancelJob(1)]
    claimed = _job(JobState.CLAIMED, NEW, attempts=1, claimed_at=NOW)
    assert plan_updates([TagSnapshot(10, OLD, OLD)], [claimed]) == []


def test_active_job_for_desired_hash_needs_nothing() -> None:
    for state in (JobState.QUEUED, JobState.CLAIMED, JobState.FAILED):
        assert plan_updates([TagSnapshot(10, NEW, OLD)], [_job(state, NEW)]) == []


def test_failed_job_for_old_hash_is_replaced() -> None:
    failed = _job(JobState.FAILED, OLD, attempts=2, next_attempt_at=NOW)
    assert plan_updates([TagSnapshot(10, NEW, None)], [failed]) == [
        CancelJob(1),
        CreateJob(10, NEW),
    ]


def test_dead_job_rules() -> None:
    dead = _job(JobState.DEAD, NEW, attempts=MAX_ATTEMPTS, created_at=NOW)
    # Never changed since before any job: suppressed.
    assert plan_updates([TagSnapshot(10, NEW, OLD)], [], [dead]) == []
    # Changed at the very moment the dead job was created: still that label, suppressed.
    assert plan_updates([TagSnapshot(10, NEW, OLD, NOW)], [], [dead]) == []
    # A dead job for another label does not suppress.
    other = _job(JobState.DEAD, OLD, attempts=MAX_ATTEMPTS, created_at=NOW)
    assert plan_updates([TagSnapshot(10, NEW, None, NOW)], [], [other]) == [CreateJob(10, NEW)]


def test_plan_updates_checks_its_inputs() -> None:
    tag = TagSnapshot(10, NEW, OLD)
    with pytest.raises(ValueError, match="not active"):
        plan_updates([tag], [_job(JobState.DEAD)])
    with pytest.raises(ValueError, match="more than one active"):
        plan_updates([tag], [_job(JobState.QUEUED), _job(JobState.FAILED)])
    with pytest.raises(ValueError, match="latest_dead"):
        plan_updates([tag], [], [_job(JobState.ACKED)])


def test_plan_handles_many_tags_independently() -> None:
    tags = [TagSnapshot(1, NEW, OLD), TagSnapshot(2, OLD, OLD), TagSnapshot(3, NEW, None)]
    queued = JobSnapshot(5, 3, OLD, JobState.QUEUED, 0, None, None, NOW)
    assert plan_updates(tags, [queued]) == [CreateJob(1, NEW), CancelJob(5), CreateJob(3, NEW)]


@pytest.mark.parametrize("state", [s for s in JobState if s is not JobState.QUEUED])
def test_only_queued_jobs_can_be_claimed(state: JobState) -> None:
    with pytest.raises(ValueError):
        claim(_job(state), 1, NOW)


def test_failure_records_error_and_keeps_claimed_at() -> None:
    claimed_at = NOW - timedelta(seconds=30)
    job = _job(JobState.CLAIMED, attempts=2, claimed_at=claimed_at)
    t = apply_result(job, 2, JobOutcome.FAILED, PushErrorCode.NOT_FOUND, NOW, error="gone")
    assert (t.state, t.next_attempt_at, t.claimed_at) == (
        JobState.FAILED,
        NOW + BACKOFF[1],
        claimed_at,
    )
    assert (t.error_code, t.error, t.set_displayed_hash) == (PushErrorCode.NOT_FOUND, "gone", None)


def test_ack_clears_errors() -> None:
    t = apply_result(
        _job(JobState.CLAIMED, attempts=1, claimed_at=NOW), 1, JobOutcome.ACKED, None, NOW
    )
    assert (t.error_code, t.error, t.next_attempt_at, t.displayed_hash_unknown) == (
        None,
        None,
        None,
        False,
    )


def test_result_code_must_match_outcome() -> None:
    job = _job(JobState.CLAIMED, attempts=1, claimed_at=NOW)
    with pytest.raises(ValueError):
        apply_result(job, 1, JobOutcome.FAILED, None, NOW)
    with pytest.raises(ValueError):
        apply_result(job, 1, JobOutcome.ACKED, PushErrorCode.TIMEOUT, NOW)


@pytest.mark.parametrize("state", [JobState.QUEUED, JobState.ACKED, JobState.DEAD, JobState.FAILED])
def test_result_for_a_job_that_is_not_claimed_is_stale(state: JobState) -> None:
    with pytest.raises(StaleResult):
        apply_result(_job(state, attempts=1), 1, JobOutcome.ACKED, None, NOW)


def test_next_attempt_at_needs_a_push() -> None:
    with pytest.raises(ValueError):
        next_attempt_at(0, NOW)


def test_due_retries_requeue_only_due_failed_jobs() -> None:
    from core.domain.reconciler import due_retries

    due = JobSnapshot(1, 10, OLD, JobState.FAILED, 2, NOW, NOW - timedelta(minutes=6), NOW)
    early = JobSnapshot(2, 11, OLD, JobState.FAILED, 2, NOW + timedelta(seconds=1), None, NOW)
    queued = JobSnapshot(3, 12, OLD, JobState.QUEUED, 0, None, None, NOW)
    [t] = due_retries([due, early, queued], NOW)
    assert (t.job_id, t.state, t.attempts, t.next_attempt_at, t.claimed_at) == (
        1, JobState.QUEUED, 2, None, None,
    )  # fmt: skip


def test_stale_claim_records_timeout() -> None:
    from core.domain.reconciler import CLAIM_TIMEOUT, STALE_CLAIM_ERROR

    at_limit = _job(JobState.CLAIMED, attempts=1, claimed_at=NOW - CLAIM_TIMEOUT)
    [t] = reclaim_stale([at_limit, _job(JobState.QUEUED)], NOW)
    assert (t.error_code, t.error, t.gateway_id) == (PushErrorCode.TIMEOUT, STALE_CLAIM_ERROR, None)


def test_admin_retry_checks_its_inputs() -> None:
    tag = TagSnapshot(10, NEW, OLD)
    with pytest.raises(ValueError, match="not dead"):
        admin_retry(tag, _job(JobState.FAILED), None, NOW)
    other_tag = JobSnapshot(1, 99, NEW, JobState.DEAD, MAX_ATTEMPTS, None, None, NOW)
    with pytest.raises(ValueError, match="belongs to tag"):
        admin_retry(tag, other_tag, None, NOW)
    dead = _job(JobState.DEAD, NEW, attempts=MAX_ATTEMPTS)
    assert admin_retry(TagSnapshot(10, None, OLD), dead, None, NOW) == []


# What each transition keeps or clears of gateway_id, error_code and error.

FAILED_BY_GW3: dict[str, Any] = dict(gateway_id=3, error_code=PushErrorCode.NOT_FOUND, error="gone")


def test_claim_sets_new_gateway_and_clears_last_error() -> None:
    queued = _job(JobState.QUEUED, attempts=2, **FAILED_BY_GW3)
    t = claim(queued, gateway_id=4, now=NOW)
    assert (t.gateway_id, t.error_code, t.error) == (4, None, None)


def test_ack_keeps_gateway_and_clears_errors() -> None:
    job = _job(JobState.CLAIMED, attempts=1, claimed_at=NOW, gateway_id=3)
    t = apply_result(job, 1, JobOutcome.ACKED, None, NOW)
    assert (t.gateway_id, t.error_code, t.error) == (3, None, None)


@pytest.mark.parametrize("attempts", [1, MAX_ATTEMPTS], ids=["failed", "dead"])
def test_failure_keeps_gateway_and_records_new_error(attempts: int) -> None:
    job = _job(JobState.CLAIMED, attempts=attempts, claimed_at=NOW, gateway_id=3)
    t = apply_result(job, attempts, JobOutcome.FAILED, PushErrorCode.PROTOCOL, NOW, error="bad")
    assert (t.gateway_id, t.error_code, t.error) == (3, PushErrorCode.PROTOCOL, "bad")


def test_due_retry_keeps_last_error_and_clears_gateway() -> None:
    from core.domain.reconciler import due_retries

    failed = _job(JobState.FAILED, attempts=2, next_attempt_at=NOW, claimed_at=NOW, **FAILED_BY_GW3)
    [t] = due_retries([failed], NOW)
    assert (t.state, t.gateway_id, t.claimed_at) == (JobState.QUEUED, None, None)
    assert (t.error_code, t.error) == (PushErrorCode.NOT_FOUND, "gone")


@pytest.mark.parametrize("attempts", [1, MAX_ATTEMPTS], ids=["queued", "dead"])
def test_stale_claim_clears_gateway_and_sets_timeout(attempts: int) -> None:
    from core.domain.reconciler import CLAIM_TIMEOUT, STALE_CLAIM_ERROR

    stale = _job(
        JobState.CLAIMED, attempts=attempts, claimed_at=NOW - CLAIM_TIMEOUT, **FAILED_BY_GW3
    )
    [t] = reclaim_stale([stale], NOW)
    assert (t.gateway_id, t.error_code, t.error) == (None, PushErrorCode.TIMEOUT, STALE_CLAIM_ERROR)


def test_admin_retry_creates_a_job_without_gateway_or_error() -> None:
    dead = _job(JobState.DEAD, NEW, attempts=MAX_ATTEMPTS, **FAILED_BY_GW3)
    # CreateJob has no gateway or error fields: the new job starts clean.
    assert admin_retry(TagSnapshot(10, NEW, OLD), dead, None, NOW) == [CreateJob(10, NEW)]

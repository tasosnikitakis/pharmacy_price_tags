"""Shape checks for the wave 0 contracts. They pass now and must keep passing."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import DateTime, Integer

from core.db.models import Base
from gateway.drivers.base import PushError, PushResult, SeenTag, TagDriver, TagStatus
from shared.gateway_api import (
    ClaimedJob,
    ClaimRequest,
    JobOutcome,
    JobResultRequest,
    SeenTagReport,
    TagsSeenRequest,
)
from shared.tag_types import PALETTE_COLOURS, Palette, TagType

GICISKY = TagType(
    key="gicisky-2.9-bwr", vendor="gicisky", model="2.9", width=296, height=128,
    palette=Palette.BWR,
)  # fmt: skip


def test_every_table_but_store_has_store_id() -> None:
    for table in Base.metadata.sorted_tables:
        if table.name != "store":
            assert "store_id" in table.c, table.name
            assert not table.c.store_id.nullable, table.name


def test_money_is_integer_cents() -> None:
    for table in Base.metadata.sorted_tables:
        for column in table.c:
            if column.name.endswith("_cents"):
                assert isinstance(column.type, Integer), f"{table.name}.{column.name}"


def test_timestamps_are_timezone_aware() -> None:
    for table in Base.metadata.sorted_tables:
        for column in table.c:
            if isinstance(column.type, DateTime):
                assert column.type.timezone, f"{table.name}.{column.name}"


def test_palettes_start_with_white_and_match_their_names() -> None:
    for palette, colours in PALETTE_COLOURS.items():
        assert colours[0] == (255, 255, 255)
        assert len(colours) == len(palette.value)


def test_claimed_job_round_trips() -> None:
    job = ClaimedJob(
        job_id=7, vendor_id="AA:BB:CC:DD:EE:FF", tag_type=GICISKY, render_hash="0" * 64,
        attempt=1,
    )  # fmt: skip
    assert ClaimedJob.model_validate_json(job.model_dump_json()) == job


def test_render_hash_must_be_sha256_hex() -> None:
    with pytest.raises(ValidationError):
        ClaimedJob(job_id=1, vendor_id="x", tag_type=GICISKY, render_hash="abc", attempt=1)


def test_api_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ClaimRequest.model_validate({"max_jobs": 1, "gateway": "sneaky"})


def test_job_result_request() -> None:
    req = JobResultRequest(outcome=JobOutcome.FAILED, error_code=PushError.TIMEOUT, duration_ms=1)
    assert req.model_dump(mode="json")["error_code"] == "timeout"
    JobResultRequest(outcome=JobOutcome.ACKED, duration_ms=1)


def test_job_result_code_must_match_outcome() -> None:
    with pytest.raises(ValidationError):
        JobResultRequest(outcome=JobOutcome.FAILED, duration_ms=1)
    with pytest.raises(ValidationError):
        JobResultRequest(outcome=JobOutcome.ACKED, error_code=PushError.TIMEOUT, duration_ms=1)


def test_job_state_names_match_domain_and_db() -> None:
    from typing import get_args

    from core.db.models import JOB_STATES
    from core.domain.reconciler import JobState
    from shared.gateway_api import JobStateName

    assert set(get_args(JobStateName)) == {s.value for s in JobState} == set(JOB_STATES)


def test_seen_report_needs_aware_datetime() -> None:
    with pytest.raises(ValidationError):
        SeenTagReport(vendor_id="x", vendor="fake", seen_at=datetime(2026, 1, 1))  # noqa: DTZ001
    TagsSeenRequest(tags=[SeenTagReport(vendor_id="x", vendor="fake", seen_at=datetime.now(UTC))])


def test_push_result_invariants() -> None:
    PushResult(ok=True, error=None, duration_s=1.0)
    PushResult(ok=False, error=PushError.REJECTED, duration_s=0.5)
    with pytest.raises(ValueError):
        PushResult(ok=True, error=PushError.TIMEOUT, duration_s=1.0)
    with pytest.raises(ValueError):
        PushResult(ok=False, error=None, duration_s=1.0)


class _Dummy:
    vendor = "dummy"

    async def discover(self, timeout_s: float) -> list[SeenTag]:
        return []

    async def push_image(self, vendor_id: str, tag_type: TagType, png: bytes) -> PushResult:
        return PushResult(ok=True, error=None, duration_s=0.0)

    async def status(self, vendor_id: str) -> TagStatus | None:
        return None


def test_driver_protocol_is_structural() -> None:
    driver: TagDriver = _Dummy()
    assert isinstance(driver, TagDriver)

"""Request and response models for the gateway API (PLAN.md section 3).

All endpoints live under ``/gateway/v1`` on the core and require
``Authorization: Bearer <gateway token>``. The gateway always dials out to the core.

Contract: changes go through the lead agent.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from shared.tag_types import TagType

API_PREFIX = "/gateway/v1"

#: sha256 hex digest of a render.
RenderHash = str
_HASH_PATTERN = r"^[0-9a-f]{64}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PushErrorCode(StrEnum):
    """Why a push failed. Same codes as ``gateway.drivers.base.PushResult``."""

    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    REJECTED = "rejected"
    PROTOCOL = "protocol"


class JobOutcome(StrEnum):
    """What the gateway reports for a claimed job."""

    ACKED = "acked"
    FAILED = "failed"


# POST /gateway/v1/jobs/claim


class ClaimRequest(_Model):
    """Ask for up to ``max_jobs`` queued jobs. The core marks them ``claimed``."""

    max_jobs: int = Field(default=1, ge=1, le=50)


class ClaimedJob(_Model):
    """One job the gateway must push now."""

    job_id: int
    vendor_id: str = Field(description="BLE address or vendor serial of the target tag")
    tag_type: TagType
    render_hash: RenderHash = Field(pattern=_HASH_PATTERN)
    attempt: int = Field(ge=1, description="1 for the first try, 2 for the first retry, ...")


class ClaimResponse(_Model):
    jobs: list[ClaimedJob]


# GET /gateway/v1/renders/{hash}
# Returns ``image/png`` bytes; no JSON model. 404 if the hash is unknown.

RENDER_MEDIA_TYPE = "image/png"


# POST /gateway/v1/jobs/{id}/result


class JobResultRequest(_Model):
    """Result of one push. ``error_code`` is required when ``outcome`` is ``failed``."""

    outcome: JobOutcome
    error_code: PushErrorCode | None = None
    error_detail: str | None = Field(default=None, max_length=500)
    duration_ms: int = Field(ge=0)


class JobResultResponse(_Model):
    """State of the job after the core processed the result."""

    job_id: int
    state: str


# POST /gateway/v1/tags/seen


class SeenTagReport(_Model):
    vendor_id: str
    vendor: str
    model: str | None = None
    rssi: int | None = Field(default=None, description="dBm")
    battery_mv: int | None = Field(default=None, ge=0)
    battery_pct: int | None = Field(default=None, ge=0, le=100)
    seen_at: AwareDatetime


class TagsSeenRequest(_Model):
    tags: list[SeenTagReport] = Field(max_length=1000)


class TagsSeenResponse(_Model):
    known: int = Field(description="Reports that matched a tag already in the catalogue")
    created: int = Field(description="Reports that created a new, unbound tag")


# POST /gateway/v1/heartbeat


class HeartbeatRequest(_Model):
    version: str = Field(max_length=64)
    driver: str = Field(max_length=32, description="Vendor of the active driver, e.g. 'fake'")


class HeartbeatResponse(_Model):
    server_time: datetime

"""The hardware-neutral tag driver interface (PLAN.md section 8).

Every tag brand sits behind ``TagDriver``. Drivers hold no business logic: they
discover tags, push palette PNGs and report what happened.

Contract: changes go through the lead agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from shared.gateway_api import PushErrorCode
from shared.tag_types import TagType

#: Push error codes, re-exported so drivers need only this module.
PushError = PushErrorCode

#: Hard upper bound for one ``push_image`` call, in seconds (section 8, reliability).
PUSH_TIMEOUT_S = 60.0


@dataclass(frozen=True, slots=True)
class SeenTag:
    """A tag found during discovery."""

    vendor_id: str
    model: str | None
    rssi: int | None
    battery_mv: int | None
    battery_pct: int | None  # None unless the vendor reports a calibrated percentage
    seen_at: datetime  # timezone-aware UTC
    firmware: str | None = None


@dataclass(frozen=True, slots=True)
class PushResult:
    """Outcome of one push.

    ``ok`` is true only when the tag confirmed it received the full image.
    ``error`` is ``None`` exactly when ``ok`` is true.
    """

    ok: bool
    error: PushError | None
    duration_s: float
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.ok and self.error is not None:
            raise ValueError("a successful PushResult has no error")
        if not self.ok and self.error is None:
            raise ValueError("a failed PushResult needs an error code")
        if self.duration_s < 0:
            raise ValueError("duration_s must not be negative")


@dataclass(frozen=True, slots=True)
class TagStatus:
    """Reachability and battery for one tag, if the driver can tell."""

    vendor_id: str
    reachable: bool
    rssi: int | None
    battery_mv: int | None
    battery_pct: int | None


@runtime_checkable
class TagDriver(Protocol):
    """Interface every tag driver implements."""

    vendor: str

    async def discover(self, timeout_s: float) -> list[SeenTag]:
        """Tags currently reachable: vendor_id, model, rssi, battery if known."""
        ...

    async def push_image(self, vendor_id: str, tag_type: TagType, png: bytes) -> PushResult:
        """Send a palette PNG. Return ok only when the tag confirmed the full image.

        Must never raise for tag-side problems; map them to a ``PushError`` instead.
        A ``png`` whose size or palette does not match ``tag_type`` is ``rejected``
        without contacting the tag. Must return within ``PUSH_TIMEOUT_S``.
        """
        ...

    async def status(self, vendor_id: str) -> TagStatus | None:
        """Optional: battery and reachability for one tag. ``None`` if unsupported."""
        ...

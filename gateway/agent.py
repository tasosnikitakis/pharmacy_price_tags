"""Gateway agent: claim jobs, push images through the driver, report results.

It also reports discovered tags and sends heartbeats. It holds no business logic: the
core decides what each tag shows and when to retry; the agent only does what a claim
says and reports back. Its only persistent data is a small cache of downloaded images.

Everything runs in one loop, one thing at a time, because pushes and scans share one
radio. The HTTP client is injected, so tests run the agent against the core app in
process (``httpx.ASGITransport``) or a ``httpx.MockTransport``.

This module imports only ``gateway`` and ``shared``, never ``core``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import httpx

from gateway.config import GatewaySettings
from gateway.drivers.base import PUSH_TIMEOUT_S, PushError, PushResult, SeenTag, TagDriver
from shared.gateway_api import (
    API_PREFIX,
    ClaimedJob,
    ClaimRequest,
    ClaimResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    JobOutcome,
    JobResultRequest,
    JobResultResponse,
    SeenTagReport,
    TagsSeenRequest,
    TagsSeenResponse,
)

log = logging.getLogger(__name__)

try:
    VERSION = metadata.version("pharmacy-esl")
except metadata.PackageNotFoundError:  # pragma: no cover - running from a bare checkout
    VERSION = "0.0.0"

#: Most tags one ``/tags/seen`` request may carry (``TagsSeenRequest``).
_SEEN_BATCH = 1000
_HASH = re.compile(r"^[0-9a-f]{64}$")


class FinalRejection(Exception):
    """The core refused a result for good (409 stale, 404 unknown job): do not retry."""


def make_http_client(settings: GatewaySettings, *, timeout_s: float = 30.0) -> httpx.AsyncClient:
    """An HTTP client for the core with the bearer token set. The token is never logged."""
    token = settings.gateway_token.get_secret_value()
    return httpx.AsyncClient(
        base_url=str(settings.core_url).rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout_s,
    )


class CoreClient:
    """The gateway API (``shared.gateway_api``) over an injected ``httpx.AsyncClient``."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def claim(self, max_jobs: int) -> list[ClaimedJob]:
        body = ClaimRequest(max_jobs=max_jobs).model_dump(mode="json")
        response = await self.http.post(f"{API_PREFIX}/jobs/claim", json=body)
        response.raise_for_status()
        return ClaimResponse.model_validate_json(response.content).jobs

    async def render(self, render_hash: str) -> bytes:
        response = await self.http.get(f"{API_PREFIX}/renders/{render_hash}")
        response.raise_for_status()
        return response.content

    async def report(self, job_id: int, result: JobResultRequest) -> JobResultResponse:
        response = await self.http.post(
            f"{API_PREFIX}/jobs/{job_id}/result", json=result.model_dump(mode="json")
        )
        if response.status_code in (404, 409):
            raise FinalRejection(f"job {job_id}: {response.status_code}")
        response.raise_for_status()
        return JobResultResponse.model_validate_json(response.content)

    async def tags_seen(self, vendor: str, tags: list[SeenTag]) -> TagsSeenResponse:
        request = TagsSeenRequest(
            tags=[
                SeenTagReport(
                    vendor_id=t.vendor_id,
                    vendor=vendor,
                    model=t.model,
                    rssi=t.rssi,
                    battery_mv=t.battery_mv,
                    battery_pct=t.battery_pct,
                    firmware=t.firmware,
                    seen_at=t.seen_at,
                )
                for t in tags
            ]
        )
        response = await self.http.post(
            f"{API_PREFIX}/tags/seen", json=request.model_dump(mode="json")
        )
        response.raise_for_status()
        return TagsSeenResponse.model_validate_json(response.content)

    async def heartbeat(self, version: str, driver: str) -> HeartbeatResponse:
        body = HeartbeatRequest(version=version, driver=driver).model_dump(mode="json")
        response = await self.http.post(f"{API_PREFIX}/heartbeat", json=body)
        response.raise_for_status()
        return HeartbeatResponse.model_validate_json(response.content)


class RenderCache:
    """Downloaded label images on disk, by render hash, keeping the newest ``max_files``."""

    def __init__(self, directory: Path, max_files: int = 256) -> None:
        self.directory = directory
        self.max_files = max_files

    def _path(self, render_hash: str) -> Path:
        if not _HASH.match(render_hash):
            raise ValueError("not a render hash")
        return self.directory / f"{render_hash}.png"

    def get(self, render_hash: str) -> bytes | None:
        try:
            return self._path(render_hash).read_bytes()
        except FileNotFoundError:
            return None

    def put(self, render_hash: str, png: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._path(render_hash)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(png)
        os.replace(tmp, target)
        self._evict()

    def _evict(self) -> None:
        files = sorted(self.directory.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for old in files[: max(0, len(files) - self.max_files)]:
            old.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class AgentTimings:
    """How often the agent does things, in seconds."""

    poll_interval_s: float = 2.0  # wait between claims when there was nothing to do
    discovery_interval_s: float = 60.0
    discovery_timeout_s: float = 10.0
    heartbeat_interval_s: float = 30.0
    error_backoff_s: float = 5.0  # wait after the core could not be reached
    report_retries: int = 3
    report_retry_delay_s: float = 1.0
    max_jobs: int = 5


class Agent:
    """The claim, push and report loop."""

    def __init__(
        self,
        core: CoreClient,
        driver: TagDriver,
        cache: RenderCache,
        *,
        timings: AgentTimings | None = None,
        version: str = VERSION,
        push_timeout_s: float = PUSH_TIMEOUT_S,
    ) -> None:
        self.core = core
        self.driver = driver
        self.cache = cache
        self.timings = timings or AgentTimings()
        self.version = version
        self.push_timeout_s = push_timeout_s

    # One step each; ``run`` schedules them.

    async def process_jobs(self) -> int:
        """Claim once and push every claimed job. Returns how many were claimed."""
        jobs = await self.core.claim(self.timings.max_jobs)
        for job in jobs:
            await self.handle_job(job)
        return len(jobs)

    async def handle_job(self, job: ClaimedJob) -> PushResult | None:
        """Download, push and report one job. ``None`` if the image could not be fetched;
        the core then returns the claim to the queue when it times out."""
        try:
            png = await self._image(job.render_hash)
        except httpx.HTTPError as exc:
            log.warning("job %s: image not downloaded: %s", job.job_id, _describe(exc))
            return None
        result = await self._push(job, png)
        await self._report(job, result)
        return result

    async def discover_once(self) -> TagsSeenResponse | None:
        seen = await self.driver.discover(self.timings.discovery_timeout_s)
        response = None
        for start in range(0, len(seen), _SEEN_BATCH):
            response = await self.core.tags_seen(
                self.driver.vendor, seen[start : start + _SEEN_BATCH]
            )
        return response

    async def heartbeat_once(self) -> HeartbeatResponse:
        return await self.core.heartbeat(self.version, self.driver.vendor)

    async def run(self, stop: asyncio.Event) -> None:
        """Run until ``stop`` is set."""
        next_heartbeat = next_discovery = 0.0
        t = self.timings
        while not stop.is_set():
            wait = t.poll_interval_s
            try:
                now = time.monotonic()
                if now >= next_heartbeat:
                    await self.heartbeat_once()
                    next_heartbeat = now + t.heartbeat_interval_s
                if now >= next_discovery:
                    await self.discover_once()
                    next_discovery = now + t.discovery_interval_s
                if await self.process_jobs():
                    wait = 0.0  # there may be more
            except httpx.HTTPError as exc:
                log.warning("core not reachable: %s", _describe(exc))
                wait = t.error_backoff_s
            if wait:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=wait)

    # Internals

    async def _image(self, render_hash: str) -> bytes:
        cached = self.cache.get(render_hash)
        if cached is not None:
            return cached
        png = await self.core.render(render_hash)
        try:
            self.cache.put(render_hash, png)
        except OSError as exc:
            log.warning("render cache not writable: %s", exc)
        return png

    async def _push(self, job: ClaimedJob, png: bytes) -> PushResult:
        started = time.monotonic()
        try:
            return await asyncio.wait_for(
                self.driver.push_image(job.vendor_id, job.tag_type, png),
                timeout=self.push_timeout_s,
            )
        except TimeoutError:
            error, detail = PushError.TIMEOUT, "push exceeded the overall time limit"
        except Exception as exc:  # a driver bug must not stop the loop
            log.exception("driver raised during push of job %s", job.job_id)
            error, detail = PushError.PROTOCOL, f"driver error: {type(exc).__name__}"
        return PushResult(False, error, max(0.0, time.monotonic() - started), detail)

    async def _report(self, job: ClaimedJob, result: PushResult) -> None:
        request = JobResultRequest(
            attempt=job.attempt,
            outcome=JobOutcome.ACKED if result.ok else JobOutcome.FAILED,
            error_code=result.error,
            error_detail=result.detail[:500] if result.detail else None,
            duration_ms=int(result.duration_s * 1000),
        )
        for attempt in range(1, self.timings.report_retries + 1):
            try:
                await self.core.report(job.job_id, request)
                return
            except FinalRejection as exc:
                log.info("result not accepted, final: %s", exc)
                return
            except httpx.HTTPError as exc:
                if attempt == self.timings.report_retries:
                    log.warning("result for job %s not delivered: %s", job.job_id, _describe(exc))
                    return
                await asyncio.sleep(self.timings.report_retry_delay_s * attempt)


def _describe(exc: httpx.HTTPError) -> str:
    """An error description without request headers (which hold the token)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from {exc.request.url.path}"
    return type(exc).__name__

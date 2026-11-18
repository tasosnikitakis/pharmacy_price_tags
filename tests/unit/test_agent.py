"""The gateway agent against a mock core (``httpx.MockTransport``); no database."""

from __future__ import annotations

import ast
import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway.agent import Agent, AgentTimings, CoreClient, RenderCache, make_http_client
from gateway.drivers.base import PushError, PushResult, SeenTag, TagStatus
from gateway.drivers.fake import FakeDriver, FakeTag
from shared.gateway_api import API_PREFIX
from shared.tag_types import Palette, TagType
from tests.unit.test_fake_driver import palette_png

BWR = TagType(
    key="fake-bwr", vendor="fake", model="2.9-bwr", width=296, height=128, palette=Palette.BWR
)
HASH = "c" * 64
FAST = AgentTimings(poll_interval_s=0.01, error_backoff_s=0.01, report_retry_delay_s=0.0)


class MockCore:
    """Just enough of the core's gateway API to drive the agent."""

    def __init__(self, jobs: list[dict[str, Any]], result_status: int = 200) -> None:
        self.jobs = jobs
        self.result_status = result_status
        self.requests: list[tuple[str, str]] = []
        self.results: list[tuple[int, dict[str, Any]]] = []
        self.seen: list[dict[str, Any]] = []
        self.heartbeats: list[dict[str, Any]] = []
        self.auth: set[str | None] = set()
        self.png = palette_png(BWR)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(API_PREFIX)
        self.requests.append((request.method, path))
        self.auth.add(request.headers.get("authorization"))
        if path == "/jobs/claim":
            jobs, self.jobs = self.jobs, []
            return httpx.Response(200, json={"jobs": jobs})
        if path.startswith("/renders/"):
            return httpx.Response(200, content=self.png, headers={"content-type": "image/png"})
        if path.startswith("/jobs/") and path.endswith("/result"):
            body = json.loads(request.content)
            self.results.append((int(path.split("/")[2]), body))
            if self.result_status != 200:
                return httpx.Response(self.result_status, json={"detail": "x"})
            state = "acked" if body["outcome"] == "acked" else "failed"
            return httpx.Response(200, json={"job_id": int(path.split("/")[2]), "state": state})
        if path == "/tags/seen":
            body = json.loads(request.content)
            self.seen.extend(body["tags"])
            return httpx.Response(
                200, json={"known": 0, "created": len(body["tags"]), "ignored": 0}
            )
        if path == "/heartbeat":
            self.heartbeats.append(json.loads(request.content))
            return httpx.Response(200, json={"server_time": datetime.now(UTC).isoformat()})
        return httpx.Response(404)


def _job(job_id: int = 1, attempt: int = 1, vendor_id: str = "T1") -> dict[str, Any]:
    return {
        "job_id": job_id,
        "vendor_id": vendor_id,
        "tag_type": BWR.model_dump(mode="json"),
        "render_hash": HASH,
        "attempt": attempt,
    }


def _agent(core: MockCore, driver: Any, tmp_path: Path) -> tuple[Agent, httpx.AsyncClient]:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(core.handler),
        base_url="http://core.test",
        headers={"Authorization": "Bearer t"},
    )
    return Agent(CoreClient(http), driver, RenderCache(tmp_path / "cache"), timings=FAST), http


def _fake(tmp_path: Path, **kw: Any) -> FakeDriver:
    return FakeDriver(
        [FakeTag("T1", "2.9-bwr", battery_mv=2900)], output_dir=tmp_path / "out", **kw
    )


async def test_claims_pushes_and_reports_ack_with_attempt(tmp_path: Path) -> None:
    core = MockCore([_job(7, attempt=3)])
    driver = _fake(tmp_path)
    agent, http = _agent(core, driver, tmp_path)
    async with http:
        assert await agent.process_jobs() == 1
    [(job_id, body)] = core.results
    assert job_id == 7
    assert body["attempt"] == 3 and body["outcome"] == "acked" and body["error_code"] is None
    assert (tmp_path / "out" / "T1.png").read_bytes() == core.png


async def test_failure_is_reported_with_code(tmp_path: Path) -> None:
    core = MockCore([_job()])
    agent, http = _agent(
        core, _fake(tmp_path, fail_rate=1.0, fail_with=PushError.PROTOCOL), tmp_path
    )
    async with http:
        await agent.process_jobs()
    [(_, body)] = core.results
    assert (body["outcome"], body["error_code"], body["error_detail"]) == (
        "failed", "protocol", "injected failure",
    )  # fmt: skip


async def test_images_are_cached_by_hash(tmp_path: Path) -> None:
    core = MockCore([_job(1), _job(2)])
    agent, http = _agent(core, _fake(tmp_path), tmp_path)
    async with http:
        await agent.process_jobs()
    assert [r for r in core.requests if r[1].startswith("/renders/")] == [
        ("GET", f"/renders/{HASH}")
    ]
    assert (tmp_path / "cache" / f"{HASH}.png").read_bytes() == core.png


def test_cache_keeps_newest_files(tmp_path: Path) -> None:
    import os

    cache = RenderCache(tmp_path, max_files=2)
    for i, h in enumerate(("a" * 64, "b" * 64, "c" * 64)):
        cache.put(h, b"x")
        os.utime(tmp_path / f"{h}.png", (i, i))
    cache.put("d" * 64, b"y")
    assert sorted(p.stem[0] for p in tmp_path.glob("*.png")) == ["c", "d"]
    with pytest.raises(ValueError):
        cache.get("../etc")


@pytest.mark.parametrize("status", [409, 404])
async def test_409_and_404_are_final(tmp_path: Path, status: int) -> None:
    core = MockCore([_job()], result_status=status)
    agent, http = _agent(core, _fake(tmp_path), tmp_path)
    async with http:
        await agent.process_jobs()
    assert len(core.results) == 1  # not retried


async def test_server_error_on_result_is_retried(tmp_path: Path) -> None:
    core = MockCore([_job()], result_status=503)
    agent, http = _agent(core, _fake(tmp_path), tmp_path)
    async with http:
        await agent.process_jobs()
    assert len(core.results) == FAST.report_retries


class _HangingDriver:
    vendor = "fake"

    async def discover(self, timeout_s: float) -> list[SeenTag]:
        return []

    async def push_image(self, vendor_id: str, tag_type: TagType, png: bytes) -> PushResult:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    async def status(self, vendor_id: str) -> TagStatus | None:
        return None


class _BrokenDriver(_HangingDriver):
    async def push_image(self, vendor_id: str, tag_type: TagType, png: bytes) -> PushResult:
        raise RuntimeError("bug")


async def test_push_over_time_limit_is_reported_as_timeout(tmp_path: Path) -> None:
    core = MockCore([_job()])
    http = httpx.AsyncClient(transport=httpx.MockTransport(core.handler), base_url="http://c")
    agent = Agent(
        CoreClient(http), _HangingDriver(), RenderCache(tmp_path), timings=FAST, push_timeout_s=0.05
    )
    async with http:
        await agent.process_jobs()
    assert core.results[0][1]["error_code"] == "timeout"


async def test_driver_exception_is_reported_as_protocol(tmp_path: Path) -> None:
    core = MockCore([_job()])
    agent, http = _agent(core, _BrokenDriver(), tmp_path)
    async with http:
        await agent.process_jobs()
    assert core.results[0][1]["error_code"] == "protocol"


async def test_discovery_and_heartbeat(tmp_path: Path) -> None:
    core = MockCore([])
    agent, http = _agent(core, _fake(tmp_path), tmp_path)
    async with http:
        await agent.discover_once()
        await agent.heartbeat_once()
    [seen] = core.seen
    assert (seen["vendor_id"], seen["vendor"], seen["model"], seen["battery_mv"]) == (
        "T1", "fake", "2.9-bwr", 2900,
    )  # fmt: skip
    assert core.heartbeats[0]["driver"] == "fake"


async def test_run_loop_does_everything_and_stops(tmp_path: Path) -> None:
    core = MockCore([_job()])
    agent, http = _agent(core, _fake(tmp_path), tmp_path)
    stop = asyncio.Event()
    async with http:
        task = asyncio.create_task(agent.run(stop))
        for _ in range(200):
            if core.results:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 1)
    assert core.heartbeats and core.seen and core.results


async def test_run_loop_survives_unreachable_core(tmp_path: Path) -> None:
    calls = 0

    def down(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("down", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(down), base_url="http://c")
    agent = Agent(CoreClient(http), _fake(tmp_path), RenderCache(tmp_path), timings=FAST)
    stop = asyncio.Event()
    async with http:
        task = asyncio.create_task(agent.run(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, 1)
    assert calls >= 2


def test_http_client_sends_bearer_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from gateway.config import load_gateway_settings

    token = "x" * 40
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CORE_URL", "http://core.test:8000/")
    monkeypatch.setenv("GATEWAY_TOKEN", token)
    client = make_http_client(load_gateway_settings())
    assert client.headers["authorization"] == f"Bearer {token}"
    assert str(client.base_url) == "http://core.test:8000"


# The process boundary: the gateway imports only ``gateway`` and ``shared``.

GATEWAY = Path(__file__).resolve().parents[2] / "gateway"


@pytest.mark.parametrize("path", sorted(GATEWAY.rglob("*.py")), ids=lambda p: p.name)
def test_gateway_never_imports_core(path: Path) -> None:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            assert name != "core" and not name.startswith("core."), f"{path.name} imports {name}"


def test_gateway_does_not_load_core_transitively() -> None:
    code = (
        "import sys, gateway.agent, gateway.drivers.fake, gateway.__main__\n"
        "print('\\n'.join(sorted(sys.modules)))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True,
        cwd=GATEWAY.parent,
    ).stdout.split()  # fmt: skip
    bad = [m for m in loaded if m == "core" or m.startswith(("core.", "sqlalchemy", "fastapi"))]
    assert bad == []

"""M3 end to end: the core app and the real gateway agent with the fake driver.

The core runs in process (``httpx.ASGITransport``) on the rolled-back test session.
Every test runs twice: with the real label pipeline (``core.domain.pricing`` and
``core.render``, the M3 evidence) and with the fake renderer from ``support``. Time is
controlled through ``get_now``, so backoff takes no real time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.db.models import Tag, UpdateJob
from core.domain.reconciler import BACKOFF, MAX_ATTEMPTS
from core.services.tags import LabelPipeline, real_label_pipeline, refresh_desired_hashes
from gateway.agent import Agent, AgentTimings, CoreClient, RenderCache
from gateway.drivers.base import PushError
from gateway.drivers.fake import FakeDriver, FakeTag
from tests.e2e.support import Clock, FakePipeline, World, auth, bind, build_app, seed, set_price

pytestmark = pytest.mark.db


@dataclass
class Rig:
    db: Session
    world: World
    clock: Clock
    pipeline: LabelPipeline
    driver: FakeDriver
    agent: Agent

    async def drain(self) -> int:
        """Run claim, push and report until the queue has nothing due."""
        total = 0
        while n := await self.agent.process_jobs():
            total += n
        return total

    def tag(self, vendor_id: str) -> Tag:
        tag = self.db.scalars(select(Tag).where(Tag.vendor_id == vendor_id)).one()
        self.db.refresh(tag)
        return tag

    def image(self, vendor_id: str) -> bytes | None:
        path = self.driver.image_path(vendor_id)
        return path.read_bytes() if path.exists() else None


PIPELINES: dict[str, LabelPipeline] = {"real": real_label_pipeline, "fake": FakePipeline()}


async def _rig(
    db: Session,
    tmp_path: Path,
    pipeline: LabelPipeline,
    vendor_ids: list[str],
    **driver_kw: object,
) -> AsyncIterator[Rig]:
    world = seed(db)
    clock = Clock()
    app = build_app(db, clock, pipeline)
    driver = FakeDriver(
        [FakeTag(v, world.model, battery_mv=3000) for v in vendor_ids],
        output_dir=tmp_path / "fake_tags",
        seed=1,
        **driver_kw,  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://core.test", headers=auth()
    ) as http:
        agent = Agent(
            CoreClient(http),
            driver,
            RenderCache(tmp_path / "gateway_cache"),
            timings=AgentTimings(report_retry_delay_s=0.0),
        )
        yield Rig(db, world, clock, pipeline, driver, agent)


@pytest.fixture(params=list(PIPELINES))
async def rig(db: Session, tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[Rig]:
    async for r in _rig(db, tmp_path, PIPELINES[request.param], ["T1", "T2", "T3", "T4"]):
        yield r


@pytest.fixture(params=list(PIPELINES))
async def failing_rig(
    db: Session, tmp_path: Path, request: pytest.FixtureRequest
) -> AsyncIterator[Rig]:
    async for r in _rig(
        db, tmp_path, PIPELINES[request.param], ["T1"], fail_rate=1.0, fail_with=PushError.TIMEOUT
    ):
        yield r


async def test_price_change_pushes_new_png_to_exactly_the_bound_tags(rig: Rig) -> None:
    # Discovery creates four unbound tags; each shows its "unassigned" label.
    seen = await rig.agent.discover_once()
    assert seen is not None and seen.created == 4
    assert await rig.drain() == 4
    assert all(rig.image(v) for v in ("T1", "T2", "T3", "T4"))

    # Bind T1 and T2 to product A, T3 to product B; T4 stays unbound.
    a, b = rig.world.products
    tags = {v: rig.tag(v) for v in ("T1", "T2", "T3", "T4")}
    for vendor_id, product in (("T1", a), ("T2", a), ("T3", b)):
        bind(rig.db, rig.world, tags[vendor_id].id, product, rig.clock.now)
    refresh_desired_hashes(
        rig.db, rig.pipeline, rig.clock.now, tag_ids=[t.id for t in tags.values()]
    )
    assert await rig.drain() == 3
    before = {v: rig.image(v) for v in tags}
    rig.driver.pushes.clear()

    # A price change on product A.
    rig.clock.now += timedelta(minutes=10)
    set_price(rig.db, a, 1290)
    result = refresh_desired_hashes(rig.db, rig.pipeline, rig.clock.now, product_ids=[a.id])
    assert sorted(result.changed) == sorted([tags["T1"].id, tags["T2"].id])
    assert await rig.drain() == 2

    after = {v: rig.image(v) for v in tags}
    changed = {v for v in tags if after[v] != before[v]}
    assert changed == {"T1", "T2"}
    assert sorted(v for v, ok in rig.driver.pushes if ok) == ["T1", "T2"]  # one push each
    for v in tags:
        tag = rig.tag(v)
        assert tag.displayed_hash == tag.desired_hash
    # The PNG on the tag is exactly the core's render for its desired_hash.
    from core.db.models import RenderRow

    for v in ("T1", "T2"):
        render = rig.db.get(RenderRow, rig.tag(v).desired_hash)
        assert render is not None and render.png == after[v]

    assert await rig.drain() == 0  # nothing more to do
    assert await rig.agent.heartbeat_once()


async def test_injected_failures_retry_with_backoff_and_end_dead_after_6(failing_rig: Rig) -> None:
    rig = failing_rig
    await rig.agent.discover_once()
    tag = rig.tag("T1")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        assert await rig.drain() == 1, f"attempt {attempt} was not claimed"
        [job] = rig.db.scalars(select(UpdateJob).where(UpdateJob.tag_id == tag.id)).all()
        rig.db.refresh(job)
        assert job.attempts == attempt and job.error_code == "timeout"
        if attempt < MAX_ATTEMPTS:
            assert job.state == "failed"
            assert job.next_attempt_at == rig.clock.now + BACKOFF[attempt - 1]
            # Not due yet: nothing to claim one second before the backoff ends.
            rig.clock.now = job.next_attempt_at - timedelta(seconds=1)
            assert await rig.drain() == 0
            rig.clock.now = job.next_attempt_at
    assert job.state == "dead" and job.next_attempt_at is None
    assert len(rig.driver.pushes) == MAX_ATTEMPTS
    assert rig.tag("T1").displayed_hash is None  # timeouts: the tag's image is unknown

    rig.clock.now += timedelta(days=1)
    assert await rig.drain() == 0  # dead stays dead for the same label
    assert len(rig.driver.pushes) == MAX_ATTEMPTS

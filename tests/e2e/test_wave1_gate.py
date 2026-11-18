"""Wave 1 gate (PLAN.md, "Brief for the lead agent"): import a CSV, change a price, and
confirm a new PNG appears for exactly the bound tags.

Everything is real except the radio: the core app (``create_app()`` over
``httpx.ASGITransport`` on the rolled-back test session), the gateway agent, the fake
driver, the CSV import, the pricing rules and the renderer. Time comes from the
``get_now`` override, so nothing sleeps.
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

from core.db.models import Price, Product, RenderRow, Tag, TagTypeRow, UpdateJob
from core.domain.pricing import build_label_data
from core.render import TemplateKey, label_hash, render
from core.render.label import LabelData
from core.services import catalogue as svc
from core.services.gateway_auth import ensure_gateway
from core.services.queue import tag_type_of
from core.services.tags import bind_tag
from gateway.agent import Agent, AgentTimings, CoreClient, RenderCache
from gateway.drivers.fake import FakeDriver, FakeTag
from tests.e2e.support import Clock, auth, build_app, token
from tests.e2e.wave1_support import CountingPipeline, add_fake_tag_type, write_promo
from tests.unit.test_importers_erp_sample_synthetic import SAMPLE, sample_settings

pytestmark = pytest.mark.db

VENDOR_IDS = ("T1", "T2", "T3", "T4")
EAN_A = "3337104332186"  # Αντηλιακή Κρέμα Προσώπου SPF50+, 21,90 € in the fixture
EAN_B = "5205116155940"  # Κρέμα Χεριών Αμύγδαλο, 6,90 € in the fixture


@dataclass
class Gate:
    db: Session
    clock: Clock
    pipeline: CountingPipeline
    driver: FakeDriver
    agent: Agent

    async def drain(self) -> int:
        """Claim, push and report until a claim hands out nothing."""
        total = 0
        while n := await self.agent.process_jobs():
            total += n
        return total

    def tag(self, vendor_id: str) -> Tag:
        tag = self.db.scalars(select(Tag).where(Tag.vendor_id == vendor_id)).one()
        self.db.refresh(tag)
        return tag

    def product(self, ean: str) -> Product:
        return self.db.scalars(select(Product).where(Product.ean == ean)).one()

    def images(self) -> dict[str, bytes]:
        return {v: self.driver.image_path(v).read_bytes() for v in VENDOR_IDS}

    def successful_pushes(self) -> list[str]:
        return sorted(v for v, ok in self.driver.pushes if ok)

    def expected_label(self, ean: str, template: TemplateKey) -> LabelData:
        """The ``LabelData`` the pricing rules give for the product now."""
        product = self.product(ean)
        self.db.refresh(product)
        price = self.db.get_one(Price, product.id)
        self.db.refresh(price)
        return build_label_data(
            svc.product_facts(product),
            svc.price_state(price),
            svc.history_of(self.db, product.id),
            template,
            self.clock.now,
        )

    def assert_shows(self, vendor_id: str, image: bytes, label: LabelData) -> None:
        """The tag's PNG is the core's render for its ``desired_hash``, and that render
        is exactly ``label`` drawn by the renderer."""
        tag = self.tag(vendor_id)
        assert tag.displayed_hash == tag.desired_hash
        row = self.db.get(RenderRow, tag.desired_hash)
        assert row is not None and row.png == image
        tag_type = tag_type_of(self.db.get_one(TagTypeRow, tag.tag_type_id))
        expected = render(label, tag_type)
        assert expected.hash == label_hash(label, tag_type) == tag.desired_hash
        assert expected.png == image


@pytest.fixture
async def gate(db: Session, tmp_path: Path) -> AsyncIterator[Gate]:
    clock = Clock()
    pipeline = CountingPipeline()
    # Step 1: the fake tag type, through the tag-type service.
    tag_type = add_fake_tag_type(db, clock.now)
    ensure_gateway(db, token())
    driver = FakeDriver(
        [FakeTag(v, tag_type.model, battery_mv=3000) for v in VENDOR_IDS],
        output_dir=tmp_path / "fake_tags",
        seed=1,
    )
    app = build_app(db, clock, pipeline)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://core.test", headers=auth()
    ) as http:
        agent = Agent(
            CoreClient(http),
            driver,
            RenderCache(tmp_path / "gateway_cache"),
            timings=AgentTimings(report_retry_delay_s=0.0),
        )
        yield Gate(db, clock, pipeline, driver, agent)


def _edited_sample(tmp_path: Path, ean: str, new_price: str) -> Path:
    """A copy of the fixture with one price changed, kept in Windows-1253 with CRLF."""
    lines = SAMPLE.read_bytes().split(b"\r\n")
    edited = []
    for line in lines:
        if line.startswith(ean.encode("ascii") + b";"):
            cells = line.split(b";")
            cells[3] = new_price.encode("ascii")
            line = b";".join(cells)
        edited.append(line)
    data = b"\r\n".join(edited)
    assert data != SAMPLE.read_bytes()
    path = tmp_path / "erp_sample_synthetic_edited.csv"
    path.write_bytes(data)
    return path


async def test_wave1_gate(gate: Gate, tmp_path: Path) -> None:
    db, clock = gate.db, gate.clock

    # Step 2: discovery reports four tags; each gets and shows its "unassigned" label.
    seen = await gate.agent.discover_once()
    assert seen is not None and seen.created == 4
    assert await gate.drain() == 4
    assert gate.successful_pushes() == list(VENDOR_IDS)
    for v, image in gate.images().items():
        tag = gate.tag(v)
        gate.assert_shows(
            v,
            image,
            LabelData(
                TemplateKey.UNASSIGNED,
                short_code=tag.short_code,
                tag_model=gate.driver.tags[v].model,
            ),
        )

    # Step 3: import the synthetic ERP export with the profile from its .md.
    profile = svc.save_profile(db, "erp-sample-synthetic", sample_settings())
    data = SAMPLE.read_bytes()
    imported = svc.apply_import(
        db, data, SAMPLE.name, profile, actor="test", now=clock.now, pipeline=gate.pipeline
    )
    assert (imported.run.counts["new"], imported.run.counts["rejected"]) == (29, 8)
    assert await gate.drain() == 0  # no tag is bound yet: nothing to push

    # Step 4: bind T1 and T2 to product A, T3 to product B; T4 stays unbound.
    a, b = gate.product(EAN_A), gate.product(EAN_B)
    clock.now += timedelta(minutes=1)
    for v, product in (("T1", a), ("T2", a), ("T3", b)):
        bind_tag(
            db, gate.tag(v).id, product.id, actor="test", now=clock.now, pipeline=gate.pipeline
        )

    # Step 5: the agent pushes the three bound labels.
    gate.driver.pushes.clear()
    assert await gate.drain() == 3
    assert gate.successful_pushes() == ["T1", "T2", "T3"]
    before = gate.images()
    gate.assert_shows("T1", before["T1"], gate.expected_label(EAN_A, TemplateKey.STANDARD))
    gate.assert_shows("T3", before["T3"], gate.expected_label(EAN_B, TemplateKey.STANDARD))
    assert gate.expected_label(EAN_A, TemplateKey.STANDARD).price_text == "21,90 €"

    # Steps 6 and 7, first change: a manual price edit on product A.
    clock.now += timedelta(minutes=10)
    gate.driver.pushes.clear()
    gate.pipeline.calls.clear()
    assert svc.edit_price(db, a.id, 2390, actor="test", now=clock.now, pipeline=gate.pipeline)
    assert sorted(gate.pipeline.calls) == sorted([gate.tag("T1").id, gate.tag("T2").id])
    assert await gate.drain() == 2
    after_edit = _assert_only_a_changed(gate, before)
    label = gate.expected_label(EAN_A, TemplateKey.STANDARD)
    assert label.price_text == "23,90 €"
    for v in ("T1", "T2"):
        gate.assert_shows(v, after_edit[v], label)

    # Second change: re-import an edited copy of the export, still Windows-1253.
    clock.now += timedelta(minutes=10)
    gate.driver.pushes.clear()
    gate.pipeline.calls.clear()
    edited = _edited_sample(tmp_path, EAN_A, "25,40")
    edited_bytes = edited.read_bytes()
    with pytest.raises(UnicodeDecodeError):
        edited_bytes.decode("utf-8")  # still Windows-1253, not re-encoded
    reimported = svc.apply_import(
        db, edited_bytes, edited.name, profile, actor="test", now=clock.now, pipeline=gate.pipeline
    )
    assert (reimported.run.counts["changed"], reimported.run.counts["unchanged"]) == (1, 28)
    assert sorted(gate.pipeline.calls) == sorted([gate.tag("T1").id, gate.tag("T2").id])
    assert await gate.drain() == 2
    after_import = _assert_only_a_changed(gate, after_edit)
    label = gate.expected_label(EAN_A, TemplateKey.STANDARD)
    assert label.price_text == "25,40 €"
    for v in ("T1", "T2"):
        gate.assert_shows(v, after_import[v], label)
    assert len({before["T1"], after_edit["T1"], after_import["T1"]}) == 3

    # Scheduled promo on product B, starting at T + 1 minute.
    clock.now += timedelta(minutes=10)
    start = clock.now + timedelta(minutes=1)
    end = start + timedelta(days=7)
    gate.driver.pushes.clear()
    refreshed = write_promo(
        db, b.id, 590, start, end, prior_cents=690, now=clock.now, pipeline=gate.pipeline
    )
    assert refreshed.changed == [] and refreshed.next_change == start  # not yet shown
    assert await gate.drain() == 0
    clock.now = start - timedelta(seconds=1)
    gate.pipeline.calls.clear()
    assert await gate.drain() == 0
    assert gate.pipeline.calls == []  # before the start: no refresh

    clock.now = start + timedelta(seconds=30)  # the first claim after the start
    assert await gate.drain() == 1
    assert gate.pipeline.calls == [gate.tag("T3").id]  # rendered once, on the claim
    assert gate.successful_pushes() == ["T3"]
    after_promo = gate.images()
    assert {v for v in VENDOR_IDS if after_promo[v] != after_import[v]} == {"T3"}
    promo = gate.expected_label(EAN_B, TemplateKey.PROMO)
    assert (promo.template, promo.price_text, promo.prior_price_text) == (
        TemplateKey.PROMO,
        "5,90 €",
        "6,90 €",
    )
    gate.assert_shows("T3", after_promo["T3"], promo)

    clock.now += timedelta(minutes=1)  # a second claim renders nothing again
    assert await gate.drain() == 0
    assert gate.pipeline.calls == [gate.tag("T3").id]

    # The promo ends: T3 goes back to exactly its earlier regular label.
    gate.driver.pushes.clear()
    clock.now = end + timedelta(seconds=30)
    assert await gate.drain() == 1
    assert gate.successful_pushes() == ["T3"]
    after_end = gate.images()
    assert after_end["T3"] == after_import["T3"]
    assert {v: after_end[v] for v in ("T1", "T2", "T4")} == {
        v: after_promo[v] for v in ("T1", "T2", "T4")
    }

    # Nothing is left in the queue, and every tag shows what it should.
    assert await gate.drain() == 0
    active = db.scalars(select(UpdateJob).where(UpdateJob.state.in_(["queued", "failed"]))).all()
    assert active == []
    for v in VENDOR_IDS:
        tag = gate.tag(v)
        assert tag.displayed_hash == tag.desired_hash


def _assert_only_a_changed(gate: Gate, before: dict[str, bytes]) -> dict[str, bytes]:
    """New PNGs for exactly T1 and T2, one push each; T3 and T4 byte-identical."""
    after = gate.images()
    assert {v for v in VENDOR_IDS if after[v] != before[v]} == {"T1", "T2"}
    assert after["T3"] == before["T3"] and after["T4"] == before["T4"]
    assert gate.successful_pushes() == ["T1", "T2"]
    assert len(gate.driver.pushes) == 2
    return after

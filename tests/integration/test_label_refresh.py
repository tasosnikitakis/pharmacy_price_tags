"""Wave 1 wiring: catalogue writes refresh the labels of bound tags, scheduled promo
starts and ends are picked up on the gateway claim, and the minimal binding helper."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.db.models import (
    Binding,
    EventLog,
    Price,
    PriceHistory,
    Product,
    Tag,
    TagTypeRow,
    Template,
    UpdateJob,
)
from core.domain.pricing import build_label_data, unassigned_label_data
from core.render import TemplateKey, label_hash
from core.render.label import TEMPLATE_VERSIONS
from core.services import catalogue as svc
from core.services.gateway_auth import ensure_gateway
from core.services.queue import tag_type_of
from core.services.tags import (
    BindingError,
    LabelOutcome,
    LabelRequest,
    bind_tag,
    promo_boundary_tags,
    record_seen_tags,
    template_row,
)
from shared.gateway_api import API_PREFIX, SeenTagReport
from tests.e2e.support import Clock, auth, build_app, token
from tests.e2e.wave1_support import CountingPipeline, add_fake_tag_type, write_promo
from tests.unit.test_importers_erp_sample_synthetic import SAMPLE, sample_settings

pytestmark = pytest.mark.db

EAN_A = "3337104332186"
EAN_B = "5205116155940"


@dataclasses.dataclass
class Shop:
    db: Session
    clock: Clock
    pipeline: CountingPipeline
    tag_type: TagTypeRow
    profile_id: int

    def discover(self, *vendor_ids: str) -> list[Tag]:
        reports = [
            SeenTagReport(
                vendor_id=v, vendor="fake", model=self.tag_type.model, seen_at=self.clock.now
            )
            for v in vendor_ids
        ]
        record_seen_tags(self.db, reports, self.pipeline, self.clock.now)
        return [self.tag(v) for v in vendor_ids]

    def tag(self, vendor_id: str) -> Tag:
        tag = self.db.scalars(select(Tag).where(Tag.vendor_id == vendor_id)).one()
        self.db.refresh(tag)
        return tag

    def product(self, ean: str) -> Product:
        return self.db.scalars(select(Product).where(Product.ean == ean)).one()

    def bind(self, vendor_id: str, ean: str) -> None:
        bind_tag(
            self.db,
            self.tag(vendor_id).id,
            self.product(ean).id,
            actor="test",
            now=self.clock.now,
            pipeline=self.pipeline,
        )

    def active_jobs(self, vendor_id: str) -> list[UpdateJob]:
        return list(
            self.db.scalars(
                select(UpdateJob).where(
                    UpdateJob.tag_id == self.tag(vendor_id).id,
                    UpdateJob.state.in_(["queued", "claimed", "failed"]),
                )
            )
        )

    def import_sample(self, data: bytes | None = None) -> svc.ImportResult:
        profile = svc.get_profile(self.db, "erp-sample-synthetic")
        assert profile is not None
        return svc.apply_import(
            self.db,
            SAMPLE.read_bytes() if data is None else data,
            SAMPLE.name,
            profile,
            actor="test",
            now=self.clock.now,
            pipeline=self.pipeline,
        )


@pytest.fixture
def shop(db: Session) -> Shop:
    clock = Clock()
    tag_type = add_fake_tag_type(db, clock.now)
    profile = svc.save_profile(db, "erp-sample-synthetic", sample_settings())
    ensure_gateway(db, token())
    s = Shop(db, clock, CountingPipeline(), tag_type, profile.id)
    s.import_sample()
    return s


def _sample_with_price(ean: str, price: str) -> bytes:
    lines = SAMPLE.read_bytes().split(b"\r\n")
    out = []
    for line in lines:
        if line.startswith(ean.encode("ascii") + b";"):
            cells = line.split(b";")
            cells[3] = price.encode("ascii")
            line = b";".join(cells)
        out.append(line)
    return b"\r\n".join(out)


# --- Catalogue writes refresh labels -------------------------------------------------


def test_edit_price_refreshes_exactly_the_bound_tags(shop: Shop) -> None:
    shop.discover("T1", "T2", "T3", "T4")
    shop.bind("T1", EAN_A)
    shop.bind("T2", EAN_A)
    shop.bind("T3", EAN_B)
    before = {v: shop.tag(v).desired_hash for v in ("T1", "T2", "T3", "T4")}
    shop.pipeline.calls.clear()
    shop.clock.now += timedelta(minutes=5)

    assert svc.edit_price(
        shop.db,
        shop.product(EAN_A).id,
        2390,
        actor="test",
        now=shop.clock.now,
        pipeline=shop.pipeline,
    )

    t1, t2 = shop.tag("T1"), shop.tag("T2")
    assert sorted(shop.pipeline.calls) == sorted([t1.id, t2.id])
    for tag in (t1, t2):
        assert tag.desired_hash != before[tag.vendor_id]
        assert tag.desired_hash_changed_at == shop.clock.now
        [job] = shop.active_jobs(tag.vendor_id)
        assert job.render_hash == tag.desired_hash and job.state == "queued"
    assert shop.tag("T3").desired_hash == before["T3"]
    assert shop.tag("T4").desired_hash == before["T4"]


def test_edit_price_that_changes_nothing_renders_nothing(shop: Shop) -> None:
    shop.discover("T1")
    shop.bind("T1", EAN_A)
    shop.pipeline.calls.clear()
    product = shop.product(EAN_A)
    assert (
        svc.edit_price(
            shop.db, product.id, 2190, actor="test", now=shop.clock.now, pipeline=shop.pipeline
        )
        is None
    )
    assert shop.pipeline.calls == []


def test_edit_product_refreshes_its_tags(shop: Shop) -> None:
    shop.discover("T1", "T3")
    shop.bind("T1", EAN_A)
    shop.bind("T3", EAN_B)
    old = shop.tag("T1").desired_hash
    product = shop.product(EAN_A)
    shop.pipeline.calls.clear()

    fields = dataclasses.replace(svc.ProductFields.of(product), name="Νέο όνομα")
    svc.edit_product(
        shop.db, product.id, fields, actor="test", now=shop.clock.now, pipeline=shop.pipeline
    )
    assert shop.pipeline.calls == [shop.tag("T1").id]
    assert shop.tag("T1").desired_hash != old

    shop.pipeline.calls.clear()  # the same fields again: no write, no render
    svc.edit_product(
        shop.db, product.id, fields, actor="test", now=shop.clock.now, pipeline=shop.pipeline
    )
    assert shop.pipeline.calls == []


def test_import_refreshes_only_the_changed_products(shop: Shop) -> None:
    shop.discover("T1", "T3", "T4")
    shop.bind("T1", EAN_A)
    shop.bind("T3", EAN_B)
    before = {v: shop.tag(v).desired_hash for v in ("T1", "T3", "T4")}
    shop.pipeline.calls.clear()
    shop.clock.now += timedelta(hours=1)

    result = shop.import_sample(_sample_with_price(EAN_A, "25,40"))

    assert result.run.counts["changed"] == 1
    assert shop.pipeline.calls == [shop.tag("T1").id]
    assert shop.tag("T1").desired_hash != before["T1"]
    assert [j.render_hash for j in shop.active_jobs("T1")] == [shop.tag("T1").desired_hash]
    assert shop.tag("T3").desired_hash == before["T3"]
    assert shop.tag("T4").desired_hash == before["T4"]

    shop.pipeline.calls.clear()  # re-importing the same file changes and renders nothing
    shop.import_sample(_sample_with_price(EAN_A, "25,40"))
    assert shop.pipeline.calls == []


def test_default_pipeline_is_the_real_renderer(shop: Shop) -> None:
    shop.discover("T1")
    shop.bind("T1", EAN_A)
    product = shop.product(EAN_A)
    svc.edit_price(shop.db, product.id, 1999, actor="test", now=shop.clock.now)
    shop.db.refresh(product)
    price = shop.db.get_one(Price, product.id)
    shop.db.refresh(price)
    label = build_label_data(
        svc.product_facts(product),
        svc.price_state(price),
        svc.history_of(shop.db, product.id),
        TemplateKey.STANDARD,
        shop.clock.now,
    )
    assert label.price_text == "19,99 €"
    assert shop.tag("T1").desired_hash == label_hash(label, tag_type_of(shop.tag_type))


def test_a_refresh_that_fails_rolls_back_the_whole_edit(shop: Shop) -> None:
    shop.discover("T1")
    shop.bind("T1", EAN_A)
    product = shop.product(EAN_A)
    history = shop.db.scalar(select(func.count()).select_from(PriceHistory))

    def broken(request: LabelRequest, now: datetime) -> LabelOutcome:
        raise RuntimeError("renderer crashed")

    with pytest.raises(RuntimeError):
        svc.edit_price(shop.db, product.id, 2390, actor="test", now=shop.clock.now, pipeline=broken)
    price = shop.db.get_one(Price, product.id)
    shop.db.refresh(price)
    assert price.regular_cents == 2190
    assert shop.db.scalar(select(func.count()).select_from(PriceHistory)) == history


# --- Bindings -------------------------------------------------------------------------


def test_bind_tag_binds_rebinds_logs_and_refreshes(shop: Shop) -> None:
    [tag] = shop.discover("T1")
    unassigned = unassigned_label_data(tag.short_code, shop.tag_type.model)
    assert tag.desired_hash == label_hash(unassigned, tag_type_of(shop.tag_type))

    shop.bind("T1", EAN_A)
    bound_a = shop.tag("T1").desired_hash
    shop.clock.now += timedelta(minutes=1)
    shop.bind("T1", EAN_B)  # rebinding moves the tag
    tag = shop.tag("T1")
    assert tag.desired_hash not in (bound_a, None)
    [binding] = shop.db.scalars(select(Binding).where(Binding.tag_id == tag.id)).all()
    assert binding.product_id == shop.product(EAN_B).id
    template = shop.db.get_one(Template, binding.template_id)
    assert (template.key, template.version) == ("standard", TEMPLATE_VERSIONS[TemplateKey.STANDARD])
    events = shop.db.scalars(
        select(EventLog).where(EventLog.kind == "tag.bound").order_by(EventLog.id)
    ).all()
    assert [e.payload["previous_product_id"] for e in events] == [None, shop.product(EAN_A).id]
    [job] = shop.active_jobs("T1")
    assert job.render_hash == tag.desired_hash


def test_bind_tag_refuses_unknown_rows_and_the_unassigned_template(shop: Shop) -> None:
    [tag] = shop.discover("T1")
    product = shop.product(EAN_A)
    now = shop.clock.now
    with pytest.raises(BindingError):
        bind_tag(
            shop.db, tag.id, product.id, actor="test", now=now, template=TemplateKey.UNASSIGNED
        )
    with pytest.raises(BindingError):
        bind_tag(shop.db, tag.id + 10_000, product.id, actor="test", now=now)
    with pytest.raises(BindingError):
        bind_tag(shop.db, tag.id, product.id + 10_000, actor="test", now=now)


def test_template_row_is_created_once(shop: Shop) -> None:
    first = template_row(shop.db, shop.tag_type.id, TemplateKey.PROMO)
    second = template_row(shop.db, shop.tag_type.id, TemplateKey.PROMO)
    assert first.id == second.id


# --- Scheduled promo starts and ends, on the claim ------------------------------------


@pytest.fixture
def client(shop: Shop) -> Iterator[TestClient]:
    yield TestClient(build_app(shop.db, shop.clock, shop.pipeline), headers=auth())


def _claim(client: TestClient) -> list[dict[str, object]]:
    r = client.post(f"{API_PREFIX}/jobs/claim", json={"max_jobs": 10})
    assert r.status_code == 200, r.text
    jobs: list[dict[str, object]] = r.json()["jobs"]
    return jobs


def _ack_all(client: TestClient) -> None:
    for job in _claim(client):
        r = client.post(
            f"{API_PREFIX}/jobs/{job['job_id']}/result",
            json={"attempt": job["attempt"], "outcome": "acked", "duration_ms": 10},
        )
        assert r.status_code == 200, r.text


def _shown(shop: Shop, client: TestClient, *bindings: tuple[str, str]) -> None:
    """Discover, bind and acknowledge, so every tag shows its label."""
    shop.discover(*(v for v, _ in bindings))
    for v, ean in bindings:
        shop.bind(v, ean)
    _ack_all(client)
    for v, _ in bindings:
        tag = shop.tag(v)
        assert tag.displayed_hash == tag.desired_hash


def test_promo_start_switches_the_label_on_the_first_claim_after_it(
    shop: Shop, client: TestClient
) -> None:
    _shown(shop, client, ("T1", EAN_A), ("T3", EAN_B))
    start = shop.clock.now + timedelta(hours=1)
    refreshed = write_promo(
        shop.db,
        shop.product(EAN_B).id,
        590,
        start,
        start + timedelta(days=3),
        prior_cents=690,
        now=shop.clock.now,
        pipeline=shop.pipeline,
    )
    assert refreshed.changed == []
    regular = shop.tag("T3").desired_hash
    shop.pipeline.calls.clear()

    shop.clock.now = start - timedelta(seconds=1)  # just before the start
    assert _claim(client) == []
    assert shop.pipeline.calls == []
    assert promo_boundary_tags(shop.db, shop.clock.now) == []

    shop.clock.now = start  # promo_from is inclusive
    assert promo_boundary_tags(shop.db, shop.clock.now) == [shop.tag("T3").id]
    [job] = _claim(client)
    t3 = shop.tag("T3")
    assert shop.pipeline.calls == [t3.id]
    assert t3.desired_hash != regular and t3.desired_hash_changed_at == start
    assert job["render_hash"] == t3.desired_hash and job["vendor_id"] == "T3"

    shop.clock.now += timedelta(minutes=1)  # the next claim does not render again
    assert _claim(client) == []
    assert shop.pipeline.calls == [t3.id]
    assert promo_boundary_tags(shop.db, shop.clock.now) == []


def test_promo_end_switches_the_label_back_on_the_first_claim_after_it(
    shop: Shop, client: TestClient
) -> None:
    _shown(shop, client, ("T1", EAN_A), ("T3", EAN_B))
    regular = shop.tag("T3").desired_hash
    end = shop.clock.now + timedelta(hours=2)
    refreshed = write_promo(
        shop.db,
        shop.product(EAN_B).id,
        590,
        shop.clock.now,
        end,
        prior_cents=690,
        now=shop.clock.now,
        pipeline=shop.pipeline,
    )
    assert refreshed.changed == [shop.tag("T3").id]  # a promo "from now" shows at once
    _ack_all(client)
    promo = shop.tag("T3").desired_hash
    assert promo != regular
    shop.pipeline.calls.clear()

    shop.clock.now = end - timedelta(seconds=1)
    assert _claim(client) == []
    assert shop.pipeline.calls == []

    shop.clock.now = end  # promo_to is exclusive: the promo is over
    [job] = _claim(client)
    t3 = shop.tag("T3")
    assert shop.pipeline.calls == [t3.id]
    assert t3.desired_hash == regular == job["render_hash"]

    shop.clock.now += timedelta(minutes=1)
    assert _claim(client) == []
    assert shop.pipeline.calls == [t3.id]


def test_promo_that_starts_and_ends_between_two_claims_renders_nothing(
    shop: Shop, client: TestClient
) -> None:
    # The gateway was offline for the whole promo: the label is unchanged, and the
    # tag must not be rendered again on every claim from now on.
    _shown(shop, client, ("T3", EAN_B))
    start = shop.clock.now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    write_promo(
        shop.db,
        shop.product(EAN_B).id,
        590,
        start,
        end,
        prior_cents=690,
        now=shop.clock.now,
        pipeline=shop.pipeline,
    )
    shop.pipeline.calls.clear()
    shop.clock.now = end + timedelta(minutes=5)
    assert promo_boundary_tags(shop.db, shop.clock.now) == []
    assert _claim(client) == []
    assert shop.pipeline.calls == []


def test_promo_boundary_ignores_unbound_tags_and_tags_without_a_label(shop: Shop) -> None:
    shop.discover("T1", "T3")
    shop.bind("T3", EAN_B)
    start = shop.clock.now + timedelta(minutes=1)
    write_promo(
        shop.db,
        shop.product(EAN_B).id,
        590,
        start,
        start + timedelta(days=1),
        prior_cents=690,
        now=shop.clock.now,
        pipeline=shop.pipeline,
    )
    later = start + timedelta(seconds=1)
    assert promo_boundary_tags(shop.db, later) == [shop.tag("T3").id]
    t3 = shop.tag("T3")
    t3.desired_hash_changed_at = None  # as if no label was ever built
    shop.db.flush()
    assert promo_boundary_tags(shop.db, later) == []

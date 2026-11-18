"""Lock order across catalogue writes, label refreshes and claims, on real connections.

Needs committed rows seen by two connections, so it runs in a throwaway database
(the fixtures of ``test_gateway_api_concurrency``).
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core.db.models import GatewayRow, Tag, UpdateJob
from core.services import catalogue as svc
from core.services.queue import claim_jobs, report_result
from core.services.tags import (
    bind_tag,
    real_label_pipeline,
    record_seen_tags,
    refresh_promo_boundaries,
)
from shared.gateway_api import JobOutcome, SeenTagReport
from tests.e2e.support import T0, seed
from tests.e2e.wave1_support import write_promo
from tests.integration import test_gateway_api_concurrency as queue_tests

pytestmark = pytest.mark.db

START = T0 + timedelta(minutes=1)

# The throwaway database of the queue concurrency tests.
scratch_engine = queue_tests.scratch_engine
factory = queue_tests.factory


def _claim_and_ack(s: Session, gateway_id: int) -> list[int]:
    jobs = claim_jobs(s, gateway_id, 10, T0)
    for job in jobs:
        report_result(s, gateway_id, job.job_id, job.attempt, JobOutcome.ACKED, None, T0)
    return [job.job_id for job in jobs]


def _setup(factory: sessionmaker[Session]) -> tuple[int, int, int]:
    """One tag bound to a product whose promo starts at ``START``; label shown."""
    with factory() as s:
        world = seed(s)
        gateway_id = s.scalars(select(GatewayRow.id).order_by(GatewayRow.id)).first()
        assert gateway_id is not None
        report = SeenTagReport(vendor_id="LOCK-1", vendor="fake", model=world.model, seen_at=T0)
        record_seen_tags(s, [report], real_label_pipeline, T0)
        tag = s.scalars(select(Tag).where(Tag.vendor_id == "LOCK-1")).one()
        product = world.products[0]
        bind_tag(s, tag.id, product.id, actor="test", now=T0)
        _claim_and_ack(s, gateway_id)
        write_promo(
            s,
            product.id,
            300,
            START,
            START + timedelta(days=2),
            prior_cents=450,
            now=T0,
            pipeline=real_label_pipeline,
        )
        s.commit()
        return gateway_id, tag.id, product.id


def test_claim_skips_a_locked_tag_and_picks_it_up_later(factory: sessionmaker[Session]) -> None:
    gateway_id, tag_id, _ = _setup(factory)
    now = START + timedelta(seconds=5)
    with factory() as holder:
        # Another transaction holds the tag, as an import does while it renders.
        holder.scalars(select(Tag).where(Tag.id == tag_id).with_for_update(key_share=True)).one()
        done = threading.Event()
        claimed: list[int] = []

        def claim() -> None:
            with factory() as s:
                assert refresh_promo_boundaries(s, real_label_pipeline, now).changed == []
                claimed.extend(j.job_id for j in claim_jobs(s, gateway_id, 10, now))
                s.commit()
            done.set()

        thread = threading.Thread(target=claim)
        thread.start()
        assert done.wait(5), "the claim waited for the tag lock"
        thread.join()
        assert claimed == []
        holder.rollback()

    with factory() as s:  # the next claim refreshes it
        assert refresh_promo_boundaries(s, real_label_pipeline, now).changed == [tag_id]
        [job] = claim_jobs(s, gateway_id, 10, now)
        s.commit()
        tag = s.get_one(Tag, tag_id)
        assert job.render_hash == tag.desired_hash


def test_price_edit_and_claim_on_the_same_tag_do_not_deadlock(
    factory: sessionmaker[Session],
) -> None:
    # An edit holds product, price, tag, then job locks; a claim at the promo start
    # wants the same tag. Whatever the timing, both finish without a deadlock and the
    # tag's current label (the promo, which the higher regular price leaves valid) is
    # queued or claimed exactly once.
    gateway_id, tag_id, product_id = _setup(factory)
    now = START + timedelta(seconds=5)
    errors: list[BaseException] = []

    def edit() -> None:
        try:
            with factory() as s:
                svc.edit_price(s, product_id, 990, actor="test", now=now)
                s.commit()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def claim() -> None:
        try:
            with factory() as s:
                refresh_promo_boundaries(s, real_label_pipeline, now)
                claim_jobs(s, gateway_id, 10, now)
                s.commit()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=edit), threading.Thread(target=claim)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == []
    assert not any(t.is_alive() for t in threads)
    with factory() as s:  # the queue is consistent: the current label is on its way
        tag = s.get_one(Tag, tag_id)
        jobs = s.scalars(
            select(UpdateJob).where(
                UpdateJob.tag_id == tag_id, UpdateJob.state.in_(["queued", "claimed"])
            )
        ).all()
        assert tag.desired_hash is not None
        assert tag.desired_hash != tag.displayed_hash  # the promo label is not shown yet
        assert [j.render_hash for j in jobs] == [tag.desired_hash]

"""Lock order across catalogue writes, label refreshes and claims, on real connections.

Needs committed rows seen by two connections, so it runs in a throwaway database
(the fixtures of ``test_gateway_api_concurrency``).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from core.db.models import GatewayRow, Price, Tag, TagTypeRow, UpdateJob
from core.services import catalogue as svc
from core.services.queue import claim_jobs, report_result
from core.services.tag_types import add_tag_type
from core.services.tags import (
    bind_tag,
    real_label_pipeline,
    record_seen_tags,
    refresh_desired_hashes,
    refresh_promo_boundaries,
)
from shared.gateway_api import JobOutcome, SeenTagReport
from tests.e2e.support import T0, seed
from tests.e2e.wave1_support import fake_tag_type, write_promo
from tests.integration import test_gateway_api_concurrency as queue_tests

pytestmark = pytest.mark.db

START = T0 + timedelta(minutes=1)

# The throwaway database of the queue concurrency tests.
scratch_engine = queue_tests.scratch_engine
factory = queue_tests.factory


def _lock_waiters(engine: Engine) -> int:
    with engine.connect() as conn:
        count = conn.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        )
        return int(count or 0)


def _wait_until_blocked(engine: Engine, done: threading.Event) -> None:
    """Wait until another connection is waiting for a row lock (the contended case)."""
    deadline = time.monotonic() + 5
    while _lock_waiters(engine) == 0:
        assert not done.is_set(), "the other transaction did not wait for the lock"
        assert time.monotonic() < deadline, "no transaction waited for a lock"
        time.sleep(0.02)
    assert not done.is_set()


def _in_thread(
    factory: sessionmaker[Session], work: Callable[[Session], object]
) -> tuple[threading.Thread, threading.Event, list[BaseException]]:
    """Run ``work`` in its own session and commit, in a thread."""
    done = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            with factory() as s:
                work(s)
                s.commit()
        except BaseException as exc:  # pragma: no cover - reported by the test
            errors.append(exc)
        done.set()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, done, errors


def _tag_id(factory: sessionmaker[Session], vendor_id: str) -> int:
    with factory() as s:
        return s.scalars(select(Tag.id).where(Tag.vendor_id == vendor_id)).one()


def _label_is_current(factory: sessionmaker[Session], tag_id: int, now: datetime) -> bool:
    """True when the tag's desired_hash is the label for the committed catalogue."""
    with factory() as s:
        changed = refresh_desired_hashes(s, real_label_pipeline, now, tag_ids=[tag_id])
        s.rollback()
        return changed.changed == []


def _claim_and_ack(s: Session, gateway_id: int) -> list[int]:
    jobs = claim_jobs(s, gateway_id, 10, T0)
    for job in jobs:
        report_result(s, gateway_id, job.job_id, job.attempt, JobOutcome.ACKED, None, T0)
    return [job.job_id for job in jobs]


def _setup(factory: sessionmaker[Session]) -> tuple[int, int, int]:
    """Tag LOCK-1 bound to a product whose promo starts at ``START``, its label shown;
    tag LOCK-2 discovered and unbound."""
    with factory() as s:
        world = seed(s)
        gateway_id = s.scalars(select(GatewayRow.id).order_by(GatewayRow.id)).first()
        assert gateway_id is not None
        reports = [
            SeenTagReport(vendor_id=v, vendor="fake", model=world.model, seen_at=T0)
            for v in ("LOCK-1", "LOCK-2")
        ]
        record_seen_tags(s, reports, real_label_pipeline, T0)
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


def test_edit_and_claim_contending_for_the_same_tag_both_finish(
    factory: sessionmaker[Session], scratch_engine: Engine
) -> None:
    # The claim refreshes the tag at the promo start and holds its lock; an edit of the
    # product then waits for that tag (after taking product and price locks). The claim
    # takes no product or price lock, so it finishes, and so does the edit.
    gateway_id, tag_id, product_id = _setup(factory)
    now = START + timedelta(seconds=5)
    with factory() as claimer:
        assert refresh_promo_boundaries(claimer, real_label_pipeline, now).changed == [tag_id]
        thread, done, errors = _in_thread(
            factory, lambda s: svc.edit_price(s, product_id, 990, actor="test", now=now)
        )
        _wait_until_blocked(scratch_engine, done)
        [job] = claim_jobs(claimer, gateway_id, 10, now)
        claimer.commit()
    thread.join(10)
    assert done.is_set() and errors == []
    with factory() as s:
        tag = s.get_one(Tag, tag_id)
        assert s.get_one(Price, product_id).regular_cents == 990
        active = s.scalars(
            select(UpdateJob).where(
                UpdateJob.tag_id == tag_id, UpdateJob.state.in_(["queued", "claimed"])
            )
        ).all()
        # The higher regular price leaves the promo label as it was: one claimed job.
        assert [(j.id, j.state, j.render_hash) for j in active] == [
            (job.job_id, "claimed", tag.desired_hash)
        ]
    assert _label_is_current(factory, tag_id, now)


def test_bind_waits_for_an_open_price_edit_then_shows_the_new_price(
    factory: sessionmaker[Session], scratch_engine: Engine
) -> None:
    _, _, product_id = _setup(factory)
    tag_id = _tag_id(factory, "LOCK-2")
    now = T0 + timedelta(seconds=30)
    with factory() as editor:
        svc.edit_price(editor, product_id, 777, actor="test", now=now)  # not committed
        thread, done, errors = _in_thread(
            factory, lambda s: bind_tag(s, tag_id, product_id, actor="test", now=now)
        )
        _wait_until_blocked(scratch_engine, done)
        editor.commit()
    thread.join(10)
    assert done.is_set() and errors == []
    assert _label_is_current(factory, tag_id, now)  # 7,77 €, not the old 4,50 €
    with factory() as s:
        tag = s.get_one(Tag, tag_id)
        jobs = s.scalars(
            select(UpdateJob).where(UpdateJob.tag_id == tag_id, UpdateJob.state == "queued")
        ).all()
        assert [j.render_hash for j in jobs] == [tag.desired_hash]


def test_price_edit_waits_for_an_open_bind_then_refreshes_the_tag(
    factory: sessionmaker[Session], scratch_engine: Engine
) -> None:
    _, _, product_id = _setup(factory)
    tag_id = _tag_id(factory, "LOCK-2")
    now = T0 + timedelta(seconds=30)
    with factory() as binder:
        bind_tag(binder, tag_id, product_id, actor="test", now=now)  # not committed
        thread, done, errors = _in_thread(
            factory, lambda s: svc.edit_price(s, product_id, 777, actor="test", now=now)
        )
        _wait_until_blocked(scratch_engine, done)
        binder.commit()
    thread.join(10)
    assert done.is_set() and errors == []
    assert _label_is_current(factory, tag_id, now)


def test_rebind_waits_for_an_open_write_on_the_product_it_leaves(
    factory: sessionmaker[Session], scratch_engine: Engine
) -> None:
    _, tag_id, product_id = _setup(factory)
    with factory() as s:
        other = s.scalars(
            select(Price.product_id)
            .where(Price.product_id != product_id)
            .order_by(Price.product_id.desc())
        ).first()
    assert other is not None
    now = T0 + timedelta(seconds=30)
    with factory() as editor:
        svc.edit_price(editor, product_id, 777, actor="test", now=now)  # not committed
        thread, done, errors = _in_thread(
            factory, lambda s: bind_tag(s, tag_id, other, actor="test", now=now)
        )
        _wait_until_blocked(scratch_engine, done)
        editor.commit()
    thread.join(10)
    assert done.is_set() and errors == []
    assert _label_is_current(factory, tag_id, now)


def test_concurrent_identical_tag_type_adds_both_succeed(
    factory: sessionmaker[Session], scratch_engine: Engine
) -> None:
    tag_type = fake_tag_type()
    results: list[bool] = []
    with factory() as first:
        assert add_tag_type(first, tag_type, actor="test", now=T0).created
        thread, done, errors = _in_thread(
            factory,
            lambda s: results.append(add_tag_type(s, tag_type, actor="test", now=T0).created),
        )
        _wait_until_blocked(scratch_engine, done)  # waits on the unique index
        first.commit()
    thread.join(10)
    assert done.is_set() and errors == [] and results == [False]
    with factory() as s:
        rows = s.scalars(select(TagTypeRow).where(TagTypeRow.key == tag_type.key)).all()
        assert len(rows) == 1

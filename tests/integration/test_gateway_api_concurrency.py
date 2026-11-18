"""Claiming under concurrency: ``SELECT ... FOR UPDATE SKIP LOCKED`` on real connections.

These tests need committed data seen by several connections, so they run in a
throwaway database instead of the rolled-back ``db`` session.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from core.db.models import GatewayRow, Product, Tag, UpdateJob
from core.services.queue import claim_jobs
from core.services.tags import record_seen_tags, refresh_desired_hashes
from shared.gateway_api import SeenTagReport
from tests.conftest import alembic_config
from tests.e2e.support import T0, FakePipeline, World, bind, seed, set_price

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def scratch_engine(database_url: str) -> Iterator[Engine]:
    base = make_url(database_url)
    name = f"esl_queuetest_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "head")
        engine = create_engine(url, pool_size=8)
        yield engine
        engine.dispose()
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def factory(scratch_engine: Engine) -> Iterator[sessionmaker[Session]]:
    with scratch_engine.begin() as conn:
        conn.execute(text("DELETE FROM update_job; DELETE FROM binding; DELETE FROM tag"))
    yield sessionmaker(scratch_engine, expire_on_commit=False)


def _setup(factory: sessionmaker[Session], n_tags: int) -> tuple[int, list[int], World]:
    """Seed ``n_tags`` discovered tags, each with a queued job."""
    with factory() as s:
        world = seed(s)
        gateway_id = s.scalars(select(GatewayRow.id).order_by(GatewayRow.id)).first()
        reports = [
            SeenTagReport(
                vendor_id=f"C-{uuid.uuid4().hex[:6]}-{i}",
                vendor="fake",
                model=world.model,
                seen_at=datetime.now(UTC),
            )
            for i in range(n_tags)
        ]
        record_seen_tags(s, reports, FakePipeline(), T0)
        s.commit()
        job_ids = list(s.scalars(select(UpdateJob.id).where(UpdateJob.state == "queued")))
    assert gateway_id is not None and len(job_ids) == n_tags
    return gateway_id, job_ids, world


def test_two_claims_never_get_the_same_job(factory: sessionmaker[Session]) -> None:
    gateway_id, job_ids, _ = _setup(factory, 4)
    with factory() as a, factory() as b:
        first = claim_jobs(a, gateway_id, 2, T0)  # locks held: not committed yet
        b.execute(text("SET LOCAL lock_timeout = '2s'"))  # a wait would fail, not hang
        second = claim_jobs(b, gateway_id, 10, T0)
        a.commit()
        b.commit()
    got_a, got_b = {j.job_id for j in first}, {j.job_id for j in second}
    assert len(got_a) == 2 and not got_a & got_b
    assert got_a | got_b == set(job_ids)


def test_parallel_claims_hand_each_job_out_once(factory: sessionmaker[Session]) -> None:
    gateway_id, job_ids, _ = _setup(factory, 8)
    barrier = threading.Barrier(8)

    def worker(_: int) -> list[int]:
        with factory() as s:
            barrier.wait()
            claimed = claim_jobs(s, gateway_id, 1, T0)
            s.commit()
            return [j.job_id for j in claimed]

    with ThreadPoolExecutor(8) as pool:
        results = [j for jobs in pool.map(worker, range(8)) for j in jobs]
    assert len(results) == len(set(results))
    with factory() as s:
        states = s.execute(
            select(UpdateJob.state, UpdateJob.attempts).where(UpdateJob.id.in_(results))
        ).all()
    assert all(state == ("claimed", 1) for state in states)
    # Whatever was skipped while locked is still queued and claimable.
    with factory() as s:
        rest = claim_jobs(s, gateway_id, 10, T0)
        s.commit()
    assert set(results) | {j.job_id for j in rest} == set(job_ids)


def test_reconcile_waits_for_a_claim_and_never_cancels_it(factory: sessionmaker[Session]) -> None:
    gateway_id, _, world = _setup(factory, 1)
    product = world.products[0]
    pipeline = FakePipeline()
    with factory() as s:
        tag = s.scalars(select(Tag)).one()
        bind(s, world, tag.id, product, T0)
        refresh_desired_hashes(s, pipeline, T0, tag_ids=[tag.id])
        s.commit()

    with factory() as claimer:
        [job] = claim_jobs(claimer, gateway_id, 1, T0)  # holds the job's row lock

        done = threading.Event()

        def change_price() -> None:
            with factory() as s:
                p = s.get(Product, product.id)
                assert p is not None
                set_price(s, p, 1234)
                refresh_desired_hashes(s, pipeline, T0, product_ids=[product.id])
                s.commit()
            done.set()

        thread = threading.Thread(target=change_price)
        thread.start()
        assert not done.wait(0.5)  # the reconcile is waiting for the claim's lock
        claimer.commit()
    thread.join(5)
    assert done.is_set()
    with factory() as s:
        row = s.get(UpdateJob, job.job_id)
        assert row is not None and row.state == "claimed"  # not cancelled
        active = s.scalars(
            select(UpdateJob).where(UpdateJob.tag_id == row.tag_id, UpdateJob.state == "queued")
        ).all()
        assert active == []  # the new label waits for the claim's result

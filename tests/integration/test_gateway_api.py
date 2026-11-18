"""Gateway API on the core (PLAN.md section 3) against Postgres, with a fake renderer."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.db.models import EventLog, GatewayRow, Tag, UpdateJob
from core.domain.reconciler import BACKOFF, CLAIM_TIMEOUT
from core.services.gateway_auth import hash_token, verify_token
from core.services.queue import retry_dead_job
from core.services.tags import refresh_desired_hashes
from shared.gateway_api import API_PREFIX
from tests.e2e.support import (
    T0,
    Clock,
    FakePipeline,
    World,
    auth,
    bind,
    build_app,
    seed,
    set_price,
)

pytestmark = pytest.mark.db

P = API_PREFIX


@pytest.fixture
def world(db: Session) -> World:
    return seed(db)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def pipeline() -> FakePipeline:
    return FakePipeline()


@pytest.fixture
def client(db: Session, world: World, clock: Clock, pipeline: FakePipeline) -> Iterator[TestClient]:
    yield TestClient(build_app(db, clock, pipeline), headers=auth())


def _seen(vendor_id: str, model: str | None, **kw: Any) -> dict[str, Any]:
    return {
        "vendor_id": vendor_id, "vendor": "fake", "model": model,
        "seen_at": datetime.now(UTC).isoformat(), **kw,
    }  # fmt: skip


def _discover(client: TestClient, db: Session, world: World, *vendor_ids: str) -> list[Tag]:
    r = client.post(f"{P}/tags/seen", json={"tags": [_seen(v, world.model) for v in vendor_ids]})
    assert r.status_code == 200, r.text
    return [_tag(db, v) for v in vendor_ids]


def _tag(db: Session, vendor_id: str) -> Tag:
    tag = db.scalars(select(Tag).where(Tag.vendor_id == vendor_id)).one()
    db.refresh(tag)
    return tag


def _jobs(db: Session, tag_id: int) -> list[UpdateJob]:
    rows = list(
        db.scalars(select(UpdateJob).where(UpdateJob.tag_id == tag_id).order_by(UpdateJob.id))
    )
    for row in rows:
        db.refresh(row)
    return rows


def _claim(client: TestClient, max_jobs: int = 10) -> list[dict[str, Any]]:
    r = client.post(f"{P}/jobs/claim", json={"max_jobs": max_jobs})
    assert r.status_code == 200, r.text
    jobs: list[dict[str, Any]] = r.json()["jobs"]
    return jobs


def _result(
    client: TestClient,
    job: dict[str, Any],
    outcome: str = "acked",
    code: str | None = None,
    attempt: int | None = None,
) -> Any:
    body = {
        "attempt": attempt or job["attempt"],
        "outcome": outcome,
        "error_code": code,
        "duration_ms": 5,
    }
    return client.post(f"{P}/jobs/{job['job_id']}/result", json=body)


# Auth


ENDPOINTS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("post", "/jobs/claim", {"max_jobs": 1}),
    ("get", "/renders/" + "a" * 64, None),
    ("post", "/jobs/1/result", {"attempt": 1, "outcome": "acked", "duration_ms": 1}),
    ("post", "/tags/seen", {"tags": []}),
    ("post", "/heartbeat", {"version": "1", "driver": "fake"}),
]


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS, ids=[e[1] for e in ENDPOINTS])
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer " + "x" * 64}, {"Authorization": "Basic Zm9vOmJhcg=="}],
    ids=["missing", "wrong", "not-bearer"],
)
def test_wrong_or_missing_token_is_401(
    client: TestClient, method: str, path: str, body: Any, headers: dict[str, str]
) -> None:
    client.headers.pop("Authorization")
    kwargs: dict[str, Any] = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    r = getattr(client, method)(P + path, **kwargs)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_token_is_stored_only_as_a_hash(db: Session, world: World) -> None:
    from tests.e2e.support import token

    row = db.scalars(select(GatewayRow).order_by(GatewayRow.id)).first()
    assert row is not None
    assert token() not in row.token_hash and row.token_hash.startswith("scrypt$")
    assert verify_token(token(), row.token_hash)
    assert not verify_token(token() + "x", row.token_hash)
    assert not verify_token(token(), "garbage")
    assert hash_token(token()) != hash_token(token())  # salted


def test_startup_creates_or_updates_gateway_row(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.app.main import create_app
    from core.app.routers import gateway as router_module
    from tests.e2e.support import token

    for row in db.scalars(select(GatewayRow)):
        row.token_hash = hash_token("some other token " * 3)  # a rotated token
    db.flush()
    monkeypatch.setattr(router_module, "get_sessionmaker", lambda: lambda: _NoClose(db))
    with TestClient(create_app()):
        pass
    rows = list(db.scalars(select(GatewayRow)))
    assert len(rows) >= 1 and verify_token(token(), rows[0].token_hash)


class _NoClose:
    """Wraps the test session as a context manager that does not close it."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def __enter__(self) -> Session:
        return self.session

    def __exit__(self, *_: object) -> None:
        return None


# Tags seen


def test_tags_seen_creates_updates_and_ignores(
    client: TestClient, db: Session, world: World
) -> None:
    r = client.post(
        f"{P}/tags/seen",
        json={"tags": [
            _seen("F-NEW", world.model, rssi=-55, battery_mv=2900, firmware="fw-1"),
            _seen("F-NOMODEL", None),
            _seen("F-OTHER", "unknown-model"),
        ]},
    )  # fmt: skip
    assert r.json() == {"known": 0, "created": 1, "ignored": 2}
    tag = _tag(db, "F-NEW")
    assert (tag.rssi, tag.battery_mv, tag.firmware, tag.tag_type_id) == (
        -55, 2900, "fw-1", world.tag_type.id,
    )  # fmt: skip
    assert len(tag.short_code) == 4 and tag.desired_hash is not None
    assert tag.desired_hash_changed_at == T0
    assert db.scalars(select(Tag).where(Tag.vendor_id.in_(["F-NOMODEL", "F-OTHER"]))).all() == []
    [job] = _jobs(db, tag.id)  # the "unassigned" label is queued
    assert (job.state, job.render_hash, job.created_at) == ("queued", tag.desired_hash, T0)

    later = datetime.now(UTC) + timedelta(seconds=1)
    r = client.post(
        f"{P}/tags/seen",
        json={"tags": [_seen("F-NEW", world.model, rssi=-70, battery_pct=40, firmware="fw-2",
                             seen_at=later.isoformat())]},
    )  # fmt: skip
    assert r.json() == {"known": 1, "created": 0, "ignored": 0}
    tag = _tag(db, "F-NEW")
    assert (tag.rssi, tag.battery_pct, tag.firmware, tag.last_seen_at) == (-70, 40, "fw-2", later)
    assert len(_jobs(db, tag.id)) == 1  # unchanged label, no new job


def test_new_tags_get_unique_short_codes(client: TestClient, db: Session, world: World) -> None:
    tags = _discover(client, db, world, *[f"F-{i}" for i in range(30)])
    assert len({t.short_code for t in tags}) == 30


# Claim, render, result


def test_claim_hands_out_job_once_with_tag_details(
    client: TestClient, db: Session, world: World
) -> None:
    [tag] = _discover(client, db, world, "F1")
    [job] = _claim(client)
    assert job["vendor_id"] == "F1" and job["attempt"] == 1
    assert job["render_hash"] == tag.desired_hash
    assert job["tag_type"]["key"] == world.tag_type.key and job["tag_type"]["palette"] == "BWR"
    assert _claim(client) == []


def test_claim_respects_max_jobs(client: TestClient, db: Session, world: World) -> None:
    _discover(client, db, world, "F1", "F2", "F3")
    assert len(_claim(client, max_jobs=2)) == 2
    assert len(_claim(client, max_jobs=2)) == 1


def test_render_download(client: TestClient, db: Session, world: World) -> None:
    [tag] = _discover(client, db, world, "F1")
    r = client.get(f"{P}/renders/{tag.desired_hash}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")
    assert client.get(f"{P}/renders/{'f' * 64}").status_code == 404
    assert client.get(f"{P}/renders/not-a-hash").status_code == 422


def test_ack_sets_displayed_hash_and_duplicate_is_409(
    client: TestClient, db: Session, world: World
) -> None:
    [tag] = _discover(client, db, world, "F1")
    [job] = _claim(client)
    r = _result(client, job)
    assert r.json() == {"job_id": job["job_id"], "state": "acked"}
    assert _tag(db, "F1").displayed_hash == tag.desired_hash
    assert _result(client, job).status_code == 409
    assert _jobs(db, tag.id)[0].state == "acked"


def test_unknown_job_is_404(client: TestClient, world: World) -> None:
    assert _result(client, {"job_id": 999_999_999, "attempt": 1}).status_code == 404


def test_result_validation_is_422(client: TestClient, db: Session, world: World) -> None:
    _discover(client, db, world, "F1")
    [job] = _claim(client)
    assert _result(client, job, outcome="failed", code=None).status_code == 422
    assert _result(client, job, outcome="acked", code="timeout").status_code == 422


def test_failure_backs_off_then_retries(
    client: TestClient, db: Session, world: World, clock: Clock
) -> None:
    [tag] = _discover(client, db, world, "F1")
    [job] = _claim(client)
    assert _result(client, job, "failed", "not_found").json()["state"] == "failed"
    [row] = _jobs(db, tag.id)
    assert (row.error_code, row.next_attempt_at) == ("not_found", T0 + BACKOFF[0])
    assert _tag(db, "F1").displayed_hash is None  # was never set; not_found keeps it
    clock.now = T0 + BACKOFF[0] - timedelta(seconds=1)
    assert _claim(client) == []
    clock.now = T0 + BACKOFF[0]
    [again] = _claim(client)
    assert (again["job_id"], again["attempt"]) == (job["job_id"], 2)


def test_timeout_failure_makes_displayed_hash_unknown(
    client: TestClient, db: Session, world: World
) -> None:
    [tag] = _discover(client, db, world, "F1")
    [job] = _claim(client)
    _result(client, job)
    assert _tag(db, "F1").displayed_hash == tag.desired_hash
    # Force a new push of the same label, then lose its confirmation.
    db.add(
        UpdateJob(
            tag_id=tag.id, render_hash=tag.desired_hash, state="queued", attempts=0, created_at=T0
        )
    )
    db.flush()
    [job2] = _claim(client)
    _result(client, job2, "failed", "timeout")
    assert _tag(db, "F1").displayed_hash is None


def test_stale_claim_is_reclaimed_and_late_result_is_409(
    client: TestClient, db: Session, world: World, clock: Clock
) -> None:
    [tag] = _discover(client, db, world, "F1")
    [first] = _claim(client)
    clock.now = T0 + CLAIM_TIMEOUT + timedelta(seconds=1)
    [second] = _claim(client)  # reclaimed and handed out again in one call
    assert (second["job_id"], second["attempt"]) == (first["job_id"], 2)
    [row] = _jobs(db, tag.id)
    assert (row.state, row.attempts, row.claimed_at) == ("claimed", 2, clock.now)

    late = _result(client, first)
    assert late.status_code == 409
    db.refresh(row)
    assert (row.state, row.attempts) == ("claimed", 2)  # nothing changed
    assert _result(client, second).json()["state"] == "acked"


def test_stale_claim_nulls_displayed_hash(
    client: TestClient, db: Session, world: World, clock: Clock
) -> None:
    [tag] = _discover(client, db, world, "F1")
    _result(client, _claim(client)[0])
    db.add(
        UpdateJob(
            tag_id=tag.id, render_hash=tag.desired_hash, state="queued", attempts=0, created_at=T0
        )
    )
    db.flush()
    [job] = _claim(client)
    clock.now = T0 + CLAIM_TIMEOUT  # exactly at the timeout counts as stale
    [again] = _claim(client)
    assert (again["job_id"], again["attempt"]) == (job["job_id"], 2)
    assert _tag(db, "F1").displayed_hash is None  # the lost push may have landed


def test_price_change_while_claimed_waits_then_queues_new_label(
    client: TestClient, db: Session, world: World, clock: Clock, pipeline: FakePipeline
) -> None:
    [tag] = _discover(client, db, world, "F1")
    _result(client, _claim(client)[0])
    bind(db, world, tag.id, world.products[0], T0)
    refresh_desired_hashes(db, pipeline, T0, tag_ids=[tag.id])
    [job] = _claim(client)
    first_label = job["render_hash"]

    clock.now = T0 + timedelta(minutes=1)
    set_price(db, world.products[0], 999)
    result = refresh_desired_hashes(db, pipeline, clock.now, product_ids=[world.products[0].id])
    assert result.changed == [tag.id]
    assert [j.state for j in _jobs(db, tag.id)] == ["acked", "claimed"]  # claimed never cancelled

    _result(client, job)
    tag = _tag(db, "F1")
    assert tag.displayed_hash == first_label != tag.desired_hash
    jobs = _jobs(db, tag.id)
    assert [j.state for j in jobs] == ["acked", "acked", "queued"]
    assert jobs[-1].render_hash == tag.desired_hash


def test_queued_job_for_old_label_is_cancelled_and_replaced(
    client: TestClient, db: Session, world: World, pipeline: FakePipeline
) -> None:
    [tag] = _discover(client, db, world, "F1")
    bind(db, world, tag.id, world.products[0], T0)
    refresh_desired_hashes(db, pipeline, T0, tag_ids=[tag.id])
    jobs = _jobs(db, tag.id)
    assert [j.state for j in jobs] == ["cancelled", "queued"]
    assert jobs[-1].render_hash == _tag(db, "F1").desired_hash


def test_dead_job_and_admin_retry(
    client: TestClient, db: Session, world: World, clock: Clock
) -> None:
    [tag] = _discover(client, db, world, "F1")
    for attempt in range(1, 7):
        [job] = _claim(client)
        assert job["attempt"] == attempt
        state = _result(client, job, "failed", "not_found").json()["state"]
        if attempt < 6:
            assert state == "failed"
            clock.now += BACKOFF[attempt - 1]
    assert state == "dead"
    clock.now += timedelta(days=1)
    assert _claim(client) == []  # dead is not recreated for the same label
    dead = _jobs(db, tag.id)[-1]

    actions = retry_dead_job(db, dead.id, "admin@example.invalid", clock.now)
    assert len(actions) == 1
    [fresh] = _claim(client)
    assert fresh["attempt"] == 1 and fresh["job_id"] != dead.id
    event = db.scalars(select(EventLog).where(EventLog.kind == "job.retry")).all()[-1]
    assert event.payload["job_id"] == dead.id


def test_heartbeat_updates_gateway(
    client: TestClient, db: Session, world: World, clock: Clock
) -> None:
    r = client.post(f"{P}/heartbeat", json={"version": "0.1.0", "driver": "fake"})
    assert r.status_code == 200
    assert datetime.fromisoformat(r.json()["server_time"]) == T0
    row = db.scalars(select(GatewayRow).order_by(GatewayRow.id)).first()
    assert row is not None
    db.refresh(row)
    assert (row.last_heartbeat_at, row.version, row.driver) == (T0, "0.1.0", "fake")


# The real label pipeline, once workstream B has landed pricing and the renderer.


@pytest.mark.xfail(raises=NotImplementedError, strict=True, reason="needs workstream B")
def test_real_pipeline_renders_unassigned_and_bound_labels(db: Session, world: World) -> None:
    from core.services.tags import real_label_pipeline, record_seen_tags
    from shared.gateway_api import SeenTagReport

    record_seen_tags(
        db,
        [SeenTagReport.model_validate(_seen("F-REAL", world.model))],
        real_label_pipeline,
        T0,
    )
    tag = db.scalars(select(Tag).where(Tag.vendor_id == "F-REAL")).one()
    assert tag.desired_hash is not None
    unassigned = tag.desired_hash
    bind(db, world, tag.id, world.products[0], T0)
    refresh_desired_hashes(db, real_label_pipeline, T0, tag_ids=[tag.id])
    assert tag.desired_hash not in (None, unassigned)

"""The initial migration matches the models, round-trips, and enforces key invariants."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from core.db.models import Base
from tests.conftest import alembic_config

EXPECTED_TABLES = {
    "store", "app_user", "product", "price", "price_history", "tag_type", "tag",
    "template", "binding", "render", "update_job", "import_profile", "import_run",
    "gateway", "event_log",
}  # fmt: skip

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def scratch_url(database_url: str) -> Iterator[str]:
    """A throwaway database, so downgrades never touch the development data."""
    base = make_url(database_url)
    name = f"esl_migtest_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="module")
def scratch_engine(scratch_url: str) -> Iterator[Engine]:
    command.upgrade(alembic_config(scratch_url), "head")
    engine = create_engine(scratch_url)
    yield engine
    engine.dispose()


def test_all_tables_exist(scratch_engine: Engine) -> None:
    tables = set(inspect(scratch_engine).get_table_names()) - {"alembic_version"}
    assert tables == EXPECTED_TABLES


def test_models_match_migration(scratch_engine: Engine) -> None:
    with scratch_engine.connect() as conn:
        diff = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata
        )
    assert diff == []


def test_store_is_seeded(scratch_engine: Engine) -> None:
    with scratch_engine.connect() as conn:
        rows = conn.execute(text("SELECT id, timezone FROM store")).all()
    assert [tuple(r) for r in rows] == [(1, "Europe/Athens")]


def test_store_sequence_is_past_seed(scratch_engine: Engine) -> None:
    with scratch_engine.connect() as conn, conn.begin():
        new_id = conn.execute(text("INSERT INTO store (name) VALUES ('x') RETURNING id"))
        assert new_id.scalar_one() == 2
        conn.rollback()


def _product(conn: Connection) -> int:
    return int(
        conn.execute(
            text("INSERT INTO product (ean, name) VALUES (:e, 'Test') RETURNING id"),
            {"e": uuid.uuid4().hex[:13]},
        ).scalar_one()
    )


@pytest.mark.parametrize(
    "statement", ["UPDATE price_history SET effective_cents = 1", "DELETE FROM price_history"]
)
def test_price_history_is_append_only(scratch_engine: Engine, statement: str) -> None:
    with scratch_engine.connect() as conn, conn.begin():
        pid = _product(conn)
        conn.execute(
            text(
                "INSERT INTO price_history (product_id, effective_cents, kind, valid_from, source)"
                " VALUES (:p, 199, 'regular', now(), 'manual')"
            ),
            {"p": pid},
        )
        with pytest.raises(DBAPIError, match="append-only"), conn.begin_nested():
            conn.execute(text(statement))
        conn.rollback()


def test_event_log_is_append_only(scratch_engine: Engine) -> None:
    with scratch_engine.connect() as conn, conn.begin():
        conn.execute(text("INSERT INTO event_log (actor, kind) VALUES ('system', 'test')"))
        with pytest.raises(DBAPIError, match="append-only"), conn.begin_nested():
            conn.execute(text("DELETE FROM event_log"))
        conn.rollback()


def test_money_check_rejects_negative(scratch_engine: Engine) -> None:
    with scratch_engine.connect() as conn, conn.begin():
        pid = _product(conn)
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(
                text("INSERT INTO price (product_id, regular_cents) VALUES (:p, -1)"), {"p": pid}
            )
        conn.rollback()


def test_promo_must_be_complete(scratch_engine: Engine) -> None:
    with scratch_engine.connect() as conn, conn.begin():
        pid = _product(conn)
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(
                text(
                    "INSERT INTO price (product_id, regular_cents, promo_cents)"
                    " VALUES (:p, 500, 400)"
                ),
                {"p": pid},
            )
        conn.rollback()


def test_one_active_job_per_tag(scratch_engine: Engine) -> None:
    h1, h2 = "a" * 64, "b" * 64
    with scratch_engine.connect() as conn, conn.begin():
        tt = conn.execute(
            text(
                "INSERT INTO tag_type (key, vendor, model, width, height, palette)"
                " VALUES ('t', 'fake', 'm', 296, 128, 'BWR') RETURNING id"
            )
        ).scalar_one()
        tag = conn.execute(
            text(
                "INSERT INTO tag (vendor_id, tag_type_id, short_code)"
                " VALUES ('AA:BB', :tt, 'K7Q2') RETURNING id"
            ),
            {"tt": tt},
        ).scalar_one()
        for h in (h1, h2):
            conn.execute(
                text("INSERT INTO render (hash, tag_type_id, png) VALUES (:h, :tt, '\\x00')"),
                {"h": h, "tt": tt},
            )
        insert = text(
            "INSERT INTO update_job (tag_id, render_hash, state, created_at)"
            " VALUES (:t, :h, :s, now())"
        )
        conn.execute(insert, {"t": tag, "h": h1, "s": "claimed"})
        # Finished jobs do not count towards the limit.
        conn.execute(insert, {"t": tag, "h": h1, "s": "acked"})
        conn.execute(insert, {"t": tag, "h": h1, "s": "cancelled"})
        conn.execute(insert, {"t": tag, "h": h1, "s": "dead"})
        for state in ("queued", "failed", "claimed"):
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(insert, {"t": tag, "h": h2, "s": state})
        conn.rollback()


def test_downgrade_then_upgrade(scratch_url: str, scratch_engine: Engine) -> None:
    cfg = alembic_config(scratch_url)
    command.downgrade(cfg, "base")
    assert set(inspect(scratch_engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(cfg, "head")
    assert set(inspect(scratch_engine).get_table_names()) - {"alembic_version"} == EXPECTED_TABLES

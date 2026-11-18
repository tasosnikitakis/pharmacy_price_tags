"""Import and manual edit on two real connections: no deadlock (review note N1).

The scenario needs committed rows seen by two connections, so it runs in a throwaway
database instead of the rolled-back ``db`` session (as in
``test_gateway_api_concurrency.py``). ``price_history`` and ``event_log`` are
append-only, so the database is dropped rather than cleaned.

An edit locks its product and is about to insert its history and event rows, each of
which takes ``FOR KEY SHARE`` on the ``store`` row through its foreign key. An import
starts meanwhile: it locks the store row, then waits for the edit's product. With
``FOR UPDATE`` on the store this was a deadlock (the edit waits for the store, the
import for the product). With ``FOR NO KEY UPDATE`` the edit's inserts go through, it
commits, and the import then runs on the edited catalogue.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from core.db.models import EventLog, ImportProfile, Price, PriceHistory, Product
from core.domain import pricing
from core.domain.pricing import PriceChange
from core.domain.types import PricePoint, PriceSource, PriceState, ProductFacts
from core.importers import ImportSettings
from core.services import catalogue as svc
from tests.conftest import alembic_config

pytestmark = pytest.mark.db

SYNTHETIC = Path(__file__).resolve().parents[1] / "fixtures" / "erp_synthetic_cp1253.csv"
MAPPING = {
    "ean": "Barcode",
    "name": "Περιγραφή",
    "price": "Λιανική με ΦΠΑ",
    "brand": "Εταιρεία",
    "pack_qty": "Συσκευασία",
    "pack_unit": "Μονάδα",
}
OIL = "2000000000022"  # 8,50 in the file
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def scratch_engine(database_url: str) -> Iterator[Engine]:
    base = make_url(database_url)
    name = f"esl_catalogue_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "head")
        engine = create_engine(url, pool_size=4)
        yield engine
        engine.dispose()
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _waiting_for_a_lock(engine: Engine) -> int:
    with engine.connect() as conn:
        return (
            conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
            or 0
        )


def test_edit_holding_a_product_lock_and_an_import_both_complete(
    scratch_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = sessionmaker(scratch_engine, expire_on_commit=False)
    with factory() as s:
        product = Product(ean=OIL, name="ΈΛΑΙΟ ΣΩΜΑΤΟΣ ΉΠΙΟ")
        s.add(product)
        s.flush()
        s.add(Price(product_id=product.id, regular_cents=800))
        s.add(
            PriceHistory(
                product_id=product.id, effective_cents=800, kind="regular",
                valid_from=NOW - timedelta(days=60), source="import",
            )
        )  # fmt: skip
        profile = svc.save_profile(s, "synthetic", ImportSettings.create(MAPPING))
        s.commit()
        product_id, profile_id = product.id, profile.id

    edit_has_lock = threading.Event()
    real_apply = pricing.apply_price_change

    def apply_price_change(
        product: ProductFacts,
        old: PriceState | None,
        requested: PriceState,
        history: Sequence[PricePoint],
        now: datetime,
        source: PriceSource,
        entered_prior_cents: int | None = None,
    ) -> PriceChange:
        if threading.current_thread().name.startswith("edit"):
            # The edit holds its product and price locks here. Let the import start,
            # and wait until it is blocked on a lock before inserting anything.
            edit_has_lock.set()
            deadline = time.monotonic() + 10
            while _waiting_for_a_lock(scratch_engine) == 0:
                assert time.monotonic() < deadline, "the import never waited for a lock"
                time.sleep(0.05)
        return real_apply(product, old, requested, history, now, source, entered_prior_cents)

    monkeypatch.setattr(pricing, "apply_price_change", apply_price_change)

    def edit() -> PriceChange | None:
        with factory() as s:
            change = svc.edit_price(s, product_id, 999, actor="staff@example.test", now=NOW)
            s.commit()
            return change

    def run_import() -> int:
        assert edit_has_lock.wait(10)
        with factory() as s:
            stored = s.get(ImportProfile, profile_id)
            assert stored is not None
            result = svc.apply_import(
                s, SYNTHETIC.read_bytes(), "export.csv", stored, actor="system",
                now=NOW + timedelta(minutes=1),
            )  # fmt: skip
            s.commit()
            return result.run.id

    with (
        ThreadPoolExecutor(1, thread_name_prefix="edit") as edits,
        ThreadPoolExecutor(1, thread_name_prefix="import") as imports,
    ):
        edit_done = edits.submit(edit)
        import_done = imports.submit(run_import)
        assert edit_done.result(timeout=30) is not None  # no DeadlockDetected
        run_id = import_done.result(timeout=30)

    with factory() as s:
        price = s.get(Price, product_id)
        assert price is not None and price.regular_cents == 850  # the import ran last
        history = list(
            s.scalars(
                select(PriceHistory.effective_cents)
                .where(PriceHistory.product_id == product_id)
                .order_by(PriceHistory.id)
            )
        )
        assert history == [800, 999, 850]
        kinds = list(s.scalars(select(EventLog.kind).order_by(EventLog.id)))
        assert kinds.index("price.manual") < kinds.index("import.applied")
        applied = s.scalar(select(EventLog.payload).where(EventLog.kind == "import.applied"))
        assert applied is not None and applied["import_run_id"] == run_id
        # The import saw the committed manual edit: the row was flagged, then cleared.
        assert applied["counts"]["manually_changed"] == 1
        oil = s.get(Product, product_id)
        assert oil is not None and oil.manually_changed_at is None

"""Creating a product from the binding page while an import runs, on two real
connections (review of workstream E, part 1).

``core.services.products.create_product`` locks the ``store`` row ``FOR NO KEY UPDATE``
first, exactly as ``apply_import`` does, so the two are serialised:

- a create that starts while an import is open waits for the import to commit, then
  finds the EAN the import added and refuses with ``ProductExists`` (no duplicate key
  error, no second product);
- an import that starts while a create is open waits for the create to commit, then
  sees the new product in its diff as an existing product (a price change), not as a
  new one.

Neither ever deadlocks. The scenario needs committed rows seen by two connections, so it
runs in a throwaway database, as ``test_catalogue_concurrency.py`` does.
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
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from core.db.models import EventLog, ImportProfile, Price, PriceHistory, Product
from core.domain import pricing
from core.domain.pricing import PriceChange
from core.domain.types import PricePoint, PriceSource, PriceState, ProductFacts
from core.importers import ImportSettings
from core.services import catalogue as svc
from core.services.products import NewProduct, ProductExists, create_product
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


@pytest.fixture
def scratch_engine(database_url: str) -> Iterator[Engine]:
    base = make_url(database_url)
    name = f"esl_create_{uuid.uuid4().hex[:8]}"
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


def _hold_in(first: str, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Pause the thread named ``first*`` inside ``apply_price_change`` (it holds the
    store lock by then) until another connection waits for a lock. Returns an event
    set once the pause starts."""
    holding = threading.Event()
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
        if threading.current_thread().name.startswith(first) and not holding.is_set():
            holding.set()
            deadline = time.monotonic() + 10
            while _waiting_for_a_lock(engine) == 0:
                assert time.monotonic() < deadline, "the other writer never waited for a lock"
                time.sleep(0.05)
        return real_apply(product, old, requested, history, now, source, entered_prior_cents)

    monkeypatch.setattr(pricing, "apply_price_change", apply_price_change)
    return holding


def _profile_id(engine: Engine) -> int:
    with sessionmaker(engine)() as s:
        profile = svc.save_profile(s, "synthetic", ImportSettings.create(MAPPING))
        s.commit()
        return profile.id


def _import(engine: Engine, profile_id: int) -> dict[str, int]:
    with sessionmaker(engine, expire_on_commit=False)() as s:
        profile = s.get(ImportProfile, profile_id)
        assert profile is not None
        result = svc.apply_import(
            s, SYNTHETIC.read_bytes(), "export.csv", profile, actor="system",
            now=NOW + timedelta(minutes=1),
        )  # fmt: skip
        s.commit()
        return dict(result.run.counts)


def _create(engine: Engine, cents: int) -> int:
    with sessionmaker(engine, expire_on_commit=False)() as s:
        product = create_product(
            s, NewProduct(OIL, "Έλαιο σώματος", cents), actor="admin@example.test", now=NOW
        )
        s.commit()
        return product.id


def test_create_during_an_import_waits_and_finds_the_imported_product(
    scratch_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_id = _profile_id(scratch_engine)
    holding = _hold_in("import", scratch_engine, monkeypatch)

    def create_after_import_started() -> int:
        assert holding.wait(10)
        return _create(scratch_engine, 800)

    with (
        ThreadPoolExecutor(1, thread_name_prefix="import") as imports,
        ThreadPoolExecutor(1, thread_name_prefix="create") as creates,
    ):
        import_done = imports.submit(_import, scratch_engine, profile_id)
        create_done = creates.submit(create_after_import_started)
        counts = import_done.result(timeout=30)
        with pytest.raises(ProductExists):
            create_done.result(timeout=30)

    assert counts["new"] >= 1
    with sessionmaker(scratch_engine)() as s:
        products = list(s.scalars(select(Product).where(Product.ean == OIL)))
        assert len(products) == 1
        price = s.get(Price, products[0].id)
        assert price is not None and price.regular_cents == 850  # the import's price
        assert s.scalar(select(func.count()).where(EventLog.kind == "product.created")) == 0


def test_import_during_a_create_waits_and_sees_the_new_product(
    scratch_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_id = _profile_id(scratch_engine)
    holding = _hold_in("create", scratch_engine, monkeypatch)

    def import_after_create_started() -> dict[str, int]:
        assert holding.wait(10)
        return _import(scratch_engine, profile_id)

    with (
        ThreadPoolExecutor(1, thread_name_prefix="create") as creates,
        ThreadPoolExecutor(1, thread_name_prefix="import") as imports,
    ):
        create_done = creates.submit(_create, scratch_engine, 800)
        import_done = imports.submit(import_after_create_started)
        product_id = create_done.result(timeout=30)  # no DeadlockDetected
        counts = import_done.result(timeout=30)

    with sessionmaker(scratch_engine)() as s:
        assert s.scalar(select(func.count()).where(Product.ean == OIL)) == 1
        price = s.get(Price, product_id)
        assert price is not None and price.regular_cents == 850  # the import ran last
        history = list(
            s.scalars(
                select(PriceHistory.source)
                .where(PriceHistory.product_id == product_id)
                .order_by(PriceHistory.id)
            )
        )
        assert history == ["manual", "import"]
        applied = s.scalar(select(EventLog.payload).where(EventLog.kind == "import.applied"))
        assert applied is not None
        # The new product was matched (a price change, 8,00 -> 8,50), not created again,
        # and its manual flag was seen.
        assert applied["counts"]["manually_changed"] == 1
    assert counts["changed"] >= 1

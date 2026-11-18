"""Catalogue services against Postgres (workstream A).

Two kinds of test:

- Storage tests replace ``core.domain.pricing.apply_price_change`` and
  ``apply_product_change`` with a recording stand-in. They check that the services
  call the shared write path with the right intent and store exactly what it returns,
  in one transaction. They do not depend on workstream B and keep their value after
  B lands.
- End-to-end tests use the real pricing functions and are strict ``xfail`` until
  workstream B implements them.
"""

# ruff: noqa: RUF001  (Greek test data is meant to look like Latin letters)
from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.db.models import EventLog, ImportProfile, ImportRun, Price, PriceHistory, Product
from core.domain import pricing
from core.domain.pricing import NewPricePoint, PriceChange, PriceEvent
from core.domain.types import (
    PackUnit,
    PriceKind,
    PricePoint,
    PriceSource,
    PriceState,
    ProductFacts,
)
from core.importers import ImportFormatError, ImportSettings
from core.services import catalogue as svc

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SYNTHETIC = FIXTURES / "erp_synthetic_cp1253.csv"
MAPPING = {
    "ean": "Barcode",
    "name": "Περιγραφή",
    "price": "Λιανική με ΦΠΑ",
    "brand": "Εταιρεία",
    "pack_qty": "Συσκευασία",
    "pack_unit": "Μονάδα",
}
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
ACTOR = "admin@example.test"
NEEDS_B = pytest.mark.xfail(raises=NotImplementedError, strict=True, reason="needs workstream B")


# --- Helpers --------------------------------------------------------------------------


@pytest.fixture
def profile(db: Session) -> ImportProfile:
    return svc.save_profile(db, "synthetic", ImportSettings.create(MAPPING))


def _synthetic() -> bytes:
    return SYNTHETIC.read_bytes()


def _synthetic_edited(replacements: dict[str, str]) -> bytes:
    """The synthetic file with some lines' text replaced, still Windows-1253."""
    text = _synthetic().decode("cp1253")
    for old, new in replacements.items():
        assert old in text
        text = text.replace(old, new)
    return text.encode("cp1253")


def _seed(
    db: Session,
    ean: str,
    name: str,
    cents: int,
    *,
    manually_changed_at: datetime | None = None,
    **price: Any,
) -> Product:
    product = Product(ean=ean, name=name, manually_changed_at=manually_changed_at)
    db.add(product)
    db.flush()
    db.add(Price(product_id=product.id, regular_cents=cents, **price))
    db.add(
        PriceHistory(
            product_id=product.id,
            effective_cents=cents,
            kind="regular",
            valid_from=NOW - timedelta(days=60),
            source="import",
        )
    )
    db.flush()
    return product


def _count(db: Session, model: type[Any]) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def _events(db: Session, kind: str | None = None) -> list[EventLog]:
    query = select(EventLog).order_by(EventLog.id)
    if kind is not None:
        query = query.where(EventLog.kind == kind)
    return list(db.scalars(query))


def _history(db: Session, product_id: int) -> list[PriceHistory]:
    return list(
        db.scalars(
            select(PriceHistory)
            .where(PriceHistory.product_id == product_id)
            .order_by(PriceHistory.id)
        )
    )


def _product(db: Session, ean: str) -> Product:
    product = db.scalar(select(Product).where(Product.ean == ean))
    assert product is not None
    return product


@dataclasses.dataclass
class FakePricing:
    """Stand-in for workstream B: keeps the requested state, writes one regular row
    at ``now`` and one recognisable event, and records every call."""

    price_calls: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    product_calls: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    fail_on_call: int | None = None

    def apply_price_change(
        self,
        product: ProductFacts,
        old: PriceState | None,
        requested: PriceState,
        history: Sequence[PricePoint],
        now: datetime,
        source: PriceSource,
        entered_prior_cents: int | None = None,
    ) -> PriceChange:
        self.price_calls.append(
            dict(product=product, old=old, requested=requested, history=list(history),
                 now=now, source=source)
        )  # fmt: skip
        if self.fail_on_call == len(self.price_calls):
            raise RuntimeError("injected failure")
        kept_prior = old.promo_prior_cents if old is not None else None
        return PriceChange(
            state=dataclasses.replace(requested, promo_prior_cents=kept_prior),
            history_rows=[NewPricePoint(requested.regular_cents, PriceKind.REGULAR, now, source)],
            events=[PriceEvent("test.price", {"product_id": product.product_id, "at": now})],
        )

    def apply_product_change(
        self,
        old: ProductFacts,
        new: ProductFacts,
        price: PriceState | None,
        history: Sequence[PricePoint],
        now: datetime,
    ) -> PriceChange | None:
        self.product_calls.append(dict(old=old, new=new, price=price, now=now))
        if new.is_rx and price is not None and price.promo_cents is not None:
            ended = PriceState(regular_cents=price.regular_cents)
            return PriceChange(
                state=ended,
                history_rows=[
                    NewPricePoint(price.regular_cents, PriceKind.REGULAR, now, PriceSource.MANUAL)
                ],
                events=[PriceEvent("promo.ended_rx", {"product_id": new.product_id})],
            )
        return None


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakePricing:
    stub = FakePricing()
    monkeypatch.setattr(pricing, "apply_price_change", stub.apply_price_change)
    monkeypatch.setattr(pricing, "apply_product_change", stub.apply_product_change)
    return stub


# --- Profiles -------------------------------------------------------------------------


def test_profiles_save_update_list(db: Session) -> None:
    first = svc.save_profile(db, " erp ", ImportSettings.create(MAPPING, "cp1253", ";"))
    assert (first.name, first.encoding, first.delimiter) == ("erp", "cp1253", ";")
    assert first.mapping == MAPPING
    smaller = {"ean": "EAN", "name": "Όνομα", "price": "Τιμή"}
    again = svc.save_profile(db, "erp", ImportSettings.create(smaller))
    assert again.id == first.id
    assert (again.mapping, again.encoding, again.delimiter) == (smaller, None, None)
    svc.save_profile(db, "another", ImportSettings.create(MAPPING))
    assert [p.name for p in svc.list_profiles(db)] == ["another", "erp"]
    assert svc.get_profile(db, "missing") is None
    assert svc.profile_settings(again) == ImportSettings.create(smaller)
    with pytest.raises(ValueError, match="empty"):
        svc.save_profile(db, "  ", ImportSettings.create(MAPPING))


# --- Preview (no workstream B needed) -------------------------------------------------


def test_preview_empty_catalogue_lists_new_and_rejected(
    db: Session, profile: ImportProfile
) -> None:
    preview = svc.preview_import(db, _synthetic(), "export.csv", profile)
    assert (preview.encoding, preview.delimiter) == ("cp1253", ";")
    assert (preview.filename, preview.profile_name) == ("export.csv", "synthetic")
    assert preview.diff.counts() == {
        "new": 9, "changed": 0, "unchanged": 0, "rejected": 4, "missing": 0,
        "price_changed": 0, "manually_changed": 0,
    }  # fmt: skip
    assert [r.line for r in preview.diff.rejected] == [10, 11, 12, 13]
    assert _count(db, Product) == 0 and _count(db, ImportRun) == 0  # nothing written


def test_preview_against_catalogue(db: Session, profile: ImportProfile) -> None:
    _seed(db, "2000000000015", "ΆΛΟΗ ΚΡΕΜΑ ΧΕΡΙΩΝ", 1290)  # unchanged price, new fields
    _seed(db, "2000000000022", "ΈΛΑΙΟ ΣΩΜΑΤΟΣ ΉΠΙΟ", 800, manually_changed_at=NOW)
    gone = _seed(db, "2999999999999", "ΠΑΛΙΟ ΠΡΟΪΟΝ", 100)
    _seed(db, "2000000000091", "ΣΙΡΟΠΙ", 700)  # its row is rejected: not missing
    diff = svc.preview_import(db, _synthetic(), "export.csv", profile).diff

    assert len(diff.new) == 7
    changed = {e.row.ean: e for e in diff.changed}
    assert set(changed) == {"2000000000015", "2000000000022"}
    assert not changed["2000000000015"].price_changed  # only brand and pack are new
    assert [c.field for c in changed["2000000000015"].field_changes] == [
        "brand", "pack_qty_milli", "pack_unit"
    ]  # fmt: skip
    oil = changed["2000000000022"]
    assert (oil.old_regular_cents, oil.row.regular_cents, oil.manually_changed) == (800, 850, True)
    assert [m.product_id for m in diff.missing] == [gone.id]
    assert [e.row.ean for e in diff.manually_changed] == ["2000000000022"]


def test_preview_refuses_unreadable_file(db: Session, profile: ImportProfile) -> None:
    with pytest.raises(ImportFormatError):
        svc.preview_import(db, b"ean;price\n1;2\n", "wrong.csv", profile)


def test_last_applied_import_at(db: Session) -> None:
    assert svc.last_applied_import_at(db) is None
    db.add(ImportRun(filename="a.csv", applied_at=NOW - timedelta(days=9)))
    db.add(ImportRun(filename="b.csv", applied_at=NOW - timedelta(days=2)))
    db.add(ImportRun(filename="c.csv"))  # previewed or failed, never applied
    db.flush()
    assert svc.last_applied_import_at(db) == NOW - timedelta(days=2)


# --- Apply: storage through the shared write path (stand-in for B) ------------------


def test_apply_import_stores_what_pricing_returns(
    db: Session, profile: ImportProfile, fake: FakePricing
) -> None:
    result = svc.apply_import(db, _synthetic(), "export.csv", profile, actor=ACTOR, now=NOW)
    run = result.run
    assert (run.applied_at, run.started_at, run.profile_id) == (NOW, NOW, profile.id)
    assert run.counts["new"] == 9 and run.counts["rejected"] == 4
    assert run.counts["promo_refused"] == 0
    assert svc.last_applied_import_at(db) == NOW

    assert _count(db, Product) == 9
    balm = _product(db, "2000000000060")
    assert (balm.name, balm.pack_qty_milli, balm.pack_unit) == (
        "ΒΑΛΣΑΜΟ ΧΕΙΛΙΩΝ ΆΧΡΩΜΟ", 4800, "g"
    )  # fmt: skip
    price = db.get(Price, balm.id)
    assert price is not None and price.regular_cents == 390 and price.updated_at == NOW
    (row,) = _history(db, balm.id)
    assert (row.effective_cents, row.kind, row.valid_from, row.source, row.import_run_id) == (
        390, "regular", NOW, "import", run.id
    )  # fmt: skip

    # Intent: a new product has no old price; source is import; one clock throughout.
    call = next(c for c in fake.price_calls if c["product"].ean == "2000000000060")
    assert call["old"] is None and call["history"] == []
    assert call["requested"] == PriceState(regular_cents=390)
    assert {c["now"] for c in fake.price_calls} == {NOW}
    assert {c["source"] for c in fake.price_calls} == {PriceSource.IMPORT}

    # Every PriceEvent is written, then import.applied with the counts.
    events = _events(db)
    assert [e.kind for e in events] == ["test.price"] * 9 + ["import.applied"]
    assert {e.actor for e in events} == {ACTOR}
    assert {e.at for e in events} == {NOW}
    assert events[0].payload["at"] == NOW.isoformat()
    applied = events[-1].payload
    assert applied["import_run_id"] == run.id
    assert applied["counts"] == run.counts
    assert (applied["encoding"], applied["delimiter"], applied["filename"]) == (
        "cp1253", ";", "export.csv"
    )  # fmt: skip


def test_reimport_shows_and_writes_only_real_changes(
    db: Session, profile: ImportProfile, fake: FakePricing
) -> None:
    svc.apply_import(db, _synthetic(), "v1.csv", profile, actor=ACTOR, now=NOW)
    history_before = _count(db, PriceHistory)
    calls_before = len(fake.price_calls)

    again = svc.preview_import(db, _synthetic(), "v1.csv", profile).diff
    assert (len(again.new), len(again.changed), len(again.unchanged)) == (0, 0, 9)
    second = svc.apply_import(db, _synthetic(), "v1.csv", profile, actor=ACTOR, now=NOW)
    assert second.run.counts["unchanged"] == 9
    assert _count(db, PriceHistory) == history_before
    assert len(fake.price_calls) == calls_before

    later = NOW + timedelta(days=1)
    edited = _synthetic_edited({";250;ml;6,4\r\n": ";250;ml;6,90\r\n"})
    diff = svc.preview_import(db, edited, "v2.csv", profile).diff
    assert [(e.row.ean, e.old_regular_cents, e.row.regular_cents) for e in diff.changed] == [
        ("2000000000039", 640, 690)
    ]
    third = svc.apply_import(db, edited, "v2.csv", profile, actor=ACTOR, now=later)
    assert _count(db, PriceHistory) == history_before + 1
    shampoo = _product(db, "2000000000039")
    last = _history(db, shampoo.id)[-1]
    assert (last.effective_cents, last.valid_from, last.import_run_id) == (690, later, third.run.id)


def test_import_passes_stored_promo_through(
    db: Session, profile: ImportProfile, fake: FakePricing
) -> None:
    promo: dict[str, Any] = dict(
        promo_cents=700, promo_from=NOW - timedelta(days=1), promo_to=NOW + timedelta(days=9),
        promo_prior_cents=820,
    )  # fmt: skip
    oil = _seed(db, "2000000000022", "ΈΛΑΙΟ ΣΩΜΑΤΟΣ ΉΠΙΟ", 800, **promo)
    svc.apply_import(db, _synthetic(), "export.csv", profile, actor=ACTOR, now=NOW)
    call = next(c for c in fake.price_calls if c["product"].product_id == oil.id)
    old = PriceState(800, 700, promo["promo_from"], promo["promo_to"], 820)
    assert call["old"] == old
    assert call["requested"] == dataclasses.replace(old, regular_cents=850)
    assert [p.effective_cents for p in call["history"]] == [800]
    price = db.get(Price, oil.id)
    assert price is not None
    assert (price.regular_cents, price.promo_cents, price.promo_prior_cents) == (850, 700, 820)


def test_import_clears_manual_flag_and_never_deletes(
    db: Session, profile: ImportProfile, fake: FakePricing
) -> None:
    in_file = _seed(db, "2000000000022", "ΈΛΑΙΟ ΣΩΜΑΤΟΣ ΉΠΙΟ", 850, manually_changed_at=NOW)
    not_in_file = _seed(db, "2999999999999", "ΠΑΛΙΟ", 100, manually_changed_at=NOW)
    result = svc.apply_import(db, _synthetic(), "export.csv", profile, actor=ACTOR, now=NOW)
    assert result.run.counts["manually_changed"] == 1
    assert result.run.counts["missing"] == 1
    db.refresh(in_file)
    db.refresh(not_in_file)
    assert in_file.manually_changed_at is None
    # Missing products stay, unchanged, with their flag: they are only listed.
    assert not_in_file.manually_changed_at == NOW and not_in_file.active
    price = db.get(Price, not_in_file.id)
    assert price is not None and price.regular_cents == 100


def test_import_field_changes_use_apply_product_change(
    db: Session, profile: ImportProfile, fake: FakePricing
) -> None:
    hand = _seed(db, "2000000000015", "ΚΡΕΜΑ ΧΕΡΙΩΝ", 1290)
    svc.apply_import(db, _synthetic(), "export.csv", profile, actor=ACTOR, now=NOW)
    (call,) = fake.product_calls
    assert call["old"].name == "ΚΡΕΜΑ ΧΕΡΙΩΝ" and call["new"].name == "ΆΛΟΗ ΚΡΕΜΑ ΧΕΡΙΩΝ"
    assert call["new"].pack_qty_milli == 75_000 and call["new"].pack_unit is PackUnit.MILLILITRE
    assert call["price"] == PriceState(1290)
    db.refresh(hand)
    assert (hand.name, hand.brand, hand.pack_unit) == ("ΆΛΟΗ ΚΡΕΜΑ ΧΕΡΙΩΝ", "Δοκιμή Α.Ε.", "ml")
    # The price is unchanged, so no price write and no history row.
    assert len(_history(db, hand.id)) == 1
    assert all(c["product"].product_id != hand.id for c in fake.price_calls)


def test_apply_import_is_one_transaction(
    db: Session, profile: ImportProfile, fake: FakePricing
) -> None:
    existing = _seed(db, "2000000000022", "ΈΛΑΙΟ ΣΩΜΑΤΟΣ ΉΠΙΟ", 800, manually_changed_at=NOW)
    fake.fail_on_call = 5
    with pytest.raises(RuntimeError, match="injected"):
        svc.apply_import(db, _synthetic(), "export.csv", profile, actor=ACTOR, now=NOW)
    assert _count(db, Product) == 1
    assert _count(db, ImportRun) == 0
    assert _count(db, EventLog) == 0
    assert _count(db, PriceHistory) == 1
    db.refresh(existing)
    assert existing.manually_changed_at == NOW


def test_apply_import_requires_aware_now(db: Session, profile: ImportProfile) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        svc.apply_import(db, _synthetic(), "x.csv", profile, actor=ACTOR, now=datetime(2026, 1, 1))  # noqa: DTZ001


# --- Manual edits (stand-in for B) ----------------------------------------------------


def test_edit_price_writes_manual_history_and_flag(db: Session, fake: FakePricing) -> None:
    product = _seed(db, "2000000000015", "ΆΛΟΗ", 1290)
    change = svc.edit_price(db, product.id, 1350, actor=ACTOR, now=NOW)
    assert change is not None
    (call,) = fake.price_calls
    assert call["source"] is PriceSource.MANUAL
    assert call["requested"] == PriceState(1350)
    db.refresh(product)
    assert product.manually_changed_at == NOW
    last = _history(db, product.id)[-1]
    assert (last.effective_cents, last.source, last.valid_from) == (1350, "manual", NOW)
    assert [e.kind for e in _events(db)] == ["test.price", "price.manual"]
    manual = _events(db, "price.manual")[0]
    assert manual.actor == ACTOR
    assert manual.payload == {
        "product_id": product.id, "ean": "2000000000015",
        "old_regular_cents": 1290, "new_regular_cents": 1350,
    }  # fmt: skip


def test_edit_price_same_value_writes_nothing(db: Session, fake: FakePricing) -> None:
    product = _seed(db, "2000000000015", "ΆΛΟΗ", 1290)
    assert svc.edit_price(db, product.id, 1290, actor=ACTOR, now=NOW) is None
    assert fake.price_calls == [] and _count(db, EventLog) == 0
    assert product.manually_changed_at is None


def test_edit_price_keeps_promo(db: Session, fake: FakePricing) -> None:
    window: dict[str, Any] = dict(promo_cents=900, promo_from=NOW + timedelta(days=1),
                  promo_to=NOW + timedelta(days=8))  # fmt: skip
    product = _seed(db, "2000000000015", "ΆΛΟΗ", 1290, **window)
    svc.edit_price(db, product.id, 1350, actor=ACTOR, now=NOW)
    assert fake.price_calls[0]["requested"] == PriceState(
        1350, 900, window["promo_from"], window["promo_to"]
    )


def test_edit_price_validates_input(db: Session) -> None:
    with pytest.raises(ValueError, match="negative"):
        svc.edit_price(db, 1, -1, actor=ACTOR, now=NOW)
    with pytest.raises(TypeError):
        svc.edit_price(db, 1, 12.9, actor=ACTOR, now=NOW)  # type: ignore[arg-type]
    with pytest.raises(svc.NotFound):
        svc.edit_price(db, 999_999, 100, actor=ACTOR, now=NOW)


def test_edit_product_is_rx_goes_through_write_path(db: Session, fake: FakePricing) -> None:
    window: dict[str, Any] = dict(promo_cents=900, promo_from=NOW - timedelta(days=1),
                  promo_to=NOW + timedelta(days=8))  # fmt: skip
    product = _seed(db, "2000000000015", "ΆΛΟΗ", 1290, **window)
    fields = dataclasses.replace(svc.ProductFields.of(product), is_rx=True)
    change = svc.edit_product(db, product.id, fields, actor=ACTOR, now=NOW)
    assert change is not None
    (call,) = fake.product_calls
    assert (call["old"].is_rx, call["new"].is_rx, call["now"]) == (False, True, NOW)
    price = db.get(Price, product.id)
    assert price is not None and price.promo_cents is None and price.promo_from is None
    last = _history(db, product.id)[-1]
    assert (last.effective_cents, last.kind, last.source) == (1290, "regular", "manual")
    assert [e.kind for e in _events(db)] == ["promo.ended_rx", "product.edited"]
    edited = _events(db, "product.edited")[0].payload
    assert edited["changes"] == {"is_rx": [False, True]}
    assert product.is_rx


def test_edit_product_without_price_effect(db: Session, fake: FakePricing) -> None:
    product = _seed(db, "2000000000015", "ΆΛΟΗ", 1290)
    fields = dataclasses.replace(
        svc.ProductFields.of(product), name="ΆΛΟΗ ΝΕΑ", pack_qty_milli=4800,
        pack_unit=PackUnit.GRAM, unit_price_required=True,
    )  # fmt: skip
    assert svc.edit_product(db, product.id, fields, actor=ACTOR, now=NOW) is None
    assert len(fake.product_calls) == 1
    assert len(_history(db, product.id)) == 1
    payload = _events(db, "product.edited")[0].payload
    assert payload["changes"]["pack_unit"] == [None, "g"]
    assert (product.name, product.pack_qty_milli, product.unit_price_required) == (
        "ΆΛΟΗ ΝΕΑ", 4800, True
    )  # fmt: skip
    # Nothing to change: nothing written.
    same = svc.ProductFields.of(product)
    assert svc.edit_product(db, product.id, same, actor=ACTOR, now=NOW) is None
    assert len(_events(db, "product.edited")) == 1


def test_edit_product_validates(db: Session) -> None:
    product = _seed(db, "2000000000015", "ΆΛΟΗ", 1290)
    base = svc.ProductFields.of(product)
    for bad in (
        dataclasses.replace(base, unit_price_required=True),
        dataclasses.replace(base, name=" "),
        dataclasses.replace(base, pack_qty_milli=0, pack_unit=PackUnit.GRAM),
    ):
        with pytest.raises(ValueError):
            svc.edit_product(db, product.id, bad, actor=ACTOR, now=NOW)


# --- End to end with the real pricing rules (waiting on workstream B) ----------------


@NEEDS_B
def test_greek_import_end_to_end(db: Session, profile: ImportProfile) -> None:
    """M1: the synthetic Greek export imports, re-import shows only real changes,
    and every change appears in history."""
    first = svc.apply_import(db, _synthetic(), "v1.csv", profile, actor=ACTOR, now=NOW)
    assert first.run.counts["new"] == 9
    gauze = _product(db, "2000000000084")
    (row,) = _history(db, gauze.id)
    assert (row.effective_cents, row.kind, row.source, row.valid_from) == (
        210, "regular", "import", NOW
    )  # fmt: skip

    assert svc.preview_import(db, _synthetic(), "v1.csv", profile).diff.counts()["unchanged"] == 9

    later = NOW + timedelta(days=1)
    edited = _synthetic_edited({";2,10\r\n": ";2,30\r\n", ";3,15\r\n": ";2,95\r\n"})
    diff = svc.preview_import(db, edited, "v2.csv", profile).diff
    assert sorted(e.row.ean for e in diff.changed) == ["2000000000084", "2000000000121"]
    svc.apply_import(db, edited, "v2.csv", profile, actor=ACTOR, now=later)
    assert [h.effective_cents for h in _history(db, gauze.id)] == [210, 230]
    paste = _product(db, "2000000000121")
    assert [(h.effective_cents, h.source) for h in _history(db, paste.id)] == [
        (315, "import"), (295, "import")
    ]  # fmt: skip
    assert _count(db, PriceHistory) == 11


@NEEDS_B
def test_manual_edit_then_import_end_to_end(db: Session, profile: ImportProfile) -> None:
    svc.apply_import(db, _synthetic(), "v1.csv", profile, actor=ACTOR, now=NOW)
    paste = _product(db, "2000000000121")
    svc.edit_price(db, paste.id, 350, actor=ACTOR, now=NOW + timedelta(hours=1))
    diff = svc.preview_import(db, _synthetic(), "v1.csv", profile).diff
    assert [
        (e.row.ean, e.old_regular_cents, e.row.regular_cents) for e in diff.manually_changed
    ] == [("2000000000121", 350, 315)]
    svc.apply_import(db, _synthetic(), "v1.csv", profile, actor=ACTOR, now=NOW + timedelta(hours=2))
    db.refresh(paste)
    assert paste.manually_changed_at is None
    assert [(h.effective_cents, h.source) for h in _history(db, paste.id)] == [
        (315, "import"), (350, "manual"), (315, "import")
    ]  # fmt: skip


@NEEDS_B
def test_setting_is_rx_ends_promo_end_to_end(db: Session) -> None:
    product = _seed(
        db, "2000000000015", "ΆΛΟΗ", 1290,
        promo_cents=900, promo_from=NOW + timedelta(days=1), promo_to=NOW + timedelta(days=8),
        promo_prior_cents=1290,
    )  # fmt: skip
    fields = dataclasses.replace(svc.ProductFields.of(product), is_rx=True)
    change = svc.edit_product(db, product.id, fields, actor=ACTOR, now=NOW)
    assert change is not None
    price = db.get(Price, product.id)
    assert price is not None and price.promo_cents is None and price.promo_prior_cents is None
    assert {e.kind for e in _events(db)} >= {"promo.ended_rx", "product.edited"}
    last = _history(db, product.id)[-1]
    assert (last.kind, last.source, last.valid_from) == ("regular", "manual", NOW)

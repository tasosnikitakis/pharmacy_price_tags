"""Catalogue services against Postgres (workstream A).

Two kinds of test:

- Storage tests replace ``core.domain.pricing.apply_price_change`` and
  ``apply_product_change`` with a recording stand-in. They check that the services
  call the shared write path with the right intent and store exactly what it returns,
  in one transaction, independently of the pricing rules themselves.
- End-to-end tests use the real pricing functions.
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
from core.domain.catalogue import PRICE_CENTS_MAX, JumpThresholds
from core.domain.pricing import NewPricePoint, PriceChange, PriceEvent, PromoRefusal
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
    # Behave like B removing a promo that a new regular price makes unlawful: the
    # returned state has no promo even though ``requested`` carried it through.
    refuse_promo: bool = False

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
        if self.refuse_promo and requested.promo_cents is not None:
            return PriceChange(
                state=PriceState(regular_cents=requested.regular_cents),
                history_rows=[
                    NewPricePoint(requested.regular_cents, PriceKind.REGULAR, now, source)
                ],
                events=[
                    PriceEvent(
                        "promo.refused",
                        {
                            "product_id": product.product_id,
                            "reason": PromoRefusal.NOT_BELOW_REGULAR,
                        },
                    )
                ],
            )
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
        "price_changed": 0, "manually_changed": 0, "price_jumps": 0,
    }  # fmt: skip
    assert [r.line for r in preview.diff.rejected] == [10, 11, 12, 13]
    assert _count(db, Product) == 0 and _count(db, ImportRun) == 0  # nothing written


def test_preview_against_catalogue(db: Session, profile: ImportProfile) -> None:
    _seed(db, "2000000000015", "ΆΛΟΗ ΚΡΕΜΑ ΧΕΡΙΩΝ", 1290)  # unchanged price, new fields
    _seed(db, "2000000000022", "ΈΛΑΙΟ ΣΩΜΑΤΟΣ ΉΠΙΟ", 800, manually_changed_at=NOW)
    gone = _seed(db, "2999999999991", "ΠΑΛΙΟ ΠΡΟΪΟΝ", 100)
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
    not_in_file = _seed(db, "2999999999991", "ΠΑΛΙΟ", 100, manually_changed_at=NOW)
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
    # Field changes from an import are audited per product.
    (edited,) = _events(db, "product.edited")
    run_id = db.scalar(select(ImportRun.id))
    assert (edited.actor, edited.at) == (ACTOR, NOW)
    assert edited.payload == {
        "product_id": hand.id,
        "ean": "2000000000015",
        "import_run_id": run_id,
        "changes": {
            "name": ["ΚΡΕΜΑ ΧΕΡΙΩΝ", "ΆΛΟΗ ΚΡΕΜΑ ΧΕΡΙΩΝ"],
            "brand": [None, "Δοκιμή Α.Ε."],
            "pack_qty_milli": [None, 75_000],
            "pack_unit": [None, "ml"],
        },
    }


def test_import_stores_returned_state_not_requested(
    db: Session, profile: ImportProfile, fake: FakePricing
) -> None:
    fake.refuse_promo = True
    promo: dict[str, Any] = dict(
        promo_cents=820, promo_from=NOW - timedelta(days=1), promo_to=NOW + timedelta(days=9),
    )  # fmt: skip
    oil = _seed(db, "2000000000022", "ΈΛΑΙΟ ΣΩΜΑΤΟΣ ΉΠΙΟ", 900, **promo)
    result = svc.apply_import(db, _synthetic(), "export.csv", profile, actor=ACTOR, now=NOW)
    call = next(c for c in fake.price_calls if c["product"].product_id == oil.id)
    assert call["requested"].promo_cents == 820  # the import carried the promo through
    price = db.get(Price, oil.id)
    assert price is not None
    assert (price.regular_cents, price.promo_cents, price.promo_from, price.promo_to) == (
        850, None, None, None
    )  # fmt: skip
    assert result.run.counts["promo_refused"] == 1
    assert [e.kind for e in result.price_events].count("promo.refused") == 1
    (refused,) = _events(db, "promo.refused")
    assert refused.payload == {"product_id": oil.id, "reason": "not_below_regular"}


def test_import_validates_filename(db: Session, profile: ImportProfile) -> None:
    long_name = "x" * 252 + ".csv"
    with pytest.raises(ValueError, match="longer than 255"):
        svc.preview_import(db, _synthetic(), long_name, profile)
    with pytest.raises(ValueError, match="longer than 255"):
        svc.apply_import(db, _synthetic(), long_name, profile, actor=ACTOR, now=NOW)


def test_profile_name_length_is_validated(db: Session) -> None:
    with pytest.raises(ValueError, match="longer than 100"):
        svc.save_profile(db, "p" * 101, ImportSettings.create(MAPPING))
    assert svc.save_profile(db, "p" * 100, ImportSettings.create(MAPPING)).name == "p" * 100


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
    with pytest.raises(ValueError, match="at most"):
        svc.edit_price(db, 1, PRICE_CENTS_MAX + 1, actor=ACTOR, now=NOW)
    with pytest.raises(ValueError, match="zero price"):
        svc.edit_price(db, 1, 0, actor=ACTOR, now=NOW)


def test_import_gives_a_price_to_a_product_without_one(
    db: Session, profile: ImportProfile, fake: FakePricing
) -> None:
    # Such a product is locked (with all the store's products) before the diff is
    # built, and every writer locks a product before its price, so no other
    # transaction can add its price row in between and make this insert collide.
    bare = Product(ean="2000000000022", name="ΈΛΑΙΟ ΣΩΜΑΤΟΣ ΉΠΙΟ")
    db.add(bare)
    db.flush()
    result = svc.apply_import(db, _synthetic(), "export.csv", profile, actor=ACTOR, now=NOW)
    entry = next(e for e in result.diff.changed if e.product_id == bare.id)
    assert entry.old_regular_cents is None and entry.price_changed
    call = next(c for c in fake.price_calls if c["product"].product_id == bare.id)
    assert call["old"] is None and call["requested"] == PriceState(850)
    price = db.get(Price, bare.id)
    assert price is not None and price.regular_cents == 850


def test_edit_price_other_store_is_not_found(db: Session, fake: FakePricing) -> None:
    product = _seed(db, "2000000000015", "ΆΛΟΗ", 1290)
    with pytest.raises(svc.NotFound):
        svc.edit_price(db, product.id, 1350, actor=ACTOR, now=NOW, store_id=2)
    assert fake.price_calls == []


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
        dataclasses.replace(base, name="Χ" * 301),
        dataclasses.replace(base, brand="Β" * 201),
    ):
        with pytest.raises(ValueError):
            svc.edit_product(db, product.id, bad, actor=ACTOR, now=NOW)
    assert _count(db, EventLog) == 0


def test_edit_product_normalises_like_import(db: Session, fake: FakePricing) -> None:
    product = _seed(db, "2000000000015", "ΆΛΟΗ ΚΡΕΜΑ", 1290)
    base = svc.ProductFields.of(product)
    # Whitespace only, and an empty brand where there is none: nothing changes.
    blank = dataclasses.replace(base, name="  ΆΛΟΗ   ΚΡΕΜΑ ", brand="   ")
    assert svc.edit_product(db, product.id, blank, actor=ACTOR, now=NOW) is None
    assert _count(db, EventLog) == 0 and fake.product_calls == []

    tidy = dataclasses.replace(base, name=" ΆΛΟΗ\tΝΕΑ ", brand="  Δοκιμή  Α.Ε. ")
    svc.edit_product(db, product.id, tidy, actor=ACTOR, now=NOW)
    assert (product.name, product.brand) == ("ΆΛΟΗ ΝΕΑ", "Δοκιμή Α.Ε.")
    (edited,) = _events(db, "product.edited")
    assert edited.payload["changes"] == {
        "name": ["ΆΛΟΗ ΚΡΕΜΑ", "ΆΛΟΗ ΝΕΑ"], "brand": [None, "Δοκιμή Α.Ε."]
    }  # fmt: skip

    cleared = dataclasses.replace(svc.ProductFields.of(product), brand=" ")
    svc.edit_product(db, product.id, cleared, actor=ACTOR, now=NOW)
    assert product.brand is None


# --- End to end with the real pricing rules -----------------------------------------


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


# --- Price-jump warning (real pricing rules) ------------------------------------------


def _seed_for_jumps(db: Session) -> dict[str, Product]:
    """Four price changes against the synthetic file, two of them over 50%."""
    return {
        "aloe": _seed(db, "2000000000015", "ΆΛΟΗ ΚΡΕΜΑ ΧΕΡΙΩΝ", 500),  # -> 1290, +158%
        "oil": _seed(db, "2000000000022", "ΈΛΑΙΟ ΣΩΜΑΤΟΣ ΉΠΙΟ", 800),  # -> 850, +6%
        "shampoo": _seed(db, "2000000000039", "ΣΑΜΠΟΥΑΝ", 2000),  # -> 640, -68%
        "vitamins": _seed(db, "2000000000046", "ΒΙΤΑΜΙΝΕΣ", 1490),  # -> 1500, +1%
    }


def test_preview_lists_price_jumps(db: Session, profile: ImportProfile) -> None:
    _seed_for_jumps(db)
    diff = svc.preview_import(db, _synthetic(), "export.csv", profile).diff
    assert [
        (j.ean, j.name, j.old_cents, j.new_cents, j.percent_change) for j in diff.price_jumps
    ] == [
        ("2000000000015", "ΆΛΟΗ ΚΡΕΜΑ ΧΕΡΙΩΝ", 500, 1290, 158),
        ("2000000000039", "ΊΡΙΣ ΣΑΜΠΟΥΑΝ ΌΛΟΙ ΟΙ ΤΥΠΟΙ", 2000, 640, -68),
    ]
    assert diff.suspicious is True  # 2 of 4 price changes = 50% > 20%
    assert diff.jump_thresholds == JumpThresholds(50, 20)
    relaxed = svc.preview_import(
        db, _synthetic(), "export.csv", profile, JumpThresholds(warn_percent=50, banner_percent=50)
    ).diff
    assert (len(relaxed.price_jumps), relaxed.suspicious) == (2, False)  # 50% is not > 50%


def test_apply_refuses_price_jumps_without_confirmation(
    db: Session, profile: ImportProfile
) -> None:
    seeded = _seed_for_jumps(db)
    with pytest.raises(svc.ImportConfirmationRequired, match="2 of 4 price changes") as err:
        svc.apply_import(db, _synthetic(), "export.csv", profile, actor=ACTOR, now=NOW)
    assert [j.ean for j in err.value.jumps] == ["2000000000015", "2000000000039"]
    assert isinstance(err.value, ValueError)
    # Nothing was written.
    assert _count(db, ImportRun) == 0 and _count(db, EventLog) == 0
    assert _count(db, Product) == 4
    price = db.get(Price, seeded["aloe"].id)
    assert price is not None and price.regular_cents == 500


def test_apply_with_confirmed_price_jumps(db: Session, profile: ImportProfile) -> None:
    seeded = _seed_for_jumps(db)
    result = svc.apply_import(
        db, _synthetic(), "export.csv", profile, actor=ACTOR, now=NOW, confirm_price_jumps=True
    )
    # The jumping rows are applied like any other.
    for key, cents in (("aloe", 1290), ("shampoo", 640), ("oil", 850)):
        price = db.get(Price, seeded[key].id)
        assert price is not None and price.regular_cents == cents
    assert result.run.counts["price_jumps"] == 2
    applied = _events(db, "import.applied")[0].payload
    assert applied["price_jumps"] == 2
    assert applied["price_jumps_suspicious"] is True
    assert applied["price_jumps_confirmed"] is True
    assert applied["jump_thresholds"] == {"warn_percent": 50, "banner_percent": 20}


def test_apply_without_banner_needs_no_confirmation(db: Session, profile: ImportProfile) -> None:
    _seed(db, "2000000000015", "ΆΛΟΗ ΚΡΕΜΑ ΧΕΡΙΩΝ", 500)  # one jump among six changes
    others = (
        ("2000000000022", 800),
        ("2000000000039", 600),
        ("2000000000046", 1400),
        ("2000000000053", 400),
        ("2000000000060", 380),
    )
    for ean, cents in others:
        _seed(db, ean, "Π", cents)
    diff = svc.preview_import(db, _synthetic(), "export.csv", profile).diff
    assert (len(diff.price_jumps), diff.suspicious) == (1, False)  # 1 of 6 = 17%
    svc.apply_import(db, _synthetic(), "export.csv", profile, actor=ACTOR, now=NOW)
    applied = _events(db, "import.applied")[0].payload
    assert (applied["price_jumps"], applied["price_jumps_suspicious"]) == (1, False)
    assert applied["price_jumps_confirmed"] is False


def test_apply_uses_custom_thresholds(db: Session, profile: ImportProfile) -> None:
    _seed_for_jumps(db)
    strict = JumpThresholds(warn_percent=7, banner_percent=50)  # 2 of 4 jump: 50%, no banner
    result = svc.apply_import(
        db, _synthetic(), "export.csv", profile, actor=ACTOR, now=NOW, thresholds=strict
    )
    assert result.run.counts["price_jumps"] == 2
    applied = _events(db, "import.applied")[0].payload
    assert applied["jump_thresholds"] == {"warn_percent": 7, "banner_percent": 50}
    assert applied["price_jumps_suspicious"] is False


def test_apply_refuses_with_custom_thresholds(db: Session, profile: ImportProfile) -> None:
    _seed_for_jumps(db)
    strict = JumpThresholds(warn_percent=7, banner_percent=49)  # 2 of 4 = 50% > 49%
    with pytest.raises(svc.ImportConfirmationRequired, match="2 of 4 price changes are over 7%"):
        svc.apply_import(
            db, _synthetic(), "export.csv", profile, actor=ACTOR, now=NOW, thresholds=strict
        )


# --- Locking ----------------------------------------------------------------------------


def test_late_product_reuses_its_price_row(
    db: Session, profile: ImportProfile, fake: FakePricing, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A product committed while the import waited for its locks is missing from the
    first lock pass. Simulate that by hiding it there: the import must lock it and its
    price row afterwards and update that row, not insert a second one."""
    late = _seed(db, "2000000000022", "ΈΛΑΙΟ ΣΩΜΑΤΟΣ ΉΠΙΟ", 800)
    lock_products, lock_prices = svc._lock_products, svc._lock_prices
    late_calls: list[Sequence[int] | None] = []

    def products(s: Session, store_id: int, ids: Sequence[int] | None = None) -> list[Product]:
        rows = lock_products(s, store_id, ids)
        late_calls.append(ids)
        return rows if ids is not None else [p for p in rows if p.id != late.id]

    def prices(s: Session, store_id: int, ids: Sequence[int] | None = None) -> list[Price]:
        rows = lock_prices(s, store_id, ids)
        return rows if ids is not None else [p for p in rows if p.product_id != late.id]

    monkeypatch.setattr(svc, "_lock_products", products)
    monkeypatch.setattr(svc, "_lock_prices", prices)
    svc.apply_import(db, _synthetic(), "export.csv", profile, actor=ACTOR, now=NOW)

    assert late_calls == [None, [late.id]]  # the late path ran
    call = next(c for c in fake.price_calls if c["product"].product_id == late.id)
    assert call["old"] == PriceState(800)  # the existing row was found and passed in
    assert db.scalar(select(func.count()).where(Price.product_id == late.id)) == 1
    price = db.get(Price, late.id)
    assert price is not None and price.regular_cents == 850


# --- Promos carried by an import (real pricing rules) ----------------------------------


def test_import_drops_an_expired_promo_silently(db: Session, profile: ImportProfile) -> None:
    oil = _seed(
        db, "2000000000022", "ΈΛΑΙΟ ΣΩΜΑΤΟΣ ΉΠΙΟ", 800,
        promo_cents=700, promo_from=NOW - timedelta(days=20), promo_to=NOW - timedelta(days=1),
    )  # fmt: skip
    result = svc.apply_import(db, _synthetic(), "export.csv", profile, actor=ACTOR, now=NOW)
    price = db.get(Price, oil.id)
    assert price is not None
    assert (price.regular_cents, price.promo_cents, price.promo_from, price.promo_to) == (
        850, None, None, None
    )  # fmt: skip
    # Rule 0: an expired promo is housekeeping, not a refusal.
    assert result.run.counts["promo_refused"] == 0
    assert _events(db, "promo.refused") == []
    last = _history(db, oil.id)[-1]
    assert (last.effective_cents, last.kind, last.source, last.valid_from) == (
        850, "regular", "import", NOW
    )  # fmt: skip


def test_import_below_a_running_promo_refuses_the_promo(
    db: Session, profile: ImportProfile
) -> None:
    # Regular 12,00 with a running promo at 9,00 (prior price 12,00 from history). The
    # file lowers the regular price to 8,50, below the promo: rule 3 removes the promo.
    oil = _seed(
        db, "2000000000022", "ΈΛΑΙΟ ΣΩΜΑΤΟΣ ΉΠΙΟ", 1200,
        promo_cents=900, promo_from=NOW - timedelta(days=1), promo_to=NOW + timedelta(days=9),
    )  # fmt: skip
    result = svc.apply_import(db, _synthetic(), "export.csv", profile, actor=ACTOR, now=NOW)
    price = db.get(Price, oil.id)
    assert price is not None
    assert (price.regular_cents, price.promo_cents, price.promo_from, price.promo_to) == (
        850, None, None, None
    )  # fmt: skip
    assert result.run.counts["promo_refused"] == 1
    (refused,) = _events(db, "promo.refused")
    assert refused.payload["product_id"] == oil.id
    assert refused.payload["reason"] == "not_below_regular"
    assert [e.kind for e in result.price_events].count("promo.refused") == 1
    # History never shows the refused promo price from now on.
    last = _history(db, oil.id)[-1]
    assert (last.effective_cents, last.kind, last.valid_from) == (850, "regular", NOW)

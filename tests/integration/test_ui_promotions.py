"""``core.services.promotions.edit_promo``: the promo write path of the Products page."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.db.models import EventLog, Price, PriceHistory, Product
from core.domain.pricing import PromoRefusal, PromoRefused
from core.services.catalogue import NotFound
from core.services.promotions import (
    NoPrice,
    PromoInput,
    edit_promo,
    end_promo,
    last_day,
    promo_end,
    promo_start,
)
from core.services.tags import real_label_pipeline
from tests.integration.test_ui_helpers import (
    T0,
    add_product,
    add_tag,
    add_tag_type_row,
    suffix,
)

pytestmark = pytest.mark.db

WEEK = PromoInput(990, T0, T0 + timedelta(days=7))


def _price(db: Session, product: Product) -> Price:
    price = db.scalars(select(Price).where(Price.product_id == product.id)).one()
    db.refresh(price)
    return price


def _history(db: Session, product: Product) -> list[PriceHistory]:
    return list(
        db.scalars(
            select(PriceHistory)
            .where(PriceHistory.product_id == product.id)
            .order_by(PriceHistory.id)
        )
    )


def _events(db: Session, product: Product) -> list[str]:
    rows = db.scalars(select(EventLog).order_by(EventLog.id))
    return [e.kind for e in rows if e.payload.get("product_id") == product.id]


def test_promo_is_stored_with_history_and_events(db: Session) -> None:
    product = add_product(db, f"Κρέμα {suffix()}", 1290)
    tag = add_tag(db, add_tag_type_row(db), product)
    before = tag.desired_hash
    result = edit_promo(db, product.id, WEEK, actor="admin@example.test", now=T0)
    assert result.change is not None and not result.prior_saved and not result.promo_removed
    price = _price(db, product)
    assert (price.promo_cents, price.promo_from, price.promo_to) == (990, T0, WEEK.promo_to)
    rows = _history(db, product)[-2:]
    assert [(r.effective_cents, r.kind, r.valid_from, r.source) for r in rows] == [
        (990, "promo", T0, "manual"),
        (1290, "regular", WEEK.promo_to, "promo"),
    ]
    assert _events(db, product)[-1] == "promo.edited"
    db.refresh(tag)
    assert tag.desired_hash != before  # the bound tag now shows the promo label


def test_same_promo_writes_nothing(db: Session) -> None:
    product = add_product(db, f"Κρέμα {suffix()}", 1290)
    edit_promo(db, product.id, WEEK, actor="a", now=T0)
    count = len(_history(db, product))
    assert edit_promo(db, product.id, WEEK, actor="a", now=T0 + timedelta(hours=1)).change is None
    assert len(_history(db, product)) == count


def test_refusals_pass_through_and_write_nothing(db: Session) -> None:
    product = add_product(db, f"Κρέμα {suffix()}", 1290)
    count = len(_history(db, product))
    with pytest.raises(PromoRefused) as refused:
        edit_promo(db, product.id, PromoInput(1290, T0, T0 + timedelta(days=1)), actor="a", now=T0)
    assert refused.value.reason is PromoRefusal.NOT_A_DISCOUNT
    with pytest.raises(PromoRefused) as refused:
        edit_promo(db, product.id, PromoInput(990, T0 - timedelta(hours=1), T0 + timedelta(days=1)),
                   actor="a", now=T0)  # fmt: skip
    assert refused.value.reason is PromoRefusal.STARTS_IN_PAST
    assert len(_history(db, product)) == count
    assert _price(db, product).promo_cents is None


def test_rx_product_is_refused(db: Session) -> None:
    product = add_product(db, f"Φάρμακο {suffix()}", 1290, is_rx=True)
    with pytest.raises(PromoRefused) as refused:
        edit_promo(db, product.id, WEEK, actor="a", now=T0)
    assert refused.value.reason is PromoRefusal.RX_PRODUCT


def test_entered_prior_price(db: Session) -> None:
    product = add_product(db, f"Νέο {suffix()}", 1290, since=T0 - timedelta(days=2))
    with pytest.raises(PromoRefused) as refused:
        edit_promo(db, product.id, WEEK, actor="a", now=T0)
    assert refused.value.reason is PromoRefusal.NO_PRIOR_PRICE
    result = edit_promo(db, product.id, WEEK, entered_prior_cents=1390, actor="a", now=T0)
    assert result.prior_saved
    assert _price(db, product).promo_prior_cents == 1390
    # Re-entering the same prior with an unchanged promo is still logged.
    again = edit_promo(db, product.id, WEEK, entered_prior_cents=1390, actor="a",
                       now=T0 + timedelta(minutes=5))  # fmt: skip
    assert again.prior_saved
    assert _events(db, product).count("promo.prior_set") == 2


def test_ending_drops_entered_prior(db: Session) -> None:
    product = add_product(db, f"Νέο {suffix()}", 1290, since=T0 - timedelta(days=2))
    edit_promo(db, product.id, WEEK, entered_prior_cents=1390, actor="a", now=T0)
    result = edit_promo(db, product.id, None, entered_prior_cents=1390, actor="a",
                        now=T0 + timedelta(hours=1))  # fmt: skip
    assert result.promo_removed and not result.prior_saved
    price = _price(db, product)
    assert price.promo_cents is None and price.promo_prior_cents is None


def test_end_promo(db: Session) -> None:
    product = add_product(db, f"Κρέμα {suffix()}", 1290)
    edit_promo(db, product.id, WEEK, actor="a", now=T0)
    result = end_promo(db, product.id, actor="a", now=T0 + timedelta(days=1))
    assert result.promo_removed
    assert _history(db, product)[-1].effective_cents == 1290
    assert end_promo(db, product.id, actor="a", now=T0 + timedelta(days=2)).change is None


def test_errors(db: Session) -> None:
    with pytest.raises(NotFound):
        edit_promo(db, 999_999_999, WEEK, actor="a", now=T0)
    bare = Product(ean=f"2{suffix()}0000"[:13], name="Χωρίς τιμή")
    db.add(bare)
    db.flush()
    with pytest.raises(NoPrice):
        edit_promo(db, bare.id, WEEK, actor="a", now=T0)
    product = add_product(db, f"Κρέμα {suffix()}", 1290)
    with pytest.raises(ValueError):
        edit_promo(db, product.id, PromoInput(0, T0, T0 + timedelta(days=1)), actor="a", now=T0)


def test_pipeline_is_injectable(db: Session) -> None:
    product = add_product(db, f"Κρέμα {suffix()}", 1290)
    add_tag(db, add_tag_type_row(db), product)
    calls: list[int] = []

    def pipeline(request, now):  # type: ignore[no-untyped-def]
        calls.append(request.tag_id)
        return real_label_pipeline(request, now)

    edit_promo(db, product.id, WEEK, actor="a", now=T0, pipeline=pipeline)
    assert len(calls) == 1


def test_form_dates() -> None:
    from zoneinfo import ZoneInfo

    from core.domain.types import PriceState

    tz = ZoneInfo("Europe/Athens")
    today = T0.astimezone(tz).date()
    assert promo_start(None, None, T0, tz) == T0
    assert promo_start(None, today, T0, tz) == T0
    later = promo_start(None, today + timedelta(days=2), T0, tz)
    assert later.isoformat() == "2026-12-03T00:00:00+02:00"
    running = PriceState(1290, 990, T0 - timedelta(days=1), T0 + timedelta(days=3))
    assert promo_start(running, None, T0, tz) == running.promo_from
    assert promo_start(running, (T0 - timedelta(days=1)).astimezone(tz).date(), T0, tz) == (
        running.promo_from
    )
    end = promo_end(today, tz)
    assert end.isoformat() == "2026-12-02T00:00:00+02:00"
    assert last_day(end, tz) == today

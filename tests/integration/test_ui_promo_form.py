"""The promo form on the Products page: a running promo's fixed start (decision 0009),
date bounds, a promo removed on re-validation, and the manual prior-price pre-fill."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from core.app.formatting import display_tz
from core.app.i18n import error_message, t
from core.db.models import Price
from core.services.promotions import PromoInput, edit_promo, promo_end
from tests.integration.test_ui_helpers import (
    T0,
    UI,
    add_product,
    suffix,
    ui_fixture,  # noqa: F401  (the "ui" fixture)
)

pytestmark = pytest.mark.db


def _price(ui: UI, product_id: int) -> Price:
    price = ui.db.scalars(select(Price).where(Price.product_id == product_id)).one()
    ui.db.refresh(price)
    return price


def _day(days: int) -> str:
    return (T0.astimezone(display_tz()) + timedelta(days=days)).date().isoformat()


def _promo(ui: UI, product_id: int, token: str, **data: str) -> str:
    r = ui.client.post(f"/products/{product_id}/promo", data={"csrf_token": token, **data})
    return str(r.text)


def _input(page: str, field_id: str) -> str:
    """The opening tag of the element with ``id=field_id``."""
    return page.split(f'id="{field_id}"', 1)[1].split(">", 1)[0]


def test_running_promo_form_keeps_start_fixed(ui: UI) -> None:
    product = add_product(ui.db, f"Νέο {suffix()}", 1290, since=T0 - timedelta(days=3))
    start = T0 - timedelta(days=1)
    edit_promo(ui.db, product.id, PromoInput(990, start, T0 + timedelta(days=5)),
               entered_prior_cents=1390, actor="test", now=start)  # fmt: skip
    ui.db.commit()
    ui.as_admin()
    page = ui.client.get(f"/products/{product.id}").text
    assert 'id="start"' not in page  # no editable start whose min it would fail
    assert f'<input type="hidden" name="start" value="{_day(-1)}">' in page
    assert t("products.promo.running_since", date="30/11/2026") in page
    # A running promo with an unchanged start: the stored manual prior is pre-filled.
    assert 'value="13,90"' in _input(page, "prior_price")
    assert 'id="prior-previous"' not in page


def test_running_promo_end_and_price_can_be_edited(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    start = T0 - timedelta(days=1)
    edit_promo(ui.db, product.id, PromoInput(990, start, T0 + timedelta(days=5)),
               actor="test", now=start)  # fmt: skip
    ui.db.commit()
    token = ui.as_admin()
    page = _promo(ui, product.id, token, promo_price="8,90", start=_day(-1), last=_day(9))
    assert t("products.promo.saved") in page
    price = _price(ui, product.id)
    assert (price.promo_cents, price.promo_from) == (890, start)


def test_moved_start_on_running_promo_is_refused(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    start = T0 - timedelta(days=1)
    edit_promo(ui.db, product.id, PromoInput(990, start, T0 + timedelta(days=5)),
               actor="test", now=start)  # fmt: skip
    ui.db.commit()
    token = ui.as_admin()
    page = _promo(ui, product.id, token, promo_price="8,90", start=_day(-2), last=_day(6))
    assert t("promo.refusal.starts_in_past") in page
    price = _price(ui, product.id)
    assert (price.promo_cents, price.promo_from) == (990, start)


@pytest.mark.parametrize(
    ("start", "last", "code"),
    [("", "9999-12-31", "promo.date_out_of_range"),
     ("0001-01-01", "2026-12-05", "promo.date_out_of_range"),
     ("", "2032-01-01", "promo.date_out_of_range"),
     ("2020-01-01", "", "promo.date_out_of_range"),
     ("2026-12-02", "", "promo.dates_invalid")],
)  # fmt: skip
def test_far_off_dates_are_refused_in_greek(ui: UI, start: str, last: str, code: str) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    token = ui.as_admin()
    r = ui.client.post(
        f"/products/{product.id}/promo",
        data={"csrf_token": token, "promo_price": "9,90", "start": start, "last": last},
    )
    assert r.status_code == 422
    assert error_message(code) in r.text
    assert _price(ui, product.id).promo_cents is None


def test_date_five_years_ahead_is_accepted(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    token = ui.as_admin()
    page = _promo(ui, product.id, token, promo_price="9,90", last="2031-11-30")
    assert t("products.promo.saved") in page
    assert 'max="2031-12-01"' in _input(page, "last")


def test_promo_removed_on_revalidation_shows_reason(ui: UI) -> None:
    product = add_product(ui.db, f"Νέο {suffix()}", 1290, since=T0 - timedelta(days=3))
    # Ends after its last day, as the form would store it, so resubmitting is no change.
    end = promo_end(date.fromisoformat(_day(6)), display_tz())
    edit_promo(ui.db, product.id, PromoInput(990, T0, end),
               entered_prior_cents=1390, actor="test", now=T0)  # fmt: skip
    ui.db.commit()
    token = ui.as_admin()
    # The same promo with a prior price below it: rule 3 removes it as no discount.
    page = _promo(ui, product.id, token, promo_price="9,90", start=_day(0), last=_day(6),
                  prior_price="9,00")  # fmt: skip
    reason = t("promo.refusal.not_a_discount")
    assert t("products.promo.removed_refused", reason=reason) in page
    assert t("products.promo.ended") not in page
    assert t("products.promo.prior_saved") not in page
    assert _price(ui, product.id).promo_cents is None


def test_prior_is_not_prefilled_for_scheduled_or_new_promo(ui: UI) -> None:
    product = add_product(ui.db, f"Νέο {suffix()}", 1290, since=T0 - timedelta(days=3))
    start = T0 + timedelta(days=3)
    edit_promo(ui.db, product.id, PromoInput(990, start, start + timedelta(days=7)),
               entered_prior_cents=1390, actor="test", now=T0)  # fmt: skip
    ui.db.commit()
    ui.as_admin()
    page = ui.client.get(f"/products/{product.id}").text
    assert 'value=""' in _input(page, "prior_price")  # cleared on change by default
    assert t("products.promo.prior_previous", price="13,90 €") in page
    assert f'min="{_day(0)}"' in _input(page, "start")  # a scheduled start can move
    # A product with no promo: the field is empty too, with no hint.
    other = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    page = ui.client.get(f"/products/{other.id}").text
    assert 'value=""' in _input(page, "prior_price")
    assert 'id="prior-previous"' not in page

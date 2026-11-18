"""Products page (section 9): search, price and field edits, promos, preview, history."""

from __future__ import annotations

import io
from datetime import timedelta

import pytest
from PIL import Image
from sqlalchemy import select

from core.app.formatting import display_tz
from core.app.i18n import error_message, t
from core.db.models import EventLog, Price, PriceHistory, Tag
from core.domain.pricing import PromoRefusal
from core.services.promotions import PromoInput, edit_promo
from shared.tag_types import Palette
from tests.integration.test_ui_helpers import (
    T0,
    UI,
    add_product,
    add_tag,
    add_tag_type_row,
    suffix,
    ui_fixture,  # noqa: F401  (the "ui" fixture)
)

pytestmark = pytest.mark.db


def _price(ui: UI, product_id: int) -> Price:
    price = ui.db.scalars(select(Price).where(Price.product_id == product_id)).one()
    ui.db.refresh(price)
    return price


def _events(ui: UI, kind: str) -> list[EventLog]:
    return list(ui.db.scalars(select(EventLog).where(EventLog.kind == kind)))


def _day(days: int) -> str:
    return (T0.astimezone(display_tz()) + timedelta(days=days)).date().isoformat()


# --- Search ---------------------------------------------------------------------------


def test_search_by_name_and_ean(ui: UI) -> None:
    word = f"Ζελέ{suffix()}"
    product = add_product(ui.db, f"Αντηλιακό {word} SPF50", 1290)
    add_product(ui.db, f"Άλλο προϊόν {suffix()}", 500)
    ui.as_staff()
    by_name = ui.client.get("/products", params={"q": word.lower()})
    assert f"/products/{product.id}" in by_name.text
    assert "12,90 €" in by_name.text
    by_ean = ui.client.get("/products", params={"q": product.ean[:12]})
    assert f"/products/{product.id}" in by_ean.text
    none = ui.client.get("/products", params={"q": f"όχι-{suffix()}"})
    assert t("products.none") in none.text


def test_search_treats_wildcards_literally(ui: UI) -> None:
    ui.as_staff()
    page = ui.client.get("/products", params={"q": "%_%"})
    assert page.status_code == 200


# --- Price ----------------------------------------------------------------------------


def test_admin_edits_price_and_tags_get_new_label(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    tag = add_tag(ui.db, add_tag_type_row(ui.db), product)
    before = tag.desired_hash
    token = ui.as_admin()
    r = ui.client.post(
        f"/products/{product.id}/price",
        data={"regular_price": "13,50", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _price(ui, product.id).regular_cents == 1350
    page = ui.client.get(r.headers["location"])
    assert t("products.price.saved", price="13,50 €") in page.text
    ui.db.refresh(tag)
    assert tag.desired_hash != before
    rows = ui.db.scalars(select(PriceHistory).where(PriceHistory.product_id == product.id)).all()
    assert rows[-1].effective_cents == 1350 and rows[-1].source == "manual"
    edits = [e for e in _events(ui, "price.manual") if e.payload["product_id"] == product.id]
    assert edits[-1].payload["new_regular_cents"] == 1350
    assert edits[-1].actor == ui.admin.email


@pytest.mark.parametrize(
    ("text", "code"),
    [("0", "price.zero"), ("0,00", "price.zero"), ("abc", "price.invalid"),
     ("12,345", "price.invalid"), ("-3", "price.invalid"), ("99999999", "price.too_large")],
)  # fmt: skip
def test_invalid_price_is_refused_in_greek(ui: UI, text: str, code: str) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    token = ui.as_admin()
    r = ui.client.post(
        f"/products/{product.id}/price", data={"regular_price": text, "csrf_token": token}
    )
    assert r.status_code == 422
    assert error_message(code) in r.text
    assert _price(ui, product.id).regular_cents == 1290


def test_same_price_writes_nothing(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    token = ui.as_admin()
    r = ui.client.post(
        f"/products/{product.id}/price", data={"regular_price": "12.90", "csrf_token": token}
    )
    assert t("products.price.unchanged") in r.text


def test_staff_sees_no_edit_forms(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    ui.as_staff()
    page = ui.client.get(f"/products/{product.id}")
    assert page.status_code == 200
    assert f'action="/products/{product.id}/price"' not in page.text
    assert f'action="/products/{product.id}/promo"' not in page.text


# --- Fields ---------------------------------------------------------------------------


def _fields(**overrides: str) -> dict[str, str]:
    data = {"name": "Κρέμα", "brand": "", "pack_qty": "", "pack_unit": "", "active": "true"}
    data.update(overrides)
    return data


def test_edit_fields(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    token = ui.as_admin()
    data = _fields(name="  Κρέμα  ημέρας ", brand="Korres", pack_qty="4,8", pack_unit="g",
                   unit_price_required="true", csrf_token=token)  # fmt: skip
    r = ui.client.post(f"/products/{product.id}/fields", data=data)
    assert t("products.fields.saved") in r.text
    ui.db.refresh(product)
    assert (product.name, product.brand, product.pack_qty_milli, product.pack_unit) == (
        "Κρέμα ημέρας", "Korres", 4800, "g",
    )  # fmt: skip
    assert product.unit_price_required and product.active and not product.is_rx
    assert 'value="4,8"' in r.text


@pytest.mark.parametrize(
    ("overrides", "code"),
    [({"name": " "}, "product.name_empty"), ({"pack_qty": "abc", "pack_unit": "g"},
     "product.pack_invalid"), ({"pack_qty": "5"}, "product.pack_invalid"),
     ({"unit_price_required": "true"}, "product.unit_price_needs_pack")],
)  # fmt: skip
def test_invalid_fields_are_refused(ui: UI, overrides: dict[str, str], code: str) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    token = ui.as_admin()
    r = ui.client.post(
        f"/products/{product.id}/fields", data=_fields(csrf_token=token, **overrides)
    )
    assert r.status_code == 422
    assert error_message(code) in r.text


def test_setting_rx_ends_promo(ui: UI) -> None:
    product = add_product(ui.db, f"Φάρμακο {suffix()}", 1000)
    edit_promo(ui.db, product.id, PromoInput(800, T0, T0 + timedelta(days=5)),
               actor="test", now=T0)  # fmt: skip
    ui.db.commit()
    token = ui.as_admin()
    r = ui.client.post(
        f"/products/{product.id}/fields",
        data=_fields(name=product.name, is_rx="true", csrf_token=token),
    )
    assert t("products.promo.ended_rx") in r.text
    assert t("products.promo.rx_not_allowed") in r.text
    assert _price(ui, product.id).promo_cents is None


# --- Promo ----------------------------------------------------------------------------


def _promo(ui: UI, product_id: int, token: str, **data: str) -> str:
    r = ui.client.post(f"/products/{product_id}/promo", data={"csrf_token": token, **data})
    return str(r.text)


def test_set_promo_from_today(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    tag = add_tag(ui.db, add_tag_type_row(ui.db), product)
    token = ui.as_admin()
    page = _promo(ui, product.id, token, promo_price="9,90", start=_day(0), last=_day(6))
    assert t("products.promo.saved") in page
    assert t("products.promo.prior_saved") not in page
    price = _price(ui, product.id)
    assert price.promo_cents == 990
    assert price.promo_from == T0  # "from today" starts when saved
    # Last day 07/12 inclusive: promo_to is 08/12 00:00 Athens time.
    assert price.promo_to is not None
    assert price.promo_to.astimezone(display_tz()).isoformat() == "2026-12-08T00:00:00+02:00"
    assert t("products.promo.running") in page
    ui.db.refresh(tag)
    assert t("template.promo") in page  # the preview shows the promo template
    assert [e for e in _events(ui, "promo.edited") if e.payload["product_id"] == product.id]


def test_promo_must_be_a_discount(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    token = ui.as_admin()
    page = _promo(ui, product.id, token, promo_price="12,90", last=_day(6))
    assert t(f"promo.refusal.{PromoRefusal.NOT_A_DISCOUNT.value}") in page
    assert _price(ui, product.id).promo_cents is None


def test_promo_cannot_start_in_the_past(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    token = ui.as_admin()
    page = _promo(ui, product.id, token, promo_price="9,90", start=_day(-1), last=_day(6))
    assert t("promo.refusal.starts_in_past") in page


def test_short_history_needs_entered_prior_price(ui: UI) -> None:
    product = add_product(ui.db, f"Νέο {suffix()}", 1290, since=T0 - timedelta(days=3))
    token = ui.as_admin()
    page = _promo(ui, product.id, token, promo_price="9,90", last=_day(6))
    assert t("promo.refusal.no_prior_price") in page
    page = _promo(ui, product.id, token, promo_price="9,90", last=_day(6), prior_price="13,90")
    assert t("products.promo.saved") in page
    assert t("products.promo.prior_saved") in page
    assert _price(ui, product.id).promo_prior_cents == 1390
    # The form pre-fills the stored manual prior price.
    form = page.split('id="prior_price"', 1)[1].split(">", 1)[0]
    assert 'value="13,90"' in form


def test_removing_promo_in_same_edit_does_not_claim_prior_saved(ui: UI) -> None:
    product = add_product(ui.db, f"Νέο {suffix()}", 1290, since=T0 - timedelta(days=3))
    edit_promo(ui.db, product.id, PromoInput(990, T0, T0 + timedelta(days=5)),
               entered_prior_cents=1390, actor="test", now=T0)  # fmt: skip
    ui.db.commit()
    ui.clock.now = T0 + timedelta(days=1)
    token = ui.as_admin()
    # Ending a running promo by moving its last day before today removes it; the
    # entered prior price is then ignored by apply_price_change.
    page = _promo(ui, product.id, token, promo_price="9,90", start=_day(0), last=_day(-1),
                  prior_price="13,90")  # fmt: skip
    assert t("products.promo.ended") in page
    assert t("products.promo.prior_saved") not in page
    price = _price(ui, product.id)
    assert price.promo_cents is None and price.promo_prior_cents is None


def test_running_promo_keeps_its_start_when_edited(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    edit_promo(ui.db, product.id, PromoInput(990, T0, T0 + timedelta(days=5)),
               actor="test", now=T0)  # fmt: skip
    ui.db.commit()
    ui.clock.now = T0 + timedelta(days=2)
    token = ui.as_admin()
    page = _promo(ui, product.id, token, promo_price="8,90", start=_day(0), last=_day(9))
    assert t("products.promo.saved") in page
    price = _price(ui, product.id)
    assert price.promo_from == T0 and price.promo_cents == 890


def test_end_promo_now(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    edit_promo(ui.db, product.id, PromoInput(990, T0, T0 + timedelta(days=5)),
               actor="test", now=T0)  # fmt: skip
    ui.db.commit()
    token = ui.as_admin()
    r = ui.client.post(f"/products/{product.id}/promo/end", data={"csrf_token": token})
    assert t("products.promo.ended") in r.text
    assert _price(ui, product.id).promo_cents is None


def test_rx_product_cannot_get_promo(ui: UI) -> None:
    product = add_product(ui.db, f"Φάρμακο {suffix()}", 1290, is_rx=True)
    token = ui.as_admin()
    page = ui.client.get(f"/products/{product.id}").text
    assert t("products.promo.rx_not_allowed") in page
    assert f'action="/products/{product.id}/promo"' not in page
    page = _promo(ui, product.id, token, promo_price="9,90", last=_day(6))
    assert error_message("promo.rx") in page
    assert _price(ui, product.id).promo_cents is None


# --- Preview and history --------------------------------------------------------------


def test_label_preview_is_enlarged_render(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    tag_type = add_tag_type_row(ui.db)
    ui.as_staff()
    page = ui.client.get(f"/products/{product.id}", params={"tag_type": tag_type.key}).text
    assert f"/products/{product.id}/preview.png?tag_type=" in page
    r = ui.client.get(f"/products/{product.id}/preview.png", params={"tag_type": tag_type.key})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(r.content)) as image:
        assert image.size == (296 * 3, 128 * 3)
        assert image.mode == "P"
        # Nearest neighbour: every 3x3 block is one colour.
        pixels = image.load()
        assert pixels is not None
        for x in range(0, 296 * 3, 3):
            for y in range(0, 128 * 3, 3):
                assert pixels[x, y] == pixels[x + 2, y + 2] == pixels[x + 1, y]


def test_preview_url_changes_with_the_price(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    add_tag_type_row(ui.db)
    token = ui.as_admin()
    before = ui.client.get(f"/products/{product.id}").text
    ui.client.post(
        f"/products/{product.id}/price", data={"regular_price": "15", "csrf_token": token}
    )
    after = ui.client.get(f"/products/{product.id}").text
    marker = "preview.png?"
    assert before.split(marker)[1][:120] != after.split(marker)[1][:120]


def test_promo_on_black_and_white_tag_has_no_png(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    edit_promo(ui.db, product.id, PromoInput(990, T0, T0 + timedelta(days=5)),
               actor="test", now=T0)  # fmt: skip
    ui.db.commit()
    tag_type = add_tag_type_row(ui.db, Palette.BW)
    ui.as_staff()
    r = ui.client.get(f"/products/{product.id}/preview.png", params={"tag_type": tag_type.key})
    assert r.status_code == 404


def test_history_in_athens_time(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    ui.as_staff()
    page = ui.client.get(f"/products/{product.id}").text
    # Written at T0 - 40 days = 22/10/2026 08:00 UTC, which is 11:00 in Athens (EEST).
    assert "22/10/2026 11:00" in page
    assert t("history.source.manual") in page


def test_bound_tags_are_listed(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    tag = add_tag(ui.db, add_tag_type_row(ui.db), product)
    ui.as_staff()
    page = ui.client.get(f"/products/{product.id}").text
    assert tag.short_code in page
    assert ui.db.get(Tag, tag.id) is not None

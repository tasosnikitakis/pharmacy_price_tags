"""Audit by product barcode (decision 0018), through the real app on synthetic data.

The tag-code lookup is also covered in ``test_ui_bind.py``; here it is checked again
together with the EAN-versus-code precedence.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import pytest
from sqlalchemy import event

from core.app.i18n import error_message, t
from core.app.routers import ui_audit
from core.db.models import Product, Tag, TagTypeRow
from core.services.tags import bind_tag
from tests.integration.test_ui_helpers import (
    T0,
    UI,
    add_product,
    add_tag,
    add_tag_type_row,
    suffix,
    synthetic_ean,
    ui_fixture,  # noqa: F401  (the "ui" fixture)
)

pytestmark = pytest.mark.db

#: A 64-character hash that is in no ``render`` row.
GONE = "ab" * 32


def section(page: str, code: str) -> str:
    found = re.search(rf'<section[^>]*id="audit-result-{code}".*?</section>', page, re.DOTALL)
    assert found is not None, f"no audit section for {code}"
    return found.group(0)


def codes_listed(page: str) -> list[str]:
    return re.findall(r'class="card audit-tag"[^>]*data-code="([^"]+)"', page)


def confirmed_of(html: str) -> str:
    found = re.search(r'data-confirmed="([a-z_]+)"', html)
    assert found is not None
    return found.group(1)


def up_to_date_tag(ui: UI, tag_type: TagTypeRow, product: Product) -> Tag:
    tag = add_tag(ui.db, tag_type, product)
    tag.displayed_hash = tag.desired_hash
    ui.db.commit()
    return tag


def newly_bound_tag(ui: UI, tag_type: TagTypeRow, product: Product) -> Tag:
    """A tag that confirmed its "unassigned" label (still in the render store) and was
    then bound: pending, with a different ``displayed_hash``."""
    tag = add_tag(ui.db, tag_type)
    tag.displayed_hash = tag.desired_hash
    ui.db.commit()
    bind_tag(ui.db, tag.id, product.id, actor="test", now=T0)
    ui.db.commit()
    ui.db.refresh(tag)
    assert tag.displayed_hash is not None and tag.displayed_hash != tag.desired_hash
    return tag


def test_product_lists_every_bound_tag_with_its_status(ui: UI) -> None:
    tag_type = add_tag_type_row(ui.db)
    product = add_product(ui.db, f"Σιρόπι {suffix()}", 1290)
    done = up_to_date_tag(ui, tag_type, product)
    done.last_seen_at, done.battery_pct, done.rssi = T0 - timedelta(minutes=5), 64, -71
    pending = newly_bound_tag(ui, tag_type, product)
    other = add_tag(ui.db, tag_type, add_product(ui.db, f"Άλλο {suffix()}", 100))
    ui.db.commit()
    ui.as_staff()

    page = ui.client.get("/audit", params={"code": product.ean}).text
    assert product.name in page and 'id="audit-product-price">12,90 €<' in page
    assert t("audit.product.tag_count", count=2) in page
    assert codes_listed(page) == sorted([done.short_code, pending.short_code])
    assert other.short_code not in codes_listed(page)

    first = section(page, done.short_code)
    assert 'data-status="up_to_date"' in first and t("tags.status.up_to_date") in first
    assert f"/tags/{done.id}/label/desired.png?v={done.desired_hash}" in first
    assert confirmed_of(first) == "same" and t("audit.confirmed.same") in first
    assert re.search(r'id="audit-price-[A-Z0-9]+">12,90 €<', first)
    assert "01/12/2026 09:55" in first  # Athens time, Greek format
    assert t("tags.battery_pct", value=64) in first and t("tags.rssi", value=-71) in first

    second = section(page, pending.short_code)
    assert 'data-status="pending"' in second and t("tags.status.pending") in second
    assert f"/tags/{pending.id}/label/desired.png?v={pending.desired_hash}" in second
    assert t("tags.never") in second
    # The field is empty and focused for the next scan.
    assert re.search(r'id="code" name="code" value=""[^>]*autofocus', page)


def test_confirmed_image_shown_when_kept_and_explained_when_not(ui: UI) -> None:
    tag_type = add_tag_type_row(ui.db)
    product = add_product(ui.db, f"Κρέμα {suffix()}", 750)
    kept = newly_bound_tag(ui, tag_type, product)
    gone = add_tag(ui.db, tag_type, product)
    gone.displayed_hash = GONE
    never = add_tag(ui.db, tag_type, product)
    assert never.displayed_hash is None
    ui.db.commit()
    ui.as_staff()

    page = ui.client.get("/audit", params={"code": product.ean}).text
    html = section(page, kept.short_code)
    assert confirmed_of(html) == "image"
    url = f"/tags/{kept.id}/label/displayed.png?v={kept.displayed_hash}"
    assert url in html
    image = ui.client.get(url)
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"

    html = section(page, gone.short_code)
    assert confirmed_of(html) == "not_kept" and t("audit.confirmed.not_kept") in html
    assert "displayed.png" not in html

    html = section(page, never.short_code)
    assert confirmed_of(html) == "unknown" and t("audit.confirmed.unknown") in html
    assert "displayed.png" not in html


def test_confirmed_image_on_a_tag_lookup(ui: UI) -> None:
    tag = newly_bound_tag(
        ui, add_tag_type_row(ui.db), add_product(ui.db, f"Αλοιφή {suffix()}", 200)
    )
    ui.as_staff()
    page = ui.client.get("/audit", params={"code": tag.short_code}).text
    assert f"/tags/{tag.id}/label/displayed.png?v={tag.displayed_hash}" in page
    tag.displayed_hash = GONE
    ui.db.commit()
    page = ui.client.get("/audit", params={"code": tag.short_code}).text
    assert t("audit.confirmed.not_kept") in page


def test_product_without_tags_offers_to_bind(ui: UI) -> None:
    product = add_product(ui.db, f"Βιταμίνη {suffix()}", 1999)
    ui.as_staff()
    page = ui.client.get("/audit", params={"code": f" {product.ean} "}).text
    assert product.name in page and 'id="audit-product-price">19,99 €<' in page
    assert t("audit.product.no_tags") in page
    assert f'href="/bind?ean={quote(product.ean)}"' in page
    assert codes_listed(page) == []


def test_unknown_ean(ui: UI) -> None:
    ean = synthetic_ean()
    ui.as_staff()
    page = ui.client.get("/audit", params={"code": ean}).text
    assert t("audit.ean_unknown", ean=ean) in page
    assert 'id="audit-product"' not in page and codes_listed(page) == []


def test_tag_code_lookup_and_ean_precedence(ui: UI) -> None:
    tag_type = add_tag_type_row(ui.db)
    product = add_product(ui.db, f"Σαμπουάν {suffix()}", 880)
    bound = add_tag(ui.db, tag_type, product)
    ui.as_staff()

    # A short code, in any case, is still a tag lookup.
    page = ui.client.get("/audit", params={"code": bound.short_code.lower()}).text
    assert 'id="audit-result"' in page and re.search(r'id="audit-price">8,80 €<', page)
    assert 'id="audit-product-h"' not in page

    # An EAN-shaped vendor_id that matches no product: the tag is found.
    ean_like = synthetic_ean()
    sticker = add_tag(ui.db, tag_type)
    sticker.vendor_id = ean_like
    ui.db.commit()
    page = ui.client.get("/audit", params={"code": ean_like}).text
    assert 'id="audit-result"' in page and sticker.short_code in page
    assert t("audit.ean_unknown", ean=ean_like) not in page

    # An EAN that matches a product and a tag's vendor_id: the product wins.
    sticker.vendor_id = product.ean
    ui.db.commit()
    page = ui.client.get("/audit", params={"code": product.ean}).text
    assert codes_listed(page) == [bound.short_code]
    assert sticker.short_code not in codes_listed(page)

    # Thirteen digits with a wrong check digit are not an EAN: a tag lookup.
    bad = product.ean[:-1] + str((int(product.ean[-1]) + 1) % 10)
    page = ui.client.get("/audit", params={"code": bad}).text
    assert error_message("bind.tag_unknown", code=bad) in page


def test_as_ean_uses_the_import_rules() -> None:
    assert ui_audit.as_ean("4006381333931") == "4006381333931"
    assert ui_audit.as_ean("4006 3813 3393 1") == "4006381333931"
    assert ui_audit.as_ean("96385074") == "96385074"
    for not_ean in ("4006381333932", "012345678905", "ABCD", "1234", "", "00000000"):
        assert ui_audit.as_ean(not_ean) is None


def test_staff_and_admin_can_use_it_logged_out_is_redirected(ui: UI) -> None:
    product = add_product(ui.db, f"Γάζες {suffix()}", 300)
    r = ui.client.get("/audit", params={"code": product.ean}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    ui.as_staff()
    assert product.name in ui.client.get("/audit", params={"code": product.ean}).text
    ui.client.cookies.clear()
    ui.as_admin()
    assert product.name in ui.client.get("/audit", params={"code": product.ean}).text


@contextmanager
def count_queries(ui: UI) -> Iterator[list[str]]:
    statements: list[str] = []
    conn = ui.db.connection()

    def on_execute(*args: Any) -> None:
        statements.append(args[2])

    event.listen(conn, "before_cursor_execute", on_execute)
    try:
        yield statements
    finally:
        event.remove(conn, "before_cursor_execute", on_execute)


def test_query_count_does_not_grow_with_tags(ui: UI) -> None:
    tag_type = add_tag_type_row(ui.db)
    one = add_product(ui.db, f"Ένα {suffix()}", 100)
    many = add_product(ui.db, f"Πολλά {suffix()}", 100)
    newly_bound_tag(ui, tag_type, one)
    for _ in range(2):
        newly_bound_tag(ui, tag_type, many)
        up_to_date_tag(ui, add_tag_type_row(ui.db), many)
    ui.as_staff()

    def queries_for(product: Product) -> int:
        ui.client.get("/audit", params={"code": product.ean})  # warm up
        with count_queries(ui) as statements:
            page = ui.client.get("/audit", params={"code": product.ean}).text
        assert product.name in page
        return len(statements)

    n_one, n_many = queries_for(one), queries_for(many)
    assert n_one == n_many, (n_one, n_many)

"""Phone binding flow (PLAN.md section 9) and the audit page, through the real app on
the rolled-back test session. The browser test (``tests/e2e/test_ui_phone.py``) drives
the same pages on a phone-sized screen with the fake-driver gateway."""

from __future__ import annotations

import re
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from core.app.formatting import display_tz
from core.app.i18n import error_message, t
from core.app.import_messages import row_reason
from core.app.routers import ui_bind
from core.db.models import Binding, EventLog, Price, PriceHistory, Product, Tag, UpdateJob
from core.domain.catalogue import CellError, ean_check_digit, normalise_ean
from core.render.label import TemplateKey
from core.services import tags as tag_service
from core.services.promotions import PromoInput, edit_promo, promo_end
from core.services.tags import bind_tag
from shared.tag_types import Palette
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

_RADIO = re.compile(r'name="template" value="([a-z_]+)"( checked)?')


def _options(html: str) -> tuple[list[str], str | None]:
    found = _RADIO.findall(html)
    checked = [value for value, mark in found if mark]
    return [value for value, _ in found], checked[0] if checked else None


def _binding(ui: UI, tag: Tag) -> Binding | None:
    ui.db.expire_all()
    return ui.db.scalars(select(Binding).where(Binding.tag_id == tag.id)).one_or_none()


def _confirm(
    ui: UI, token: str, product: Product, tag: Tag, template: str, rebind: str = ""
) -> httpx.Response:
    data = {"csrf_token": token, "product_id": str(product.id), "tag_id": str(tag.id),
            "template": template, "rebind_from": rebind}  # fmt: skip
    response: httpx.Response = ui.client.post("/bind", data=data, follow_redirects=False)
    return response


# --- Step 1: the EAN -----------------------------------------------------------------


def test_staff_binds_a_tag_step_by_step(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα χεριών {suffix()}", 1290)
    tag = add_tag(ui.db, add_tag_type_row(ui.db))
    token = ui.as_staff()

    page = ui.client.get("/bind").text
    assert 'id="ean"' in page and 'inputmode="numeric"' in page and "autofocus" in page
    assert t("scan.start") in page  # the camera button (shown by app.js over HTTPS)

    page = ui.client.get("/bind", params={"ean": f" {product.ean} "}).text
    assert product.name in page and "12,90 €" in page
    assert 'id="code"' in page and t("bind.code.hint") in page

    # Any case is accepted for the short code.
    page = ui.client.get("/bind", params={"ean": product.ean, "code": tag.short_code.lower()})
    assert page.status_code == 200
    options, checked = _options(page.text)
    assert options == ["standard", "promo"]  # no pack size: no unit price template
    assert checked == "standard"
    assert 'id="label-preview"' in page.text
    src = re.search(r'id="label-preview"[^>]*src="([^"]+)"', page.text, re.DOTALL)
    assert src is not None
    png = ui.client.get(src.group(1).replace("&amp;", "&"))
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"

    r = _confirm(ui, token, product, tag, "standard")
    assert r.status_code == 303 and r.headers["location"] == f"/bind/tags/{tag.id}"
    binding = _binding(ui, tag)
    assert binding is not None and binding.product_id == product.id
    assert binding.bound_by == ui.staff.id
    event = ui.db.scalars(
        select(EventLog).where(EventLog.kind == "tag.bound").order_by(EventLog.id.desc())
    ).first()
    assert event is not None and event.actor == ui.staff.email
    assert event.payload["tag_id"] == tag.id

    page = ui.client.get(f"/bind/tags/{tag.id}").text
    assert t("bind.bound", code=tag.short_code, name=product.name) in page
    assert 'data-state="pending"' in page and 'hx-trigger="every 3s"' in page

    # The gateway pushes and the tag shows its label: the box says done and stops polling.
    ui.db.refresh(tag)
    tag.displayed_hash = tag.desired_hash
    ui.db.commit()
    fragment = ui.client.get(f"/bind/tags/{tag.id}/progress").text
    assert 'data-state="up_to_date"' in fragment and t("bind.progress.up_to_date") in fragment
    assert "hx-trigger" not in fragment
    assert "<html" not in fragment  # a fragment, no layout


def test_invalid_ean_says_why_in_greek(ui: UI) -> None:
    ui.as_staff()
    body = "290000000001"
    bad = body + str((ean_check_digit(body) + 1) % 10)
    with pytest.raises(CellError) as reason:
        normalise_ean(bad)
    page = ui.client.get("/bind", params={"ean": bad}).text
    assert row_reason(str(reason.value)) in page
    assert 'id="ean"' in page
    page = ui.client.get("/bind", params={"ean": "5.20123E+12"}).text
    assert row_reason("EAN is in scientific notation, its digits are lost: '5.20123E+12'") in page


def test_usb_scanner_fallback_is_the_same_field(ui: UI) -> None:
    """A USB scanner types the code and presses Enter: the plain GET form, nothing else."""
    product = add_product(ui.db, f"Σαμπουάν {suffix()}", 750)
    ui.as_staff()
    page = ui.client.get("/bind").text
    assert re.search(r'<form method="get" action="/bind"[^>]*id="ean-form"', page)
    assert product.name in ui.client.get(f"/bind?ean={product.ean}").text


# --- Step 2: the tag -----------------------------------------------------------------


def test_unknown_tag_code_is_refused(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 990)
    ui.as_staff()
    page = ui.client.get("/bind", params={"ean": product.ean, "code": "zz99"}).text
    assert error_message("bind.tag_unknown", code="zz99") in page
    assert 'id="code"' in page


def test_bound_tag_says_to_what_and_asks_before_rebinding(ui: UI) -> None:
    tag_type = add_tag_type_row(ui.db)
    old = add_product(ui.db, f"Παλιό {suffix()}", 500)
    new = add_product(ui.db, f"Νέο {suffix()}", 700)
    tag = add_tag(ui.db, tag_type, old)
    token = ui.as_staff()

    page = ui.client.get("/bind", params={"ean": new.ean, "code": tag.short_code}).text
    assert t("bind.rebind.other", code=tag.short_code, name=old.name) in page
    assert 'id="confirm"' not in page
    assert f"rebind={old.id}" in page

    # Without the confirmation, the form says the tag is unbound: refused, nothing changes.
    r = _confirm(ui, token, new, tag, "standard", rebind="")
    assert r.status_code == 303 and "/bind?" in r.headers["location"]
    binding = _binding(ui, tag)
    assert binding is not None and binding.product_id == old.id
    assert t("bind.changed_meanwhile") in ui.client.get(r.headers["location"]).text

    page = ui.client.get(
        "/bind", params={"ean": new.ean, "code": tag.short_code, "rebind": str(old.id)}
    ).text
    assert 'id="confirm"' in page and f'name="rebind_from" value="{old.id}"' in page
    r = _confirm(ui, token, new, tag, "standard", rebind=str(old.id))
    assert r.headers["location"] == f"/bind/tags/{tag.id}"
    binding = _binding(ui, tag)
    assert binding is not None and binding.product_id == new.id


def test_rebind_to_the_same_product_is_asked_too(ui: UI) -> None:
    product = add_product(ui.db, f"Ίδιο {suffix()}", 500)
    tag = add_tag(ui.db, add_tag_type_row(ui.db), product)
    ui.as_staff()
    page = ui.client.get("/bind", params={"ean": product.ean, "code": tag.short_code}).text
    assert t("bind.rebind.same", code=tag.short_code, name=product.name) in page


# --- Step 3: templates ---------------------------------------------------------------


def test_only_valid_templates_are_offered(ui: UI) -> None:
    bwr = add_tag_type_row(ui.db)
    bw = add_tag_type_row(ui.db, Palette.BW)
    rx = add_product(ui.db, f"Φάρμακο {suffix()}", 800, is_rx=True)
    unit = add_product(
        ui.db, f"Γάλα {suffix()}", 450, pack_qty_milli=200_000, pack_unit="ml",
        unit_price_required=True,
    )  # fmt: skip
    packed = add_product(ui.db, f"Κρέμα {suffix()}", 900, pack_qty_milli=50_000, pack_unit="ml")
    token = ui.as_staff()

    def offered(product: Product, tag_type_id: int) -> tuple[list[str], str | None]:
        tag = add_tag(ui.db, bw if tag_type_id == bw.id else bwr)
        return _options(ui.client.get(f"/bind?ean={product.ean}&code={tag.short_code}").text)

    assert offered(rx, bwr.id) == (["standard"], "standard")  # never a promo for Rx
    assert offered(unit, bwr.id) == (["unit_price", "promo"], "unit_price")
    assert offered(packed, bwr.id) == (["standard", "unit_price", "promo"], "standard")
    assert offered(packed, bw.id) == (["standard", "unit_price"], "standard")  # no red

    tag = add_tag(ui.db, bwr)
    r = _confirm(ui, token, rx, tag, "promo")
    assert r.status_code == 303
    assert _binding(ui, tag) is None
    assert error_message("bind.template_invalid") in ui.client.get(r.headers["location"]).text


def test_running_promo_makes_promo_the_default(ui: UI) -> None:
    product = add_product(ui.db, f"Αντηλιακό {suffix()}", 1500)
    end = promo_end((T0 + timedelta(days=5)).date(), display_tz())
    edit_promo(ui.db, product.id, PromoInput(1190, T0 - timedelta(hours=1), end),
               actor="test", now=T0 - timedelta(hours=1))  # fmt: skip
    ui.db.commit()
    tag = add_tag(ui.db, add_tag_type_row(ui.db))
    ui.as_staff()
    page = ui.client.get(f"/bind?ean={product.ean}&code={tag.short_code}").text
    assert _options(page) == (["standard", "promo"], "promo")
    # The preview for another choice comes from the HTMX fragment.
    fragment = ui.client.get(
        "/bind/preview", params={"product_id": product.id, "tag_id": tag.id, "template": "standard"}
    ).text
    assert 'data-template="standard"' in fragment and "<html" not in fragment


def test_a_bind_that_commits_after_the_check_is_asked_again(
    ui: UI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two people confirm the same unbound tag: the second must be asked, not silently
    rebind it (decision 0017). The other bind lands between the route's check and the
    tag lock, which ``bind_tag`` re-checks."""
    tag_type = add_tag_type_row(ui.db)
    first = add_product(ui.db, f"Πρώτο {suffix()}", 500)
    second = add_product(ui.db, f"Δεύτερο {suffix()}", 700)
    tag = add_tag(ui.db, tag_type)
    token = ui.as_staff()
    real_bind = tag_service.bind_tag

    def other_person_binds_first(*args: object, **kwargs: object) -> object:
        monkeypatch.setattr(ui_bind, "bind_tag", real_bind)
        real_bind(ui.db, tag.id, first.id, actor="other", now=T0)
        ui.db.commit()
        return real_bind(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ui_bind, "bind_tag", other_person_binds_first)
    r = _confirm(ui, token, second, tag, "standard", rebind="")
    assert r.status_code == 303 and "/bind?" in r.headers["location"]
    binding = _binding(ui, tag)
    assert binding is not None and binding.product_id == first.id
    assert t("bind.changed_meanwhile") in ui.client.get(r.headers["location"]).text


# --- Step 4: progress ----------------------------------------------------------------


def test_progress_shows_the_greek_push_error_and_stops_when_dead(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 990)
    tag = add_tag(ui.db, add_tag_type_row(ui.db))
    bind_tag(ui.db, tag.id, product.id, actor="test", now=T0, template=TemplateKey.STANDARD)
    ui.db.commit()
    job = ui.db.scalars(
        select(UpdateJob).where(UpdateJob.tag_id == tag.id).order_by(UpdateJob.id.desc())
    ).first()
    assert job is not None
    job.state, job.attempts, job.error_code = "failed", 1, "timeout"
    job.next_attempt_at = T0 + timedelta(minutes=1)
    ui.db.commit()
    ui.as_staff()
    fragment = ui.client.get(f"/bind/tags/{tag.id}/progress").text
    assert 'data-state="failed"' in fragment
    assert t("bind.progress.reason", reason=t("push_error.timeout")) in fragment
    assert t("bind.progress.retry_at", when="10:01") in fragment  # Athens time
    assert "hx-trigger" in fragment  # it is retried, so keep polling

    job.state, job.attempts = "dead", 6
    ui.db.commit()
    fragment = ui.client.get(f"/bind/tags/{tag.id}/progress").text
    assert 'data-state="dead"' in fragment and t("bind.progress.dead_hint") in fragment
    assert "hx-trigger" not in fragment


def test_progress_needs_login_and_redirects_htmx(ui: UI) -> None:
    tag = add_tag(ui.db, add_tag_type_row(ui.db))
    r = ui.client.get(
        f"/bind/tags/{tag.id}/progress", headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert r.status_code == 401 and r.headers["hx-redirect"].startswith("/login?next=")


# --- Unknown EAN: creating the product -------------------------------------------------


def test_staff_are_told_an_admin_must_create_the_product(ui: UI) -> None:
    ean = synthetic_ean()
    token = ui.as_staff()
    page = ui.client.get("/bind", params={"ean": ean}).text
    assert t("bind.create.unknown", ean=ean) in page
    assert t("bind.create.admin_needed") in page
    assert 'action="/bind/product"' not in page
    r = ui.client.post(
        "/bind/product",
        data={"csrf_token": token, "ean": ean, "name": "Κρέμα", "regular_price": "9,90"},
    )
    assert r.status_code == 403
    assert ui.db.scalars(select(Product).where(Product.ean == ean)).first() is None


def test_admin_creates_the_unknown_product_and_continues(ui: UI) -> None:
    ean = synthetic_ean()
    token = ui.as_admin()
    page = ui.client.get("/bind", params={"ean": ean}).text
    assert 'action="/bind/product"' in page
    r = ui.client.post(
        "/bind/product",
        data={"csrf_token": token, "ean": ean, "name": "  Γάλα   σώματος ",
              "regular_price": "1.290,50", "pack_qty": "0,5", "pack_unit": "ml",
              "unit_price_required": "true"},
        follow_redirects=False,
    )  # fmt: skip
    assert r.status_code == 303 and r.headers["location"] == f"/bind?ean={ean}"
    product = ui.db.scalars(select(Product).where(Product.ean == ean)).one()
    assert (product.name, product.pack_qty_milli, product.pack_unit) == ("Γάλα σώματος", 500, "ml")
    assert product.unit_price_required and not product.is_rx and product.active
    assert product.manually_changed_at == T0
    price = ui.db.get_one(Price, product.id)
    assert price.regular_cents == 129050  # parsed like the import: Greek thousands
    history = list(ui.db.scalars(select(PriceHistory).where(PriceHistory.product_id == product.id)))
    assert [(h.effective_cents, h.source, h.kind) for h in history] == [
        (129050, "manual", "regular")
    ]
    created = ui.db.scalars(
        select(EventLog).where(EventLog.kind == "product.created", EventLog.actor == ui.admin.email)
    ).all()
    assert any(e.payload["product_id"] == product.id for e in created)
    page = ui.client.get(r.headers["location"]).text
    assert t("bind.product_created", name="Γάλα σώματος") in page and 'id="code"' in page


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"regular_price": "0,00"}, row_reason("zero price, probably no price in the ERP: '0,00'")),
        ({"regular_price": "12,999"}, row_reason("price has more than 2 decimals: '12,999'")),
        ({"name": "   "}, error_message("product.name_empty")),
        ({"unit_price_required": "true"}, error_message("product.unit_price_needs_pack")),
        ({"pack_unit": "g"}, error_message("product.pack_invalid")),
    ],
)  # fmt: skip
def test_product_create_refusals(ui: UI, fields: dict[str, str], message: str) -> None:
    ean = synthetic_ean()
    token = ui.as_admin()
    data = {"csrf_token": token, "ean": ean, "name": "Κρέμα", "regular_price": "9,90", **fields}
    r = ui.client.post("/bind/product", data=data)
    assert r.status_code == 422
    assert message.replace("'", "&#39;") in r.text
    assert ui.db.scalars(select(Product).where(Product.ean == ean)).first() is None


def test_product_create_with_an_existing_ean_goes_on_with_it(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 990)
    token = ui.as_admin()
    r = ui.client.post(
        "/bind/product",
        data={"csrf_token": token, "ean": product.ean, "name": "Άλλο", "regular_price": "1,00"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == f"/bind?ean={product.ean}"
    ui.db.refresh(product)
    assert product.name.startswith("Κρέμα")


# --- Audit ---------------------------------------------------------------------------


def test_audit_shows_what_the_tag_should_show(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα ποδιών {suffix()}", 1290)
    tag = add_tag(ui.db, add_tag_type_row(ui.db), product)
    tag.last_seen_at, tag.rssi, tag.battery_pct = T0 - timedelta(minutes=5), -71, 64
    ui.db.commit()
    ui.as_staff()
    page = ui.client.get("/audit", params={"code": tag.short_code.lower()}).text
    assert product.name in page
    assert re.search(r'id="audit-price">12,90 €<', page)
    assert t("tags.status.pending") in page
    assert "01/12/2026 09:55" in page  # Athens time
    assert t("tags.battery_pct", value=64) in page and t("tags.rssi", value=-71) in page
    assert f"/tags/{tag.id}/label/desired.png?v={tag.desired_hash}" in page
    assert 'id="audit-stale"' not in page
    # The field is empty and focused for the next tag.
    assert re.search(r'id="code" name="code" value=""[^>]*autofocus', page)


def test_audit_unknown_and_unbound_and_behind(ui: UI) -> None:
    tag_type = add_tag_type_row(ui.db)
    ui.as_staff()
    page = ui.client.get("/audit", params={"code": "QQQQ"}).text
    assert error_message("bind.tag_unknown", code="QQQQ") in page

    unbound = add_tag(ui.db, tag_type)
    page = ui.client.get("/audit", params={"code": unbound.vendor_id.upper()}).text
    assert t("tags.unbound") in page and unbound.short_code in page

    product = add_product(ui.db, f"Κρέμα {suffix()}", 500)
    tag = add_tag(ui.db, tag_type, product)
    # A price changed but the label was not rebuilt yet (as before the next refresh).
    price = ui.db.get_one(Price, product.id)
    price.regular_cents = 600
    ui.db.commit()
    page = ui.client.get("/audit", params={"code": tag.short_code}).text
    assert 'id="audit-stale"' in page and re.search(r'id="audit-price">6,00 €<', page)


def test_audit_is_read_only(ui: UI) -> None:
    assert not any(
        getattr(route, "path", "").startswith("/audit") and "POST" in getattr(route, "methods", ())
        for route in ui.client.app.routes  # type: ignore[attr-defined]
    )

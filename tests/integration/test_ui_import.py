"""Import page (section 9, decision 0011): upload, mapping, preview, apply, past runs."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from core.app.i18n import error_message, t
from core.db.models import ImportProfile, ImportRun, Price, Product
from tests.integration.test_ui_helpers import (
    UI,
    add_product,
    suffix,
    synthetic_ean,
    ui_fixture,  # noqa: F401  (the "ui" fixture)
)

pytestmark = pytest.mark.db

HEADER = "Barcode;Περιγραφή;Λιανική;Εταιρεία"
MAPPING = {"map_ean": "Barcode", "map_name": "Περιγραφή", "map_price": "Λιανική",
           "map_brand": "Εταιρεία"}  # fmt: skip


def ean() -> str:
    return synthetic_ean()


def csv(*rows: str) -> bytes:
    return ("\n".join([HEADER, *rows]) + "\n").encode("utf-8")


def _preview(ui: UI, token: str, data: bytes, **form: str) -> str:
    fields = {"csrf_token": token, "profile": "", **MAPPING, **form}
    r = ui.client.post(
        "/import/preview", data=fields, files={"file": ("τιμές.csv", data, "text/csv")}
    )
    return str(r.text)


def _apply(ui: UI, token: str, **form: str) -> str:
    return str(ui.client.post("/import/apply", data={"csrf_token": token, **form}).text)


def _product(ui: UI, code: str) -> Product | None:
    return ui.db.scalars(select(Product).where(Product.ean == code)).one_or_none()


def test_upload_preview_apply(ui: UI) -> None:
    new_ean = ean()
    existing = add_product(ui.db, f"Κρέμα {suffix()}", 1000)
    absent = add_product(ui.db, f"Λείπει {suffix()}", 700)
    token = ui.as_admin()
    data = csv(
        f"{new_ean};Σαπούνι {suffix()};3,50;Korres",
        f"{existing.ean};{existing.name};11,00;",
        "123;Χωρίς EAN;2,00;",
        f"{ean()};Μηδέν;0,00;",
    )
    page = _preview(ui, token, data)
    assert t("import.preview.title") in page
    assert t("import.preview.new.title") in page
    assert t("import.preview.changed.title") in page
    assert "10,00 €" in page and "11,00 €" in page and "+1,00 €" in page
    # Rejected rows show the Greek reason.
    assert t("import.reason.ean_internal", value="123") in page
    assert t("import.reason.price_zero", value="0,00") in page
    assert t("import.preview.missing.title") in page and absent.ean in page
    assert _product(ui, new_ean) is None  # preview writes nothing
    stored = list(ui.store.directory.glob("*.csv"))
    assert len(stored) == 1
    page = _apply(ui, token)
    assert t("import.applied", new=1, changed=1, unchanged=0, rejected=2) in page
    assert _product(ui, new_ean) is not None
    price = ui.db.scalars(select(Price).where(Price.product_id == existing.id)).one()
    ui.db.refresh(price)
    assert price.regular_cents == 1100
    assert list(ui.store.directory.glob("*.csv")) == []  # the upload is gone
    assert "τιμές.csv" in page  # listed under past imports
    run = ui.db.scalars(select(ImportRun).order_by(ImportRun.id.desc())).first()
    assert run is not None and run.created_by == ui.admin.id


def test_suspicious_import_needs_second_confirmation(ui: UI) -> None:
    products = [add_product(ui.db, f"Κρέμα {suffix()}", 1000) for _ in range(3)]
    token = ui.as_admin()
    # Every price changes tenfold: a wrong column, most likely.
    data = csv(*(f"{p.ean};{p.name};100,00;" for p in products))
    page = _preview(ui, token, data)
    assert 'id="jump-banner"' in page
    assert t("import.preview.suspicious.title") in page
    assert 'name="confirm_price_jumps"' in page
    page = _apply(ui, token)  # without the confirmation
    assert error_message("import.confirmation_required") in page
    assert 'id="jump-banner"' in page
    for p in products:
        price = ui.db.scalars(select(Price).where(Price.product_id == p.id)).one()
        ui.db.refresh(price)
        assert price.regular_cents == 1000
    page = _apply(ui, token, confirm_price_jumps="true")
    assert t("import.applied", new=0, changed=3, unchanged=0, rejected=0) in page


def test_ordinary_import_has_no_banner(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1000)
    token = ui.as_admin()
    page = _preview(ui, token, csv(f"{product.ean};{product.name};10,50;"))
    assert 'id="jump-banner"' not in page
    assert 'name="confirm_price_jumps"' not in page


def test_manually_changed_products_are_flagged(ui: UI) -> None:
    from core.services.catalogue import edit_price

    product = add_product(ui.db, f"Κρέμα {suffix()}", 1000)
    edit_price(ui.db, product.id, 1200, actor="test")
    ui.db.commit()
    token = ui.as_admin()
    page = _preview(ui, token, csv(f"{product.ean};{product.name};10,00;"))
    assert t("import.preview.manual.title") in page


def test_save_profile_and_reuse_it(ui: UI) -> None:
    name = f"ERP {suffix()}"
    token = ui.as_admin()
    page = _preview(ui, token, csv(f"{ean()};Σαπούνι;3,50;"), save_as=name)
    assert t("import.profile.saved", name=name) in page
    assert ui.db.scalars(select(ImportProfile).where(ImportProfile.name == name)).one()
    page = ui.client.post(
        "/import/preview",
        data={"csrf_token": token, "profile": name},
        files={"file": ("b.csv", csv(f"{ean()};Σαπούνι;3,50;"), "text/csv")},
    ).text
    assert t("import.preview.title") in page


def test_unreadable_file_is_explained_in_greek(ui: UI) -> None:
    token = ui.as_admin()
    page = _preview(ui, token, b"Barcode;Name\n1;2\n")
    message = t(
        "import.file.missing_columns",
        columns="'Περιγραφή', 'Λιανική', 'Εταιρεία'",
        header="'Barcode', 'Name'",
    )
    assert message.replace("'", "&#39;") in page
    assert not ui.store.directory.exists() or not list(ui.store.directory.glob("*.csv"))


def test_mapping_without_price_column_is_refused(ui: UI) -> None:
    token = ui.as_admin()
    page = _preview(ui, token, csv(f"{ean()};Σαπούνι;3,50;"), map_price="")
    assert error_message("import.profile_invalid") in page


def test_missing_file(ui: UI) -> None:
    token = ui.as_admin()
    r = ui.client.post("/import/preview", data={"csrf_token": token, **MAPPING})
    assert r.status_code == 422
    assert error_message("import.no_file") in r.text


def test_apply_without_preview_has_expired(ui: UI) -> None:
    token = ui.as_admin()
    r = ui.client.post("/import/apply", data={"csrf_token": token})
    assert r.status_code == 409
    assert error_message("import.expired") in r.text


def test_cancel_deletes_upload(ui: UI) -> None:
    token = ui.as_admin()
    _preview(ui, token, csv(f"{ean()};Σαπούνι;3,50;"))
    assert len(list(ui.store.directory.glob("*.csv"))) == 1
    page = ui.client.post("/import/cancel", data={"csrf_token": token}).text
    assert t("import.cancelled") in page
    assert list(ui.store.directory.glob("*.csv")) == []


def test_windows_1253_file(ui: UI) -> None:
    token = ui.as_admin()
    code = ean()
    data = (f"{HEADER}\r\n{code};Οδοντόκρεμα;1.290,50;\r\n").encode("cp1253")
    page = _preview(ui, token, data)
    assert "1290,50 €" in page and "cp1253" in page

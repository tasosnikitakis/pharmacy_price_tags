"""Pure catalogue rules: cell parsing, duplicates and the preview diff (workstream A)."""

# ruff: noqa: RUF001  (Greek test data is meant to look like Latin letters)
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.domain.catalogue import (
    CatalogueEntry,
    CellError,
    Column,
    FieldChange,
    ImportRow,
    RejectedRow,
    RowStatus,
    build_diff,
    normalise_ean,
    normalise_text,
    parse_pack,
    parse_pack_unit,
    parse_price_cents,
    parse_row,
    reject_duplicates,
)
from core.domain.types import PackUnit

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


# --- Prices ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("12,90", 1290),
        ("12,9", 1290),
        ("12", 1200),
        ("0,05", 5),
        ("0", 0),
        ("12.90", 1290),
        (" 12,90 ", 1290),
        ("12,90 €", 1290),
        ("€12,90", 1290),
        ("1.290,50", 129050),
        ("1,290.50", 129050),
        ("1.234.567,89", 123456789),
        ("12 ,90", 1290),
        ("+3,15", 315),
        # Values a float would get wrong: exact cents, no rounding.
        ("0,29", 29),
        ("1,15", 115),
        ("4,35", 435),
    ],
)
def test_parse_price_cents(text: str, cents: int) -> None:
    assert parse_price_cents(text) == cents


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("αβγ", "not a number"),
        ("12,90,1", "not a number"),
        ("-1,50", "negative"),
        ("-0", "negative"),
        ("12,905", "more than 2 decimals"),
        ("1.290", "ambiguous"),
        ("12,", "not a number"),
        (",50", "not a number"),
        ("1.2.3,00", "thousands"),
        ("12.34,5,6", "not a number"),
        ("1e3", "not a number"),
        ("99999999999", "too large"),
    ],
)
def test_parse_price_rejects(text: str, reason: str) -> None:
    with pytest.raises(CellError, match=reason):
        parse_price_cents(text)


def test_parse_price_never_uses_float() -> None:
    # 0.1 + 0.2 style errors cannot appear: every two-decimal value maps exactly.
    for cents in range(0, 100_000, 7):
        text = f"{cents // 100},{cents % 100:02d}"
        assert parse_price_cents(text) == cents


# --- Pack sizes -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("qty", "unit", "expected"),
    [
        ("4,8", "g", (4800, PackUnit.GRAM)),
        ("4.8", "gr", (4800, PackUnit.GRAM)),
        ("200", "ml", (200_000, PackUnit.MILLILITRE)),
        ("200 ml", "", (200_000, PackUnit.MILLILITRE)),
        ("200ML", "", (200_000, PackUnit.MILLILITRE)),
        ("7,5", "ΜΛ", (7500, PackUnit.MILLILITRE)),
        ("30", "τεμ", (30_000, PackUnit.PIECE)),
        ("30", "ΤΕΜ.", (30_000, PackUnit.PIECE)),
        ("30", "Τεμάχια", (30_000, PackUnit.PIECE)),
        ("0,4", "kg", (400_000, PackUnit.GRAM)),
        ("1,5", "lt", (1_500_000, PackUnit.MILLILITRE)),
        ("0,125", "kg", (125_000, PackUnit.GRAM)),
        ("4,8", "γρ.", (4800, PackUnit.GRAM)),
        ("0,001", "g", (1, PackUnit.GRAM)),
        ("200", "ml ", (200_000, PackUnit.MILLILITRE)),
        ("200 ml", "ml", (200_000, PackUnit.MILLILITRE)),
    ],
)
def test_parse_pack(qty: str, unit: str, expected: tuple[int, PackUnit]) -> None:
    assert parse_pack(qty, unit) == expected


def test_parse_pack_empty_quantity_is_no_pack() -> None:
    assert parse_pack("", "ml") is None
    assert parse_pack("  ", "") is None


@pytest.mark.parametrize(
    ("qty", "unit", "reason"),
    [
        ("0", "g", "positive"),
        ("abc", "g", "not a number"),
        ("200", "", "no unit"),
        ("200", "οκάδες", "unknown pack unit"),
        ("0,0005", "g", "more precise"),
        ("1.000", "ml", "ambiguous"),
        ("200 ml", "g", "disagrees"),
        ("3000000", "kg", "too large"),
    ],
)
def test_parse_pack_rejects(qty: str, unit: str, reason: str) -> None:
    with pytest.raises(CellError, match=reason):
        parse_pack(qty, unit)


def test_parse_pack_unit_factor() -> None:
    assert parse_pack_unit("KG") == (PackUnit.GRAM, 1000)
    assert parse_pack_unit("Λίτρο") == (PackUnit.MILLILITRE, 1000)


# --- EAN and text ---------------------------------------------------------------------


def test_normalise_ean_keeps_leading_zeros_and_strips_spaces() -> None:
    assert normalise_ean(" 0012345678905 ") == "0012345678905"
    assert normalise_ean("520 1234 567890") == "5201234567890"
    assert normalise_ean("12345670") == "12345670"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "empty"),
        ("5.20123E+12", "not all digits"),
        ("52012345678AB", "not all digits"),
        ("１２３４５６７８", "not all digits"),  # full-width digits
        ("1234567", "8 to 14"),
        ("123456789012345", "8 to 14"),
    ],
)
def test_normalise_ean_rejects(text: str, reason: str) -> None:
    with pytest.raises(CellError, match=reason):
        normalise_ean(text)


def test_normalise_text_collapses_whitespace_and_composes() -> None:
    decomposed = "ÁΛΟΗ"  # Latin A + combining acute, as some tools write it
    assert normalise_text("  ΆΛΟΗ   ΚΡΕΜΑ\t") == "ΆΛΟΗ ΚΡΕΜΑ"
    assert normalise_text("ΆΛΟΗ") == "ΆΛΟΗ"
    assert normalise_text(decomposed) == "ÁΛΟΗ"


# --- Rows -----------------------------------------------------------------------------


def _cells(**kw: str) -> dict[Column, str]:
    base = {Column.EAN: "2000000000015", Column.NAME: "ΆΛΟΗ ΚΡΕΜΑ", Column.PRICE: "12,90"}
    base.update({Column(k): v for k, v in kw.items()})
    return base


def test_parse_row_accepts() -> None:
    row = parse_row(2, _cells(brand=" Δοκιμή ", pack_qty="75", pack_unit="ML"))
    assert row == ImportRow(
        2, "2000000000015", "ΆΛΟΗ ΚΡΕΜΑ", 1290, "Δοκιμή", 75_000, PackUnit.MILLILITRE
    )


def test_parse_row_optional_cells_absent_or_empty() -> None:
    row = parse_row(3, _cells(brand="", pack_qty="", pack_unit="ml"))
    assert isinstance(row, ImportRow)
    assert (row.brand, row.pack_qty_milli, row.pack_unit) == (None, None, None)


@pytest.mark.parametrize(
    ("cells", "reason", "ean"),
    [
        ({"ean": ""}, "EAN is empty", None),
        ({"name": "  "}, "name is empty", "2000000000015"),
        ({"price": "αβγ"}, "price is not a number", "2000000000015"),
        ({"price": "-5,00"}, "price is negative", "2000000000015"),
        ({"price": ""}, "price is empty", "2000000000015"),
        ({"pack_qty": "0", "pack_unit": "g"}, "positive", "2000000000015"),
        ({"name": "Χ" * 301}, "longer than 300", "2000000000015"),
    ],
)
def test_parse_row_rejects_with_reason(cells: dict[str, str], reason: str, ean: str | None) -> None:
    row = parse_row(7, _cells(**cells))
    assert isinstance(row, RejectedRow)
    assert row.line == 7
    assert reason in row.reason
    assert row.ean == ean


def test_reject_duplicates_rejects_every_occurrence() -> None:
    a = ImportRow(2, "2000000000015", "Α", 100)
    b1 = ImportRow(3, "2000000000022", "Β", 200)
    b2 = ImportRow(5, "2000000000022", "Β2", 210)
    bad = RejectedRow(4, "price is empty", "2000000000015")
    no_ean = RejectedRow(6, "EAN is empty")
    accepted, rejected = reject_duplicates([a, b1, bad, b2, no_ean])
    assert accepted == []
    assert [r.line for r in rejected] == [2, 3, 4, 5, 6]
    assert rejected[1].reason == "duplicate EAN 2000000000022 (lines 3, 5)"
    assert rejected[2].reason == "duplicate EAN 2000000000015 (lines 2, 4); price is empty"
    assert rejected[4] == no_ean


def test_reject_duplicates_keeps_unique_rows_in_order() -> None:
    rows = [ImportRow(2, "2000000000015", "Α", 100), ImportRow(3, "2000000000022", "Β", 200)]
    assert reject_duplicates(rows) == (rows, [])


# --- Diff -----------------------------------------------------------------------------


def _entry(pid: int, ean: str, cents: int | None, **kw: object) -> CatalogueEntry:
    base: dict[str, object] = dict(
        product_id=pid, ean=ean, name=f"P{pid}", brand=None, pack_qty_milli=None,
        pack_unit=None, regular_cents=cents,
    )  # fmt: skip
    base.update(kw)
    return CatalogueEntry(**base)  # type: ignore[arg-type]


def test_build_diff_classifies_rows() -> None:
    catalogue = [
        _entry(1, "2000000000015", 1290, name="P1"),
        _entry(2, "2000000000022", 850, name="P2"),
        _entry(3, "2000000000039", 640, name="P3"),
        _entry(4, "2000000000046", 1500, name="P4"),  # missing from the file
        _entry(5, "2000000000053", 420, name="P5", active=False),  # missing, inactive
        _entry(6, "2000000000060", 390, name="P6"),  # only in a rejected row
    ]
    rows = [
        ImportRow(2, "2000000000015", "P1", 1290),  # unchanged
        ImportRow(3, "2000000000022", "P2", 899),  # price change
        ImportRow(4, "2000000000039", "P3 νέο", 640),  # name change only
        ImportRow(5, "2000000000077", "Νέο", 100),  # new
    ]
    rejected = [RejectedRow(6, "price is empty", "2000000000060")]
    diff = build_diff(rows, rejected, catalogue)

    assert [e.row.ean for e in diff.new] == ["2000000000077"]
    assert diff.new[0].status is RowStatus.NEW and diff.new[0].product_id is None
    assert [e.product_id for e in diff.unchanged] == [1]
    assert [e.product_id for e in diff.changed] == [2, 3]
    price_change, name_change = diff.changed
    assert (price_change.old_regular_cents, price_change.row.regular_cents) == (850, 899)
    assert price_change.price_changed and price_change.field_changes == ()
    assert not name_change.price_changed
    assert name_change.field_changes == (FieldChange("name", "P3", "P3 νέο"),)
    assert diff.rejected == rejected
    assert [(m.product_id, m.ean) for m in diff.missing] == [(4, "2000000000046")]
    assert diff.counts() == {
        "new": 1, "changed": 2, "unchanged": 1, "rejected": 1, "missing": 1,
        "price_changed": 1, "manually_changed": 0,
    }  # fmt: skip


def test_build_diff_reimport_is_unchanged() -> None:
    rows = [
        ImportRow(2, "2000000000015", "Α", 1290, "Δοκιμή", 75_000, PackUnit.MILLILITRE),
        ImportRow(3, "2000000000022", "Β", 850),
    ]
    catalogue = [
        _entry(1, "2000000000015", 1290, name="Α", brand="Δοκιμή", pack_qty_milli=75_000,
               pack_unit=PackUnit.MILLILITRE),
        _entry(2, "2000000000022", 850, name="Β", brand="Παλιά", pack_qty_milli=4800,
               pack_unit=PackUnit.GRAM),
    ]  # fmt: skip
    diff = build_diff(rows, [], catalogue)
    # Empty brand and pack cells keep the stored values rather than clearing them.
    assert diff.new == diff.changed == []
    assert diff.missing == []
    assert len(diff.unchanged) == 2


def test_build_diff_pack_and_brand_changes() -> None:
    row = ImportRow(2, "2000000000015", "Α", 1290, "Νέα", 4800, PackUnit.GRAM)
    entry = _entry(1, "2000000000015", 1290, name="Α", brand="Παλιά", pack_qty_milli=4800,
                   pack_unit=PackUnit.MILLILITRE)  # fmt: skip
    (changed,) = build_diff([row], [], [entry]).changed
    assert changed.field_changes == (
        FieldChange("brand", "Παλιά", "Νέα"),
        FieldChange("pack_unit", PackUnit.MILLILITRE, PackUnit.GRAM),
    )


def test_build_diff_flags_manually_changed_products() -> None:
    catalogue = [
        _entry(1, "2000000000015", 1350, name="Α", manually_changed_at=NOW),
        _entry(2, "2000000000022", 850, name="Β", manually_changed_at=NOW),
        _entry(3, "2000000000039", 640, name="Γ"),
    ]
    rows = [
        ImportRow(2, "2000000000015", "Α", 1290),  # ERP differs from the manual price
        ImportRow(3, "2000000000022", "Β", 850),  # manual price equals the ERP
        ImportRow(4, "2000000000039", "Γ", 700),
    ]
    diff = build_diff(rows, [], catalogue)
    assert [(e.product_id, e.manually_changed) for e in diff.changed] == [(1, True), (3, False)]
    assert [(e.product_id, e.manually_changed) for e in diff.unchanged] == [(2, True)]
    assert [e.product_id for e in diff.manually_changed] == [1, 2]
    assert diff.counts()["manually_changed"] == 2


def test_build_diff_product_without_price_row_is_a_price_change() -> None:
    (entry,) = build_diff([ImportRow(2, "2000000000015", "P1", 100)], [],
                          [_entry(1, "2000000000015", None)]).changed  # fmt: skip
    assert entry.price_changed and entry.old_regular_cents is None


def test_build_diff_empty_catalogue_all_new() -> None:
    rows = [ImportRow(2, "2000000000015", "Α", 1), ImportRow(3, "2000000000022", "Β", 2)]
    diff = build_diff(rows, [], [])
    assert [e.status for e in diff.new] == [RowStatus.NEW, RowStatus.NEW]
    assert all(e.price_changed for e in diff.new)

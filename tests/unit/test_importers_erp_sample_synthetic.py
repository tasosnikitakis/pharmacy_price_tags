"""Pins ``tests/fixtures/erp_sample_synthetic.md``, "Expected import result".

The fixture is Tasos's synthetic export shaped like the pharmacy's ERP (Windows-1253,
``;``, CRLF, no BOM, Greek header). It is read as bytes and never modified. No
database: this runs on Windows CI too.
"""

# ruff: noqa: RUF001  (Greek test data is meant to look like Latin letters)
from __future__ import annotations

from pathlib import Path

from core.domain.types import PackUnit
from core.importers import ImportSettings, parse_import, read_table

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLE = FIXTURES / "erp_sample_synthetic.csv"

#: The column table of erp_sample_synthetic.md; encoding and delimiter are detected.
#: The md's ``product.pack_qty`` is the contract's ``pack_qty_milli``.
SAMPLE_MAPPING = {
    "ean": "Barcode",
    "name": "Περιγραφή",
    "brand": "Εταιρεία",
    "price": "Λιανική με ΦΠΑ",
    "pack_qty": "Ποσότητα",
    "pack_unit": "Μον.Μέτρ.",
}


def sample_settings() -> ImportSettings:
    return ImportSettings.create(SAMPLE_MAPPING)  # encoding=None, delimiter=None


def _raw_rows() -> dict[str, list[str]]:
    """The file's own cells by EAN text, to compare names untouched by parsing."""
    text = SAMPLE.read_bytes().decode("cp1253")
    return {line.split(";")[0]: line.split(";") for line in text.split("\r\n")[1:] if line}


def test_erp_sample_synthetic_file_format() -> None:
    data = SAMPLE.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert data.count(b"\r\n") == data.count(b"\n") == 38  # header + 37 rows, CRLF
    table = read_table(data)  # detection only
    assert (table.encoding, table.delimiter) == ("cp1253", ";")
    assert table.header == list(SAMPLE_MAPPING.values())
    assert len(table.rows) == 37


def test_erp_sample_synthetic_matches_expected_result() -> None:
    parsed = parse_import(SAMPLE.read_bytes(), sample_settings())

    # Detection worked: the profile names neither encoding nor delimiter.
    assert (parsed.encoding, parsed.delimiter) == ("cp1253", ";")

    # 37 data rows: 29 accepted, 8 rejected.
    assert len(parsed.rows) + len(parsed.rejected) == 37
    assert len(parsed.rows) == 29
    assert len(parsed.rejected) == 8

    # Accepted: the first 28 rows (file lines 2-29) plus the in-store code.
    by_ean = {row.ean: row for row in parsed.rows}
    assert [row.line for row in parsed.rows] == list(range(2, 31))
    chair = by_ean["5206670106515"]
    assert chair.regular_cents == 129_000  # 1.290,00
    assert (chair.pack_qty_milli, chair.pack_unit) == (1000, PackUnit.PIECE)  # 1 τεμ
    nappies = by_ean["8006316475251"]
    assert (nappies.regular_cents, nappies.pack_qty_milli, nappies.pack_unit) == (
        1699, 46_000, PackUnit.PIECE
    )  # fmt: skip
    assert (by_ean["3337104332186"].pack_qty_milli, by_ean["3337104332186"].pack_unit) == (
        50_000, PackUnit.MILLILITRE
    )  # fmt: skip
    assert (by_ean["0083473829972"].pack_qty_milli, by_ean["0083473829972"].pack_unit) == (
        10_000, PackUnit.GRAM
    )  # fmt: skip
    assert {row.pack_unit for row in parsed.rows if row.ean != "0083473829972"} >= {
        PackUnit.PIECE, PackUnit.MILLILITRE
    }  # fmt: skip
    # EANs stay text: leading zeros survive.
    assert {"0033537672426", "0033388496967", "0083473829972"} <= set(by_ean)
    # In-store prefix 20-29 with a valid check digit: accepted (decision pending).
    in_store = by_ean["2000000123455"]
    assert (in_store.regular_cents, in_store.brand) == (450, "Χύμα")

    # Names, accented, all-caps or very long, survive parsing unchanged.
    raw = _raw_rows()
    for row in parsed.rows:
        assert row.name == raw[row.ean][1]
        assert row.brand == raw[row.ean][2]
    assert by_ean["3504718227829"].name == "ΛΑΔΙ ΣΩΜΑΤΟΣ ΓΙΑ ΡΑΓΑΔΕΣ ΕΓΚΥΜΟΣΥΝΗΣ"
    assert by_ean["3574534192830"].name == "Μωρομάντηλα Χωρίς Άρωμα"
    long_name = by_ean["5204896383468"].name
    assert long_name == (
        "Γάλα Καθαρισμού & Τόνωσης για Κανονικές έως Μικτές Επιδερμίδες με Εκχύλισμα Χαμομηλιού"
    )
    assert len(long_name) == 86

    # Rejected: each row with a reason naming the cause.
    reasons = {r.line: r.reason for r in parsed.rejected}
    assert sorted(reasons) == [31, 32, 33, 34, 35, 36, 37, 38]
    assert reasons[31].startswith("not an EAN; internal codes are not supported yet")
    assert "ΕΣ-00417" in reasons[31]
    assert reasons[32].startswith("EAN is in scientific notation")
    assert "5.20123E+12" in reasons[32]
    assert reasons[33].startswith("12 digits (UPC-A?): not padded; use the full EAN-13")
    assert reasons[34].startswith("check digit is 3, expected 2")
    assert reasons[35].startswith("price is ambiguous")  # Hylo, 1.290
    assert "1.290" in reasons[35]
    assert reasons[36].startswith("price has more than 2 decimals")  # Floradix, 12,345
    assert reasons[37] == reasons[38] == "duplicate EAN 3337723430980 (lines 37, 38)"
    rejected_eans = {r.line: r.ean for r in parsed.rejected}
    assert rejected_eans[35] == "5201326773608"  # Hylo
    assert rejected_eans[36] == "5202606474680"  # Floradix

"""CSV reading, detection and profiles (workstream A). Fixtures are read as bytes."""

# ruff: noqa: RUF001  (Greek test data is meant to look like Latin letters)
from __future__ import annotations

import codecs
from pathlib import Path

import pytest

from core.domain.catalogue import Column, ImportRow
from core.domain.types import PackUnit
from core.importers import (
    ImportFormatError,
    ImportSettings,
    decode,
    detect_delimiter,
    parse_import,
    read_table,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SYNTHETIC = FIXTURES / "erp_synthetic_cp1253.csv"

#: The profile matching the synthetic Greek fixture.
SYNTHETIC_MAPPING = {
    "ean": "Barcode",
    "name": "Περιγραφή",
    "price": "Λιανική με ΦΠΑ",
    "brand": "Εταιρεία",
    "pack_qty": "Συσκευασία",
    "pack_unit": "Μονάδα",
}


def synthetic_settings(**kw: str | None) -> ImportSettings:
    return ImportSettings.create(SYNTHETIC_MAPPING, **kw)


# --- The synthetic fixture itself ------------------------------------------------------


def test_synthetic_fixture_is_windows_1253_with_crlf() -> None:
    data = SYNTHETIC.read_bytes()
    with pytest.raises(UnicodeDecodeError):
        data.decode("utf-8")
    text = data.decode("cp1253")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert "ΆΛΟΗ" in text and "Ϊ" in text and "Ώ" in text  # accented capitals
    assert ";" in text.splitlines()[0]


# --- Encoding -------------------------------------------------------------------------


def test_decode_detects_cp1253() -> None:
    text, used = decode("Τιμή;ΆΛΟΗ\r\n".encode("cp1253"))
    assert (text, used) == ("Τιμή;ΆΛΟΗ\r\n", "cp1253")


def test_decode_detects_utf8_and_strips_bom() -> None:
    assert decode("Τιμή;Ά".encode()) == ("Τιμή;Ά", "utf-8")
    assert decode(codecs.BOM_UTF8 + "Τιμή".encode()) == ("Τιμή", "utf-8")


def test_decode_ascii_is_utf8() -> None:
    assert decode(b"ean;price") == ("ean;price", "utf-8")


def test_decode_with_given_encoding() -> None:
    assert decode("Ά".encode("cp1253"), "Windows-1253") == ("Ά", "cp1253")
    with pytest.raises(ImportFormatError, match="not valid utf-8"):
        decode("Ά".encode("cp1253"), "utf-8")


def test_decode_refuses_utf16_and_bad_bytes() -> None:
    with pytest.raises(ImportFormatError, match="UTF-16"):
        decode("ean".encode("utf-16"))
    with pytest.raises(ImportFormatError, match="not valid"):
        decode(b"\xfe\xaa")  # 0xAA is undefined in cp1253 and invalid UTF-8


def test_decode_rejects_unsupported_encoding_name() -> None:
    with pytest.raises(ValueError, match="unsupported encoding"):
        decode(b"x", "latin-1")


# --- Delimiter ------------------------------------------------------------------------


def test_detect_semicolon_with_decimal_comma() -> None:
    assert detect_delimiter("ean;name;price\n1;Α;12,90\n2;Β;3,5\n") == ";"


def test_detect_comma_with_quoted_decimal_comma() -> None:
    assert detect_delimiter('ean,name,price\n1,Α,"12,90"\n2,Β,"3,5"\n') == ","


def test_detect_tab() -> None:
    assert detect_delimiter("ean\tname\tprice\n1\tΑ\t12,90\n") == "\t"


def test_detect_falls_back_to_widest_header() -> None:
    # Unquoted decimal commas make ',' inconsistent; ';' is inconsistent too here.
    assert detect_delimiter("a;b;c\n1;2\n3;4;5;6\n") == ";"


def test_detect_refuses_single_column() -> None:
    with pytest.raises(ImportFormatError, match="single column"):
        detect_delimiter("ean\n1\n2\n")
    with pytest.raises(ImportFormatError, match="empty"):
        detect_delimiter("\n \n")


# --- Table ----------------------------------------------------------------------------


def test_read_table_line_numbers_skip_blank_lines() -> None:
    data = "\r\nean;price\r\n1;2,5\r\n\r\n3;4\r\n".encode("cp1253")
    table = read_table(data)
    assert (table.encoding, table.delimiter) == ("utf-8", ";")
    assert table.header == ["ean", "price"]
    assert table.rows == [(3, ["1", "2,5"]), (5, ["3", "4"])]


def test_read_table_quoted_newline_keeps_start_line() -> None:
    table = read_table(b'ean;name\n1;"two\nlines"\n2;x\n')
    assert table.rows == [(2, ["1", "two\nlines"]), (4, ["2", "x"])]


def test_read_table_refuses_empty_file_and_bad_delimiter() -> None:
    with pytest.raises(ImportFormatError, match="empty"):
        read_table(b"", delimiter=";")
    with pytest.raises(ValueError, match="unsupported delimiter"):
        read_table(b"a|b", delimiter="|")


# --- Profiles -------------------------------------------------------------------------


def test_settings_validate_mapping() -> None:
    settings = ImportSettings.create(
        {"ean": " Barcode ", "name": "Περιγραφή", "price": "Τιμή", "brand": ""}, "WINDOWS-1253"
    )
    assert settings.mapping == {
        Column.EAN: "Barcode", Column.NAME: "Περιγραφή", Column.PRICE: "Τιμή"
    }  # fmt: skip
    assert settings.encoding == "cp1253"
    assert settings.mapping_json() == {"ean": "Barcode", "name": "Περιγραφή", "price": "Τιμή"}


@pytest.mark.parametrize(
    ("mapping", "kw", "reason"),
    [
        ({"ean": "a", "name": "b"}, {}, "lacks required columns: price"),
        ({"ean": "a", "name": "b", "price": "c", "vat": "d"}, {}, "unknown columns"),
        ({"ean": "a", "name": "A", "price": "c"}, {}, "same header"),
        ({"ean": "a", "name": "b", "price": 3}, {}, "header name"),
        ({"ean": "a", "name": "b", "price": "c"}, {"encoding": "latin-1"}, "encoding"),
        ({"ean": "a", "name": "b", "price": "c"}, {"delimiter": "|"}, "delimiter"),
    ],
)
def test_settings_refuse_bad_profiles(
    mapping: dict[str, object], kw: dict[str, str], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        ImportSettings.create(mapping, **kw)


# --- Parsing a whole file -------------------------------------------------------------


def test_parse_synthetic_greek_fixture() -> None:
    parsed = parse_import(SYNTHETIC.read_bytes(), synthetic_settings())
    assert (parsed.encoding, parsed.delimiter) == ("cp1253", ";")
    by_ean = {row.ean: row for row in parsed.rows}
    assert len(parsed.rows) == 9
    assert by_ean["2000000000015"] == ImportRow(
        2, "2000000000015", "ΆΛΟΗ ΚΡΕΜΑ ΧΕΡΙΩΝ", 1290, "Δοκιμή Α.Ε.", 75_000, PackUnit.MILLILITRE
    )
    assert by_ean["2000000000039"].regular_cents == 640
    assert by_ean["2000000000046"].pack_unit is PackUnit.PIECE
    assert by_ean["2000000000046"].pack_qty_milli == 30_000
    assert by_ean["2000000000053"].brand is None
    assert by_ean["2000000000053"].name == "ΪΟΝΤΑ ΧΑΠΙΑ ΛΑΙΜΟΥ"
    assert by_ean["2000000000060"].pack_qty_milli == 4800  # 4,8 g
    assert by_ean["2000000000077"].regular_cents == 102_450  # 1.024,50
    assert by_ean["2000000000077"].pack_qty_milli == 400_000  # 0,4 kg in g
    assert by_ean["2000000000084"].pack_qty_milli is None
    assert by_ean["2000000000121"].name == "ΟΔΟΝΤΟΚΡΕΜΑ ΜΈΝΤΑ"

    reasons = {r.line: r.reason for r in parsed.rejected}
    assert sorted(reasons) == [10, 11, 12, 13]
    assert "price is not a number" in reasons[10]
    assert "price is negative" in reasons[11]
    assert reasons[12] == reasons[13] == "duplicate EAN 2000000000114 (lines 12, 13)"


def test_parse_with_profile_encoding_and_delimiter() -> None:
    parsed = parse_import(
        SYNTHETIC.read_bytes(), synthetic_settings(encoding="cp1253", delimiter=";")
    )
    assert len(parsed.rows) == 9


def _utf8_comma_file() -> bytes:
    return (
        "﻿Barcode,Περιγραφή,Λιανική με ΦΠΑ\n"
        '2000000000015,ΆΛΟΗ ΚΡΕΜΑ ΧΕΡΙΩΝ,"12,90"\n'
        '2000000000022,"ΈΛΑΙΟ, ΣΩΜΑΤΟΣ","8,50"\n'
    ).encode()


def test_parse_utf8_comma_delimited_export() -> None:
    settings = ImportSettings.create(
        {"ean": "barcode", "name": "ΠΕΡΙΓΡΑΦΉ", "price": "Λιανική με ΦΠΑ"}
    )
    parsed = parse_import(_utf8_comma_file(), settings)
    assert (parsed.encoding, parsed.delimiter) == ("utf-8", ",")
    assert [(r.ean, r.name, r.regular_cents) for r in parsed.rows] == [
        ("2000000000015", "ΆΛΟΗ ΚΡΕΜΑ ΧΕΡΙΩΝ", 1290),
        ("2000000000022", "ΈΛΑΙΟ, ΣΩΜΑΤΟΣ", 850),
    ]
    assert parsed.rejected == []


def test_same_rows_whatever_the_encoding_and_delimiter() -> None:
    text = SYNTHETIC.read_bytes().decode("cp1253")
    as_utf8 = parse_import(text.encode("utf-8"), synthetic_settings())
    from_cp1253 = parse_import(SYNTHETIC.read_bytes(), synthetic_settings())
    assert as_utf8.rows == from_cp1253.rows
    assert as_utf8.rejected == from_cp1253.rejected


def test_parse_reports_missing_columns() -> None:
    settings = ImportSettings.create({"ean": "EAN", "name": "Περιγραφή", "price": "Τιμή"})
    with pytest.raises(ImportFormatError, match="lacks the column\\(s\\) 'EAN', 'Τιμή'"):
        parse_import(SYNTHETIC.read_bytes(), settings)


def test_parse_short_and_long_rows() -> None:
    settings = ImportSettings.create({"ean": "ean", "name": "name", "price": "price"})
    data = b"ean;name;price;note\n20000000;A;1,00\n20000001;B;2,00;x;y\n20000002;C;3,00;;\n"
    parsed = parse_import(data, settings)
    assert [r.ean for r in parsed.rows] == ["20000000", "20000002"]
    assert [(r.line, r.reason) for r in parsed.rejected] == [
        (3, "row has 5 cells but the header has 4")
    ]


def test_parse_row_missing_price_cell_is_rejected() -> None:
    settings = ImportSettings.create({"ean": "ean", "name": "name", "price": "price"})
    parsed = parse_import(b"ean;name;price\n20000000;A\n", settings)
    assert parsed.rows == []
    assert parsed.rejected[0].reason == "price is empty"


# --- Unquoted decimal commas in ','-delimited files ------------------------------------

_SIMPLE = {"ean": "ean", "name": "name", "price": "price"}
_WITH_BRAND = {**_SIMPLE, "brand": "brand"}


def test_comma_trailing_delimiter_unquoted_decimal_is_rejected() -> None:
    # The trailing ',' gives the header a 4th column, so "12,90" fills exactly 4 cells.
    data = b"ean,name,price,\n2000000000015,Aloe,12,90\n"
    parsed = parse_import(data, ImportSettings.create(_SIMPLE, delimiter=","))
    assert parsed.rows == []
    (rejected,) = parsed.rejected
    assert rejected.line == 2
    assert "looks like an unquoted decimal comma (12,90)" in rejected.reason


def test_comma_unquoted_decimal_shifting_columns_is_rejected() -> None:
    # "12,90" splits into price "12" and name "90", and the real name lands in brand.
    data = b"ean,price,name,brand\n2000000000015,12,90,Aloe\n"
    parsed = parse_import(data, ImportSettings.create(_WITH_BRAND, delimiter=","))
    assert parsed.rows == []
    assert "looks like an unquoted decimal comma" in parsed.rejected[0].reason


def test_comma_rows_of_another_width_are_rejected() -> None:
    data = (
        b"ean,name,price,brand\n"
        b"2000000000015,Aloe,12,90,Test\n"  # one cell too many
        b"2000000000022,Oil,8\n"  # one cell short
        b'2000000000039,Paste,"3,15",\n'  # right width, quoted price
    )
    parsed = parse_import(data, ImportSettings.create(_WITH_BRAND, delimiter=","))
    assert [(r.line, r.reason) for r in parsed.rejected] == [
        (2, "row has 5 cells but the header has 4"),
        (3, "row has 3 cells but the header has 4"),
    ]
    assert [(r.ean, r.regular_cents) for r in parsed.rows] == [("2000000000039", 315)]


def test_comma_unambiguous_prices_are_accepted() -> None:
    data = (
        b"ean,price,shelf,name\n"
        b'2000000000015,"12,90",7,Aloe\n'  # quoted price; a small number after it is fine
        b"2000000000022,12,A7,Oil\n"  # bare integer, but the next cell is not 1-2 digits
        b"2000000000039,12,100,Paste\n"
    )
    parsed = parse_import(data, ImportSettings.create(_SIMPLE, delimiter=","))
    assert parsed.rejected == []
    assert [r.regular_cents for r in parsed.rows] == [1290, 1200, 1200]


def test_semicolon_equivalents_still_import() -> None:
    trailing = b"ean;name;price;\n2000000000015;Aloe;12,90\n2000000000022;Oil;12;90\n"
    parsed = parse_import(trailing, ImportSettings.create(_SIMPLE, delimiter=";"))
    assert parsed.rejected == []
    # With ';' a decimal comma never splits a cell, so "12" then "90" are two columns.
    assert [(r.ean, r.name, r.regular_cents) for r in parsed.rows] == [
        ("2000000000015", "Aloe", 1290),
        ("2000000000022", "Oil", 1200),
    ]
    reordered = b"ean;price;name;brand\n2000000000015;12,90;Aloe\n2000000000022;12;90;Oil\n"
    parsed = parse_import(reordered, ImportSettings.create(_WITH_BRAND, delimiter=";"))
    assert parsed.rejected == []
    assert [(r.regular_cents, r.name, r.brand) for r in parsed.rows] == [
        (1290, "Aloe", None),
        (1200, "90", "Oil"),
    ]

"""Every reason the importer gives (in English) is recognised and shown in Greek.

The reasons are produced by the importer itself here, not copied, so a changed
message in ``core.domain.catalogue`` or ``core.importers`` fails this test."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from core.app.i18n import t
from core.app.import_messages import (
    file_error,
    is_known_file_error,
    is_known_row_reason,
    row_reason,
)
from core.domain.catalogue import (
    Column,
    normalise_ean,
    parse_pack,
    parse_price_cents,
    parse_row,
)
from core.importers.csv_file import ImportFormatError, decode
from core.importers.profile import ImportSettings, parse_import

SETTINGS = ImportSettings.create({"ean": "EAN", "name": "Name", "price": "Price"})
COMMA = ImportSettings.create({"ean": "EAN", "name": "Name", "price": "Price"}, delimiter=",")


def _message(call: Callable[[], object]) -> str:
    with pytest.raises(ValueError) as error:
        call()
    return str(error.value)


ROW_CASES: list[Callable[[], object]] = [
    lambda: parse_price_cents(""),
    lambda: parse_price_cents("-1"),
    lambda: parse_price_cents("0,00"),
    lambda: parse_price_cents("99999999999"),
    lambda: parse_price_cents("abc"),
    lambda: parse_price_cents("1,290.50"),
    lambda: parse_price_cents("1.29,50"),
    lambda: parse_price_cents("0.290,50"),
    lambda: parse_price_cents("1.290"),
    lambda: parse_price_cents("1,234"),
    lambda: parse_price_cents("1,2,3"),
    lambda: parse_pack("abc", "g"),
    lambda: parse_pack("1,290.5", "g"),
    lambda: parse_pack("1.29,5", "g"),
    lambda: parse_pack("0.290,5", "g"),
    lambda: parse_pack("1.000", "g"),
    lambda: parse_pack("1,1234567", "g"),
    lambda: parse_pack("5", "xyz"),
    lambda: parse_pack("5 g", "ml"),
    lambda: parse_pack("5", ""),
    lambda: parse_pack("1,0001", "g"),
    lambda: parse_pack("0", "g"),
    lambda: parse_pack("9999999999", "g"),
    lambda: normalise_ean(""),
    lambda: normalise_ean("5.20123E+12"),
    lambda: normalise_ean("123"),
    lambda: normalise_ean("123456789012"),
    lambda: normalise_ean("12345678901"),
    lambda: normalise_ean("0000000000000"),
    lambda: normalise_ean("5201234567891"),
]


@pytest.mark.parametrize("case", range(len(ROW_CASES)))
def test_cell_reasons_are_recognised(case: int) -> None:
    reason = _message(ROW_CASES[case])
    assert is_known_row_reason(reason), reason
    assert row_reason(reason) != t("import.reason.unknown", detail=reason)


def _row_reasons(**cells: str) -> list[str]:
    values = {Column.EAN: "5201234567894", Column.NAME: "Κρέμα", Column.PRICE: "1,00"}
    values.update({Column(k): v for k, v in cells.items()})
    row = parse_row(2, values)
    return [getattr(row, "reason", "")]


@pytest.mark.parametrize(
    "cells", [{"name": " "}, {"name": "x" * 301}, {"brand": "x" * 201}], ids=str
)
def test_row_field_reasons_are_recognised(cells: dict[str, str]) -> None:
    [reason] = _row_reasons(**cells)
    assert reason and is_known_row_reason(reason), reason


def test_file_level_row_reasons() -> None:
    data = "EAN;Name;Price\n5201234567894;A;1,00;x\n5201234567894;B;2,00\n5201234567894;C;0\n"
    parsed = parse_import(data.encode(), SETTINGS)
    reasons = [r.reason for r in parsed.rejected]
    assert len(reasons) == 3
    for reason in reasons:
        assert is_known_row_reason(reason), reason
    greek = row_reason(reasons[-1])
    assert greek.startswith(t("import.reason.duplicate_ean", ean="5201234567894", lines="3, 4"))
    assert "Μηδενική τιμή" in greek  # the nested reason is translated too


FILE_CASES: list[Callable[[], object]] = [
    lambda: decode(b"\xff\xfeE\x00"),
    lambda: decode(b"\xff", "utf-8"),
    lambda: parse_import(b"", SETTINGS),
    lambda: parse_import(b"EAN\n1\n", SETTINGS),
    lambda: parse_import(b'EAN;Name;Price\n"1;2;3\n', SETTINGS),
    lambda: parse_import(b"EAN;Other\n1;2\n", SETTINGS),
    lambda: parse_import(b"EAN;Name;Price;price\n1;2;3;4\n", SETTINGS),
    lambda: parse_import(b"EAN,Name,Price\n5201234567894,A,12,90\n", COMMA),
    lambda: parse_import(b"EAN,Name,Price\n5201234567894,A\n", COMMA),
]


@pytest.mark.parametrize("case", range(len(FILE_CASES)))
def test_file_errors_are_recognised(case: int) -> None:
    with pytest.raises(ImportFormatError) as error:
        FILE_CASES[case]()
    message = str(error.value)
    assert is_known_file_error(message), message
    assert file_error(message) != t("import.file.unknown", detail=message)


def test_unknown_reason_keeps_the_original() -> None:
    assert "something new" in row_reason("something new")
    assert "something new" in file_error("something new")


def test_single_values_lose_their_quotes_lists_keep_them() -> None:
    assert row_reason("price is negative: '-1'") == t("import.reason.price_negative", value="-1")
    text = file_error("the file lacks the column(s) 'A', 'B'; its header is 'C'")
    assert "'A', 'B'" in text and "C" in text

"""Greek UI formats (decision 0015): money, signed money, dates and times in Athens."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from core.app import formatting as f


@pytest.fixture(autouse=True)
def athens(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The real value comes from settings (DISPLAY_TIMEZONE, default Europe/Athens),
    # which need DATABASE_URL; these tests run without a database.
    monkeypatch.setattr(f, "display_tz", lambda: ZoneInfo("Europe/Athens"))
    yield


def test_money() -> None:
    assert f.money(1290) == "12,90 €"
    assert f.money(5) == "0,05 €"
    assert f.money(129050) == "1290,50 €"  # no thousands separator, as on labels
    assert f.money(None) == ""


def test_signed_money() -> None:
    assert f.signed_money(200) == "+2,00 €"
    assert f.signed_money(-200) == "\N{MINUS SIGN}2,00 €"
    assert f.signed_money(0) == "0,00 €"


def test_dates_and_times_in_athens() -> None:
    winter = datetime(2026, 11, 18, 12, 5, tzinfo=UTC)
    assert f.date_el(winter) == "18/11/2026"
    assert f.time_el(winter) == "14:05"  # EET, UTC+2
    assert f.datetime_el(winter) == "18/11/2026 14:05"
    summer = datetime(2026, 7, 1, 21, 30, tzinfo=UTC)
    assert f.datetime_el(summer) == "02/07/2026 00:30"  # EEST, UTC+3: the next day
    assert f.date_el(date(2026, 1, 2)) == "02/01/2026"
    assert f.date_el(None) == f.time_el(None) == f.datetime_el(None) == ""


def test_naive_datetimes_are_refused() -> None:
    with pytest.raises(ValueError):
        f.datetime_el(datetime(2026, 1, 1, 12, 0))  # noqa: DTZ001


@pytest.mark.parametrize(
    ("text", "cents"),
    [("12,90", 1290), ("12.90", 1290), ("12,9", 1290), ("12", 1200), (" 12,90 € ", 1290),
     ("0,05", 5), ("0", 0), ("12,345", None), ("-1", None), ("1.290,50", None), ("", None),
     ("abc", None), ("1e3", None)],
)  # fmt: skip
def test_parse_cents(text: str, cents: int | None) -> None:
    assert f.parse_cents(text) == cents


@pytest.mark.parametrize(
    ("text", "milli"),
    [("4,8", 4800), ("200", 200000), ("0,001", 1), ("1.5", 1500), ("1,0001", None), ("", None)],
)
def test_parse_milli(text: str, milli: int | None) -> None:
    assert f.parse_milli(text) == milli


def test_milli_and_pack_display() -> None:
    assert f.milli_input(4800) == "4,8"
    assert f.milli_input(200000) == "200"
    assert f.milli_input(1) == "0,001"
    assert f.milli_input(None) == ""
    assert f.pack_size(30000, "pieces") == "30 τεμ."
    assert f.pack_size(None, "g") == ""


def test_cents_input_and_percent() -> None:
    assert f.cents_input(1290) == "12,90"
    assert f.cents_input(None) == ""
    assert f.percent(120) == "+120 %"
    assert f.percent(-60) == "\N{MINUS SIGN}60 %"
    assert f.percent(None) == ""


def test_field_value() -> None:
    assert f.field_value("pack_qty_milli", 4800) == "4,8"
    assert f.field_value("pack_unit", "ml") == "ml"
    assert f.field_value("brand", None) == "\N{EM DASH}"
    assert f.field_value("name", "Κρέμα") == "Κρέμα"

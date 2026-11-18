"""Greek formats for the UI (decision 0015). Money is integer cents throughout.

- Money: ``12,90 €`` via ``core.domain.pricing.format_price`` (no thousands separator,
  as on the labels). Signed amounts, such as a price change, as ``+2,00 €`` or
  ``-2,00 €`` (with the minus sign U+2212), formatting the absolute value.
- Dates ``18/11/2026``, times ``14:05``, both in the display time zone
  (``Europe/Athens``) from UTC values; the database stays UTC.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from core.app.i18n import t
from core.domain.pricing import format_price
from core.domain.types import PackUnit

MINUS = "\N{MINUS SIGN}"


@lru_cache
def display_tz() -> ZoneInfo:
    from core.config import get_settings

    return ZoneInfo(get_settings().display_timezone)


def money(cents: int | None) -> str:
    """``1290`` -> ``12,90 €``; ``None`` -> empty."""
    if cents is None:
        return ""
    return format_price(cents)


def signed_money(cents: int) -> str:
    """A change in cents with its sign: ``+2,00 €``, ``-2,00 €``, ``0,00 €``."""
    if cents == 0:
        return format_price(0)
    return f"{'+' if cents > 0 else MINUS}{format_price(abs(cents))}"


def cents_input(cents: int | None) -> str:
    """Cents as a form value: ``1290`` -> ``12,90``."""
    if cents is None:
        return ""
    return f"{cents // 100},{cents % 100:02d}"


def local(when: datetime) -> datetime:
    """An aware UTC datetime in the display time zone."""
    if when.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware")
    return when.astimezone(display_tz())


def date_el(when: datetime | date | None) -> str:
    """``18/11/2026``; a datetime is converted to the display time zone first."""
    if when is None:
        return ""
    if isinstance(when, datetime):
        when = local(when)
    return when.strftime("%d/%m/%Y")


def time_el(when: datetime | None) -> str:
    """``14:05`` in the display time zone."""
    return "" if when is None else local(when).strftime("%H:%M")


def datetime_el(when: datetime | None) -> str:
    """``18/11/2026 14:05`` in the display time zone."""
    return "" if when is None else local(when).strftime("%d/%m/%Y %H:%M")


def age_el(delta: timedelta) -> str:
    """How long ago, in whole minutes, hours or days: ``3 ημέρες``."""
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return t("age.minute") if minutes == 1 else t("age.minutes", n=minutes)
    hours = minutes // 60
    if hours < 24:
        return t("age.hour") if hours == 1 else t("age.hours", n=hours)
    days = hours // 24
    return t("age.day") if days == 1 else t("age.days", n=days)


def date_input(when: date | None) -> str:
    """A date as an HTML date input value (ISO)."""
    return "" if when is None else when.isoformat()


def pack_size(qty_milli: int | None, unit: str | None) -> str:
    """``4800, "g"`` -> ``4,8 g``; empty when either is missing."""
    if qty_milli is None or unit is None:
        return ""
    return f"{milli_input(qty_milli)} {t('unit.' + PackUnit(unit).value)}"


def milli_input(qty_milli: int | None) -> str:
    """Thousandths as a Greek decimal: ``4800`` -> ``4,8``, ``200000`` -> ``200``."""
    if qty_milli is None:
        return ""
    whole, frac = divmod(qty_milli, 1000)
    return f"{whole},{frac:03d}".rstrip("0").rstrip(",")


def field_value(field: str, value: object) -> str:
    """A product field's value as the import preview shows it."""
    if value is None or value == "":
        return "—"
    if field == "pack_qty_milli" and isinstance(value, int):
        return milli_input(value)
    if field == "pack_unit":
        return t("unit." + PackUnit(str(value)).value)
    return str(value)


def percent(value: int | None) -> str:
    """A whole percent with its sign: ``+120 %``, ``-60 %``."""
    if value is None:
        return ""
    sign = "+" if value > 0 else MINUS if value < 0 else ""
    return f"{sign}{abs(value)} %"


# --- Parsing form input ---------------------------------------------------------------

_MONEY = re.compile(r"^(\d{1,8})(?:[.,](\d{1,2}))?$")
_DECIMAL3 = re.compile(r"^(\d{1,9})(?:[.,](\d{1,3}))?$")


def parse_cents(text: str) -> int | None:
    """``12,90``, ``12.9`` or ``12`` (optionally with ``€``) -> cents; ``None`` if it
    is not such a number. Never uses floats."""
    cleaned = text.replace("€", "").replace(" ", "").replace("\N{NO-BREAK SPACE}", "").strip()
    found = _MONEY.match(cleaned)
    if found is None:
        return None
    whole, frac = found.groups()
    return int(whole) * 100 + int((frac or "").ljust(2, "0"))


def parse_milli(text: str) -> int | None:
    """``4,8`` -> ``4800`` thousandths; ``None`` if it is not such a number."""
    found = _DECIMAL3.match(text.replace(" ", "").strip())
    if found is None:
        return None
    whole, frac = found.groups()
    return int(whole) * 1000 + int((frac or "").ljust(3, "0"))

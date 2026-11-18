"""Pure rules of the binding and dashboard pages (no database): template choice, ages,
staleness and battery thresholds."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.app.binding import default_template, pick_template, template_options
from core.app.formatting import age_el
from core.app.i18n import t
from core.app.routers.ui_dashboard import gateway_stale, import_stale
from core.db.models import GatewayRow, Price, Product, Tag, TagTypeRow
from core.render.label import TemplateKey as K
from core.services.admin_queries import is_low_battery, is_unseen

NOW = datetime(2026, 12, 1, 8, 0, tzinfo=UTC)
BWR = TagTypeRow(key="t", vendor="fake", model="m", width=296, height=128, palette="BWR")
BW = TagTypeRow(key="t", vendor="fake", model="m", width=296, height=128, palette="BW")


def product(**fields: object) -> Product:
    base: dict[str, object] = {"id": 1, "ean": "2900000000015", "name": "x", "brand": None,
                               "pack_qty_milli": None, "pack_unit": None,
                               "unit_price_required": False, "is_rx": False}  # fmt: skip
    return Product(**{**base, **fields})


@pytest.mark.parametrize(
    ("fields", "tag_type", "expected"),
    [
        ({}, BWR, [K.STANDARD, K.PROMO]),
        ({}, BW, [K.STANDARD]),
        ({"is_rx": True}, BWR, [K.STANDARD]),
        ({"pack_qty_milli": 50_000, "pack_unit": "ml"}, BWR, [K.STANDARD, K.UNIT_PRICE, K.PROMO]),
        ({"pack_qty_milli": 50_000, "pack_unit": "ml", "unit_price_required": True}, BWR,
         [K.UNIT_PRICE, K.PROMO]),
    ],
)  # fmt: skip
def test_template_options(
    fields: dict[str, object], tag_type: TagTypeRow, expected: list[K]
) -> None:
    assert template_options(product(**fields), tag_type) == expected


def test_default_follows_choose_template_and_falls_back() -> None:
    price = Price(regular_cents=1290, promo_cents=990, promo_from=NOW - timedelta(hours=1),
                  promo_to=NOW + timedelta(days=1), promo_prior_cents=None)  # fmt: skip
    plain = product()
    assert default_template(plain, price, [K.STANDARD, K.PROMO], NOW) is K.PROMO
    assert default_template(plain, price, [K.STANDARD], NOW) is K.STANDARD  # BW tag
    assert default_template(plain, None, [K.STANDARD, K.PROMO], NOW) is K.STANDARD
    assert pick_template("unassigned", plain, None, BWR, NOW)[1] is K.STANDARD
    assert pick_template("promo", plain, None, BWR, NOW)[1] is K.PROMO


def test_age_el() -> None:
    assert age_el(timedelta(seconds=30)) == t("age.minutes", n=0)
    assert age_el(timedelta(minutes=1)) == t("age.minute")
    assert age_el(timedelta(hours=5, minutes=59)) == t("age.hours", n=5)
    assert age_el(timedelta(days=1, hours=23)) == t("age.day")
    assert age_el(timedelta(days=10)) == t("age.days", n=10)


def test_thresholds() -> None:
    assert import_stale(None, NOW, 7)
    assert not import_stale(NOW - timedelta(days=7), NOW, 7)
    assert import_stale(NOW - timedelta(days=7, seconds=1), NOW, 7)
    gateway = GatewayRow(last_heartbeat_at=NOW - timedelta(minutes=5))
    assert not gateway_stale(gateway, NOW)
    gateway.last_heartbeat_at = NOW - timedelta(minutes=5, seconds=1)
    assert gateway_stale(gateway, NOW)
    assert gateway_stale(GatewayRow(last_heartbeat_at=None), NOW)
    day = timedelta(hours=24)
    assert is_unseen(Tag(last_seen_at=None), NOW, day)
    assert not is_unseen(Tag(last_seen_at=NOW - day), NOW, day)
    assert is_unseen(Tag(last_seen_at=NOW - day - timedelta(seconds=1)), NOW, day)
    assert is_low_battery(Tag(battery_pct=19, battery_mv=3000), 20, 2600)  # percent wins
    assert not is_low_battery(Tag(battery_pct=20, battery_mv=2000), 20, 2600)
    assert is_low_battery(Tag(battery_pct=None, battery_mv=2599), 20, 2600)
    assert not is_low_battery(Tag(battery_pct=None, battery_mv=None), 20, 2600)

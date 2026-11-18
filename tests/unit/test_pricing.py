"""Pricing rules (workstream B). Placeholders until implemented: remove each
``xfail`` marker as the function lands; strict xfail makes a passing test fail CI."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.domain.pricing import (
    PromoRefusal,
    PromoRefused,
    UnitPrice,
    build_label_data,
    choose_template,
    format_price,
    lowest_prior_price,
    resolve_prior_price,
    unit_price,
    validate_promo,
)
from core.domain.types import (
    PackUnit,
    PriceKind,
    PricePoint,
    PriceSource,
    PriceState,
    ProductFacts,
    UnitBasis,
)
from core.render.label import TemplateKey

pytestmark = pytest.mark.xfail(raises=NotImplementedError, reason="workstream B")

T0 = datetime(2026, 12, 1, 8, 0, tzinfo=UTC)


def _point(cents: int, days_before: int) -> PricePoint:
    return PricePoint(
        cents, PriceKind.REGULAR, T0 - timedelta(days=days_before), PriceSource.IMPORT
    )


def _product(**kw: object) -> ProductFacts:
    base: dict[str, object] = dict(
        product_id=1, ean="5201234567890", name="Κρέμα χεριών", brand="Korres",
        pack_qty=200, pack_unit=PackUnit.MILLILITRE, unit_price_required=False, is_rx=False,
    )  # fmt: skip
    base.update(kw)
    return ProductFacts(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("cents", "text"), [(50, "0,50 €"), (1290, "12,90 €"), (99999, "999,99 €"), (0, "0,00 €")]
)
def test_format_price(cents: int, text: str) -> None:
    assert format_price(cents) == text


def test_unit_price_per_kg() -> None:
    assert unit_price(450, 200, PackUnit.GRAM) == UnitPrice(2250, UnitBasis.KILOGRAM)


def test_lowest_prior_price_includes_price_in_effect_at_window_start() -> None:
    history = [_point(900, 60), _point(1200, 20), _point(1100, 5)]
    assert lowest_prior_price(history, T0) == 900


def test_lowest_prior_price_none_when_history_too_short() -> None:
    assert lowest_prior_price([_point(1200, 10)], T0) is None


def test_resolve_prior_price_uses_manual_entry_when_history_short() -> None:
    price = PriceState(1200, 999, T0, T0 + timedelta(days=7), promo_prior_cents=1200)
    prior = resolve_prior_price([_point(1200, 10)], price)
    assert (prior.cents, prior.from_history) == (1200, False)


def test_promo_without_prior_price_is_refused() -> None:
    price = PriceState(1200, 999, T0, T0 + timedelta(days=7))
    with pytest.raises(PromoRefused) as exc:
        validate_promo(_product(), price, [_point(1200, 10)])
    assert exc.value.reason is PromoRefusal.NO_PRIOR_PRICE


def test_rx_promo_is_refused() -> None:
    price = PriceState(1200, 999, T0, T0 + timedelta(days=7))
    with pytest.raises(PromoRefused) as exc:
        validate_promo(_product(is_rx=True), price, [_point(1200, 60)])
    assert exc.value.reason is PromoRefusal.RX_PRODUCT


def test_rx_never_gets_promo_template() -> None:
    price = PriceState(1200, 999, T0, T0 + timedelta(days=7))
    assert choose_template(_product(is_rx=True), price, T0 + timedelta(hours=1)) is not (
        TemplateKey.PROMO
    )


def test_promo_label_never_built_without_prior_price() -> None:
    price = PriceState(1200, 999, T0, T0 + timedelta(days=7))
    with pytest.raises(PromoRefused):
        build_label_data(_product(), price, [], TemplateKey.PROMO, T0 + timedelta(hours=1))

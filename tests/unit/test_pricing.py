"""Pricing rules (workstream B). Placeholders until implemented: remove each
``xfail`` marker as the function lands; strict xfail makes a passing test fail CI."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.domain.pricing import (
    PromoRefusal,
    PromoRefused,
    TemplateChoice,
    UnitPrice,
    apply_price_change,
    apply_product_change,
    build_label_data,
    choose_template,
    effective_template,
    format_price,
    history_rows_for_change,
    lowest_prior_price,
    next_label_change,
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


_seq = iter(range(1, 10_000))


def _point(cents: int, days_before: int, kind: PriceKind = PriceKind.REGULAR) -> PricePoint:
    when = T0 - timedelta(days=days_before)
    return PricePoint(next(_seq), cents, kind, when, PriceSource.IMPORT)


def _product(**kw: object) -> ProductFacts:
    base: dict[str, object] = dict(
        product_id=1, ean="5201234567890", name="Κρέμα χεριών", brand="Korres",
        pack_qty_milli=200_000, pack_unit=PackUnit.MILLILITRE, unit_price_required=False,
        is_rx=False,
    )  # fmt: skip
    base.update(kw)
    return ProductFacts(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("cents", "text"), [(50, "0,50 €"), (1290, "12,90 €"), (99999, "999,99 €"), (0, "0,00 €")]
)
def test_format_price(cents: int, text: str) -> None:
    assert format_price(cents) == text


def test_unit_price_per_kg() -> None:
    assert unit_price(450, 200_000, PackUnit.GRAM) == UnitPrice(2250, UnitBasis.KILOGRAM)


def test_unit_price_fractional_pack() -> None:
    assert unit_price(390, 4_800, PackUnit.GRAM) == UnitPrice(81250, UnitBasis.KILOGRAM)


def test_lowest_prior_price_includes_price_in_effect_at_window_start() -> None:
    history = [_point(900, 60), _point(1200, 20), _point(1100, 5)]
    assert lowest_prior_price(history, T0) == 900


def test_earlier_promo_counts_but_only_while_in_effect() -> None:
    """Regular 10,00 from day -60; promo 8,00 from day -50 to day -40 (closing
    regular row written with the promo): the prior price for day 0 is 10,00."""
    history = [_point(1000, 60), _point(800, 50, PriceKind.PROMO), _point(1000, 40)]
    assert lowest_prior_price(history, T0) == 1000


def test_promo_inside_window_lowers_prior_price() -> None:
    history = [_point(1000, 60), _point(800, 20, PriceKind.PROMO), _point(1000, 10)]
    assert lowest_prior_price(history, T0) == 800


def test_later_row_overrides_scheduled_rows() -> None:
    """A promo scheduled for day -20..-10 was cancelled on day -25: it never applied."""
    history = [
        _point(1000, 60),
        _point(800, 20, PriceKind.PROMO),
        _point(1000, 10),
        _point(1000, 25),
    ]
    assert lowest_prior_price(history, T0) == 1000


def test_history_rows_for_scheduled_promo() -> None:
    now = T0 - timedelta(days=1)
    price = PriceState(1000, 800, T0, T0 + timedelta(days=7))
    rows = history_rows_for_change(price, now, PriceSource.MANUAL)
    assert [(r.effective_cents, r.kind, r.valid_from, r.source) for r in rows] == [
        (1000, PriceKind.REGULAR, now, PriceSource.MANUAL),
        (800, PriceKind.PROMO, T0, PriceSource.PROMO),
        (1000, PriceKind.REGULAR, T0 + timedelta(days=7), PriceSource.PROMO),
    ]


def test_promo_until_shows_last_day_in_athens() -> None:
    start = datetime(2026, 11, 20, 22, 0, tzinfo=UTC)  # 21/11 00:00 Athens
    end = datetime(2026, 11, 30, 22, 0, tzinfo=UTC)  # 01/12 00:00 Athens, exclusive
    price = PriceState(1200, 999, start, end, promo_prior_cents=1200)
    label = build_label_data(_product(), price, [], TemplateKey.PROMO, start)
    assert label.promo_until_text == "30/11"


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


def test_refused_promo_falls_back_to_regular_label() -> None:
    """A promo accepted earlier becomes refused once the 30-day low drops below it."""
    price = PriceState(1000, 800, T0, T0 + timedelta(days=7))
    history = [_point(1000, 60), _point(790, 3)]  # import lowered the price 3 days ago
    choice = effective_template(
        TemplateKey.STANDARD, _product(), price, history, T0 + timedelta(hours=1)
    )
    assert choice == TemplateChoice(TemplateKey.STANDARD, PromoRefusal.NOT_A_DISCOUNT)


def test_scheduled_promo_refused_by_later_import_is_removed() -> None:
    """Regular 10,00 with a promo of 8,00 starting at T0. Three days before, an import
    lowers regular to 7,90: the promo is now no discount, so it is dropped and never
    written to history as a shelf price."""
    old = PriceState(1000, 800, T0, T0 + timedelta(days=40))
    requested = PriceState(790, 800, T0, T0 + timedelta(days=40))
    change = apply_price_change(
        _product(), old, requested, [_point(1000, 60)], T0 - timedelta(days=3),
        PriceSource.IMPORT,
    )  # fmt: skip
    assert change.state == PriceState(790)
    assert [r.kind for r in change.history_rows] == [PriceKind.REGULAR]
    assert [e.kind for e in change.events] == ["promo.refused"]


def test_changing_promo_clears_manual_prior_price() -> None:
    old = PriceState(1200, 999, T0, T0 + timedelta(days=7), promo_prior_cents=1200)
    requested = PriceState(1200, 949, T0, T0 + timedelta(days=7), promo_prior_cents=1200)
    with pytest.raises(PromoRefused):  # short history and the manual prior was cleared
        apply_price_change(_product(), old, requested, [_point(1200, 10)], T0, PriceSource.MANUAL)


def test_price_drop_earlier_the_same_day_counts_for_prior_price() -> None:
    """Regular 10,00 for 60 days, dropped to 9,00 three hours ago; a promo saved now
    takes effect now, so the 30-day window ends now and includes the drop."""
    now = T0
    drop = PricePoint(900, 900, PriceKind.REGULAR, now - timedelta(hours=3), PriceSource.MANUAL)
    history = [_point(1000, 60), drop]
    assert lowest_prior_price(history, now) == 900
    week = now + timedelta(days=7)
    change = apply_price_change(
        _product(), PriceState(900), PriceState(900, 850, now, week), history, now,
        PriceSource.MANUAL,
    )  # fmt: skip
    assert change.state.promo_cents == 850
    with pytest.raises(PromoRefused) as exc:  # 9,50 is a "discount" only on the old price
        apply_price_change(
            _product(), PriceState(900), PriceState(900, 950, now, week), history, now,
            PriceSource.MANUAL,
        )  # fmt: skip
    assert exc.value.reason in (PromoRefusal.NOT_A_DISCOUNT, PromoRefusal.NOT_BELOW_REGULAR)


def test_promo_starting_in_the_past_is_refused() -> None:
    requested = PriceState(1000, 800, T0 - timedelta(minutes=1), T0 + timedelta(days=7))
    with pytest.raises(PromoRefused) as exc:
        apply_price_change(
            _product(), PriceState(1000), requested, [_point(1000, 60)], T0, PriceSource.MANUAL
        )
    assert exc.value.reason is PromoRefusal.STARTS_IN_PAST


def test_promo_must_be_strictly_below_prior_and_regular_price() -> None:
    week = T0 + timedelta(days=7)
    # Prior price (30-day low) 9,00, regular 10,00: 9,50 and 10,00 are not below the prior.
    history = [_point(900, 60), _point(1000, 10)]
    for promo_cents in (950, 900, 1000):
        with pytest.raises(PromoRefused) as exc:
            validate_promo(_product(), PriceState(1000, promo_cents, T0, week), history)
        assert exc.value.reason is PromoRefusal.NOT_A_DISCOUNT
    # Short history, so an admin entered a prior price of 12,00; regular is 10,00.
    # 10,00 and 11,00 are below the prior price but not strictly below regular.
    history = [_point(1000, 10)]
    for promo_cents in (1000, 1100):
        price = PriceState(1000, promo_cents, T0, week, promo_prior_cents=1200)
        with pytest.raises(PromoRefused) as exc:
            validate_promo(_product(), price, history)
        assert exc.value.reason is PromoRefusal.NOT_BELOW_REGULAR
    validate_promo(_product(), PriceState(1000, 990, T0, week, promo_prior_cents=1200), history)


def test_re_entering_same_manual_prior_price_is_accepted_and_logged() -> None:
    week = T0 + timedelta(days=7)
    old = PriceState(1200, 999, T0, week, promo_prior_cents=1200)
    change = apply_price_change(
        _product(), old, PriceState(1200, 949, T0, week), [_point(1200, 10)], T0,
        PriceSource.MANUAL, entered_prior_cents=1200,
    )  # fmt: skip
    assert change.state.promo_prior_cents == 1200
    assert "promo.prior_set" in [e.kind for e in change.events]


def test_setting_rx_ends_active_promo_with_history_row() -> None:
    price = PriceState(1000, 800, T0 - timedelta(days=1), T0 + timedelta(days=7))
    change = apply_product_change(_product(), _product(is_rx=True), price, [_point(1000, 60)], T0)
    assert change is not None
    assert change.state == PriceState(1000)
    first = change.history_rows[0]
    assert (first.effective_cents, first.kind, first.valid_from) == (1000, PriceKind.REGULAR, T0)
    assert [e.kind for e in change.events] == ["promo.ended_rx"]


def test_promo_label_shows_unit_price_from_promo_price() -> None:
    product = _product(unit_price_required=True, pack_qty_milli=200_000, pack_unit=PackUnit.GRAM)
    price = PriceState(1000, 800, T0, T0 + timedelta(days=7))
    label = build_label_data(
        product, price, [_point(1000, 60)], TemplateKey.PROMO, T0 + timedelta(hours=1)
    )
    assert label.unit_price_text == "40,00 €/kg"  # 8,00 € for 200 g


def test_next_label_change() -> None:
    price = PriceState(1000, 800, T0, T0 + timedelta(days=7))
    assert next_label_change(price, T0 - timedelta(days=1)) == T0
    assert next_label_change(price, T0) == T0 + timedelta(days=7)
    assert next_label_change(PriceState(1000), T0) is None


def test_promo_label_never_built_without_prior_price() -> None:
    price = PriceState(1200, 999, T0, T0 + timedelta(days=7))
    with pytest.raises(PromoRefused):
        build_label_data(_product(), price, [], TemplateKey.PROMO, T0 + timedelta(hours=1))

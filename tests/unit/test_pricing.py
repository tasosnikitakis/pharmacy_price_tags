"""Pricing rules (workstream B, PLAN.md section 6 and docs/decisions/0008, 0009).

The first part holds the wave-0 contract tests; the second adds edge cases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.domain.pricing import (
    PriceEvent,
    PriorPrice,
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
    format_unit_price,
    history_rows_for_change,
    lowest_prior_price,
    next_label_change,
    price_timeline,
    promo_active,
    resolve_prior_price,
    unassigned_label_data,
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
    history = [_point(1000, 60)]  # written first, so it has the lower seq
    history.append(PricePoint(next(_seq), 900, PriceKind.REGULAR, now - timedelta(hours=3),
                              PriceSource.MANUAL))  # fmt: skip
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
    assert exc.value.reason is PromoRefusal.NOT_A_DISCOUNT  # prior is checked first


def test_price_drop_reversed_the_same_day_still_sets_prior_price() -> None:
    """10,00, then 9,00 at 07:00, back to 10,00 at 09:00, promo of 9,50 saved at 10:00.
    Regular is 10,00 again, so only the 30-day window (which includes the 9,00 hours)
    refuses the promo."""
    now = datetime(2026, 12, 1, 8, 0, tzinfo=UTC)  # 10:00 Athens
    seven, nine = now - timedelta(hours=3), now - timedelta(hours=1)
    history = [
        _point(1000, 60),
        PricePoint(next(_seq), 900, PriceKind.REGULAR, seven, PriceSource.MANUAL),
        PricePoint(next(_seq), 1000, PriceKind.REGULAR, nine, PriceSource.MANUAL),
    ]
    assert lowest_prior_price(history, now) == 900
    with pytest.raises(PromoRefused) as exc:
        apply_price_change(
            _product(), PriceState(1000), PriceState(1000, 950, now, now + timedelta(days=7)),
            history, now, PriceSource.MANUAL,
        )  # fmt: skip
    assert exc.value.reason is PromoRefusal.NOT_A_DISCOUNT


def test_running_promo_can_be_extended_and_keeps_its_prior_price() -> None:
    """A promo running since day -2 is extended on day 0. Its promo_from is in the past
    but unchanged, so the edit is allowed, and the prior price is still the 30-day low
    before the original start (10,00), not the promo's own 8,00."""
    start = T0 - timedelta(days=2)
    history = [_point(1000, 60), PricePoint(next(_seq), 800, PriceKind.PROMO, start,
               PriceSource.PROMO)]  # fmt: skip
    old = PriceState(1000, 800, start, T0 + timedelta(days=5))
    longer = PriceState(1000, 800, start, T0 + timedelta(days=12))
    change = apply_price_change(_product(), old, longer, history, T0, PriceSource.MANUAL)
    assert change.state.promo_to == T0 + timedelta(days=12)
    assert resolve_prior_price(history, change.state).cents == 1000
    moved = PriceState(1000, 800, start + timedelta(hours=1), T0 + timedelta(days=12))
    with pytest.raises(PromoRefused) as exc:  # moving promo_from into the past is not
        apply_price_change(_product(), old, moved, history, T0, PriceSource.MANUAL)
    assert exc.value.reason is PromoRefusal.STARTS_IN_PAST


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


def test_repricing_a_running_promo_keeps_both_limits() -> None:
    """Regular 10,00 for 60 days, promo 8,00 running since day -3, so its prior price
    is 10,00. Since the start an import raised regular to 12,00. Repricing the promo:
    9,50 is fine; 10,50 and 10,00 are below regular but not below the original prior
    price; 12,50 is above both."""
    start = T0 - timedelta(days=3)
    history = [
        _point(1000, 60),
        PricePoint(next(_seq), 800, PriceKind.PROMO, start, PriceSource.PROMO),
    ]
    end = T0 + timedelta(days=7)
    old = PriceState(1200, 800, start, end)
    ok = apply_price_change(_product(), old, PriceState(1200, 950, start, end), history, T0,
                            PriceSource.MANUAL)  # fmt: skip
    assert ok.state.promo_cents == 950
    for cents in (1050, 1000, 1250):
        with pytest.raises(PromoRefused) as exc:
            apply_price_change(_product(), old, PriceState(1200, cents, start, end), history,
                               T0, PriceSource.MANUAL)  # fmt: skip
        assert exc.value.reason is PromoRefusal.NOT_A_DISCOUNT  # prior is checked first


def test_repricing_a_running_promo_stays_below_regular() -> None:
    """Short history, so the running promo uses a manual prior price of 15,00; regular
    is 12,00. Repricing to 12,50 is below the prior price but not below regular."""
    start = T0 - timedelta(days=3)
    end = T0 + timedelta(days=7)
    history = [_point(1200, 10)]
    old = PriceState(1200, 900, start, end, promo_prior_cents=1500)
    with pytest.raises(PromoRefused) as exc:
        apply_price_change(_product(), old, PriceState(1200, 1250, start, end), history, T0,
                           PriceSource.MANUAL, entered_prior_cents=1500)  # fmt: skip
    assert exc.value.reason is PromoRefusal.NOT_BELOW_REGULAR


def test_import_below_running_promo_removes_it_now() -> None:
    """Regular 10,00, promo 8,00 running. An import sets regular to 7,90: the promo is
    no longer below regular, so it ends now and history records the 7,90 on the shelf."""
    start = T0 - timedelta(days=4)
    history = [_point(1000, 60), PricePoint(next(_seq), 800, PriceKind.PROMO, start,
               PriceSource.PROMO)]  # fmt: skip
    old = PriceState(1000, 800, start, T0 + timedelta(days=10))
    requested = PriceState(790, 800, start, T0 + timedelta(days=10))
    change = apply_price_change(_product(), old, requested, history, T0, PriceSource.IMPORT)
    assert change.state == PriceState(790)
    assert [(r.effective_cents, r.kind, r.valid_from) for r in change.history_rows] == [
        (790, PriceKind.REGULAR, T0)
    ]
    refused = [e for e in change.events if e.kind == "promo.refused"]
    assert refused and refused[0].payload["reason"] == PromoRefusal.NOT_BELOW_REGULAR


def test_expired_promo_cannot_be_revived_with_its_old_start() -> None:
    """A promo ran 40 to 30 days ago. Re-saving it with a new end keeps its old start,
    but it is a new promo now, so the past start is refused."""
    start, end = T0 - timedelta(days=40), T0 - timedelta(days=30)
    old = PriceState(1000, 800, start, end)
    revived = PriceState(1000, 800, start, T0 + timedelta(days=7))
    with pytest.raises(PromoRefused) as exc:
        apply_price_change(_product(), old, revived, [_point(1000, 60)], T0,
                           PriceSource.MANUAL)  # fmt: skip
    assert exc.value.reason is PromoRefusal.STARTS_IN_PAST


def test_ending_a_running_promo_now_removes_it() -> None:
    start = T0 - timedelta(days=2)
    old = PriceState(1000, 800, start, T0 + timedelta(days=5))
    change = apply_price_change(_product(), old, PriceState(1000, 800, start, T0),
                                [_point(1000, 60)], T0, PriceSource.MANUAL)  # fmt: skip
    assert change.state == PriceState(1000)


def test_re_entering_identical_prior_price_with_no_other_change_is_logged() -> None:
    week = T0 + timedelta(days=7)
    old = PriceState(1200, 999, T0, week, promo_prior_cents=1200)
    change = apply_price_change(
        _product(), old, PriceState(1200, 999, T0, week), [_point(1200, 10)], T0,
        PriceSource.MANUAL, entered_prior_cents=1200,
    )  # fmt: skip
    assert change.state.promo_prior_cents == 1200
    assert [e.kind for e in change.events].count("promo.prior_set") == 1


def test_import_after_promo_expired_drops_it_silently() -> None:
    """A promo ended two days ago is still stored. An import carries it through
    unchanged: it is dropped, with no refusal and no events."""
    old = PriceState(1000, 800, T0 - timedelta(days=9), T0 - timedelta(days=2))
    requested = PriceState(950, 800, T0 - timedelta(days=9), T0 - timedelta(days=2))
    change = apply_price_change(_product(), old, requested, [_point(1000, 60)], T0,
                                PriceSource.IMPORT)  # fmt: skip
    assert change.state == PriceState(950)
    assert change.events == []


def test_leaving_promo_out_of_a_write_ends_it() -> None:
    start = T0 - timedelta(days=2)
    old = PriceState(1000, 800, start, T0 + timedelta(days=5), promo_prior_cents=1000)
    change = apply_price_change(_product(), old, PriceState(1000), [_point(1000, 10)], T0,
                                PriceSource.MANUAL)  # fmt: skip
    assert change.state == PriceState(1000)
    assert [e.kind for e in change.events] == ["promo.prior_cleared"]


def test_new_promo_ending_before_now_is_an_invalid_period() -> None:
    """A typo: a scheduled promo whose end is already past."""
    requested = PriceState(1000, 800, T0 + timedelta(days=1), T0 - timedelta(hours=1))
    with pytest.raises(PromoRefused) as exc:
        apply_price_change(_product(), PriceState(1000), requested, [_point(1000, 60)], T0,
                           PriceSource.MANUAL)  # fmt: skip
    assert exc.value.reason is PromoRefusal.INVALID_PERIOD


def test_promo_removed_by_revalidation_also_clears_manual_prior() -> None:
    """Short history, manual prior 12,00, promo 9,00 running; an import sets regular
    to 8,50: the promo is removed and its manual prior price is cleared and logged."""
    start = T0 - timedelta(days=2)
    old = PriceState(1000, 900, start, T0 + timedelta(days=5), promo_prior_cents=1200)
    requested = PriceState(850, 900, start, T0 + timedelta(days=5))
    change = apply_price_change(_product(), old, requested, [_point(1000, 10)], T0,
                                PriceSource.IMPORT)  # fmt: skip
    assert change.state == PriceState(850)
    assert sorted(e.kind for e in change.events) == ["promo.prior_cleared", "promo.refused"]


def test_prefilled_prior_is_ignored_when_revalidation_removes_the_promo() -> None:
    """E's form pre-fills the manual prior. The admin lowers regular to 8,50, below the
    unchanged running promo of 9,00: no error; the promo goes, the prior is not stored."""
    start, end = T0 - timedelta(days=2), T0 + timedelta(days=5)
    old = PriceState(1000, 900, start, end, promo_prior_cents=1200)
    change = apply_price_change(_product(), old, PriceState(850, 900, start, end),
                                [_point(1000, 10)], T0, PriceSource.MANUAL,
                                entered_prior_cents=1200)  # fmt: skip
    assert change.state == PriceState(850)
    assert sorted(e.kind for e in change.events) == ["promo.prior_cleared", "promo.refused"]


def test_prefilled_prior_is_ignored_when_running_promo_is_ended() -> None:
    """Rule 1b: the admin ends the running promo by setting its end to now."""
    start = T0 - timedelta(days=2)
    old = PriceState(1000, 900, start, T0 + timedelta(days=5), promo_prior_cents=1200)
    change = apply_price_change(_product(), old, PriceState(1000, 900, start, T0),
                                [_point(1000, 10)], T0, PriceSource.MANUAL,
                                entered_prior_cents=1200)  # fmt: skip
    assert change.state == PriceState(1000)
    assert [e.kind for e in change.events] == ["promo.prior_cleared"]


def test_prefilled_prior_is_ignored_when_expired_promo_is_dropped() -> None:
    """Rule 0: the form is submitted just after the promo ended."""
    start, end = T0 - timedelta(days=9), T0 - timedelta(minutes=1)
    old = PriceState(1000, 900, start, end, promo_prior_cents=1200)
    change = apply_price_change(_product(), old, PriceState(1000, 900, start, end),
                                [_point(1000, 10)], T0, PriceSource.MANUAL,
                                entered_prior_cents=1200)  # fmt: skip
    assert change.state == PriceState(1000)
    assert [e.kind for e in change.events] == ["promo.prior_cleared"]


def test_partial_promo_and_negative_prior_are_errors() -> None:
    with pytest.raises(ValueError):
        apply_price_change(_product(), PriceState(1000), PriceState(1000, 800), [], T0,
                           PriceSource.MANUAL)  # fmt: skip
    week = T0 + timedelta(days=7)
    with pytest.raises(ValueError):
        apply_price_change(_product(), PriceState(1000), PriceState(1000, 800, T0, week),
                           [], T0, PriceSource.MANUAL, entered_prior_cents=-1)  # fmt: skip


def test_entered_prior_without_a_promo_is_an_error() -> None:
    with pytest.raises(ValueError):
        apply_price_change(_product(), PriceState(1000), PriceState(1000), [], T0,
                           PriceSource.MANUAL, entered_prior_cents=1200)  # fmt: skip


def test_next_label_change() -> None:
    price = PriceState(1000, 800, T0, T0 + timedelta(days=7))
    assert next_label_change(price, T0 - timedelta(days=1)) == T0
    assert next_label_change(price, T0) == T0 + timedelta(days=7)
    assert next_label_change(PriceState(1000), T0) is None


def test_promo_label_never_built_without_prior_price() -> None:
    price = PriceState(1200, 999, T0, T0 + timedelta(days=7))
    with pytest.raises(PromoRefused):
        build_label_data(_product(), price, [], TemplateKey.PROMO, T0 + timedelta(hours=1))


# --- edge cases beyond the contract tests -------------------------------------------


def test_format_price_refuses_negative() -> None:
    with pytest.raises(ValueError):
        format_price(-1)


@pytest.mark.parametrize(
    ("cents", "qty", "unit", "expected"),
    [
        (450, 200_000, PackUnit.MILLILITRE, UnitPrice(2250, UnitBasis.LITRE)),
        (320, 20_000, PackUnit.PIECE, UnitPrice(16, UnitBasis.PIECE)),
        (100, 3_000, PackUnit.PIECE, UnitPrice(33, UnitBasis.PIECE)),  # 33,33 -> 33
        (100, 6_000, PackUnit.PIECE, UnitPrice(17, UnitBasis.PIECE)),  # 16,67 -> 17
        (1, 2_000, PackUnit.PIECE, UnitPrice(1, UnitBasis.PIECE)),  # 0,5 rounds half up
        (99999, 1, PackUnit.GRAM, UnitPrice(99_999_000_000, UnitBasis.KILOGRAM)),
    ],
)
def test_unit_price_bases_and_rounding(
    cents: int, qty: int, unit: PackUnit, expected: UnitPrice
) -> None:
    assert unit_price(cents, qty, unit) == expected


@pytest.mark.parametrize("qty", [0, -1])
def test_unit_price_needs_a_positive_pack(qty: int) -> None:
    with pytest.raises(ValueError):
        unit_price(100, qty, PackUnit.GRAM)


def test_format_unit_price() -> None:
    assert format_unit_price(UnitPrice(2250, UnitBasis.KILOGRAM)) == "22,50 €/kg"
    assert format_unit_price(UnitPrice(81250, UnitBasis.KILOGRAM)) == "812,50 €/kg"
    assert format_unit_price(UnitPrice(5, UnitBasis.LITRE)) == "0,05 €/l"
    piece = format_unit_price(UnitPrice(16, UnitBasis.PIECE))
    ok = piece == "0,16 €/τεμ."
    assert ok, ascii(piece)


def test_price_timeline_applies_rows_in_seq_order() -> None:
    a = PricePoint(1, 1000, PriceKind.REGULAR, T0 - timedelta(days=10), PriceSource.IMPORT)
    b = PricePoint(2, 800, PriceKind.PROMO, T0, PriceSource.PROMO)
    c = PricePoint(3, 1000, PriceKind.REGULAR, T0 + timedelta(days=5), PriceSource.PROMO)
    cancel = PricePoint(4, 1100, PriceKind.REGULAR, T0 - timedelta(days=1), PriceSource.MANUAL)
    assert price_timeline([c, a, b]) == [
        (a.valid_from, 1000, PriceKind.REGULAR),
        (T0, 800, PriceKind.PROMO),
        (c.valid_from, 1000, PriceKind.REGULAR),
    ]
    assert price_timeline([a, b, c, cancel]) == [
        (a.valid_from, 1000, PriceKind.REGULAR),
        (cancel.valid_from, 1100, PriceKind.REGULAR),
    ]
    same_moment = PricePoint(5, 900, PriceKind.REGULAR, a.valid_from, PriceSource.MANUAL)
    assert price_timeline([a, same_moment]) == [(a.valid_from, 900, PriceKind.REGULAR)]
    assert price_timeline([]) == []


def test_lowest_prior_price_window_edges() -> None:
    """The window is [promo_from - 30 days, promo_from): a price that ended exactly at
    the window start does not count; one starting exactly at promo_from does not."""
    start = T0 - timedelta(days=30)
    history = [
        PricePoint(1, 500, PriceKind.REGULAR, T0 - timedelta(days=40), PriceSource.IMPORT),
        PricePoint(2, 1000, PriceKind.REGULAR, start, PriceSource.IMPORT),
        PricePoint(3, 700, PriceKind.PROMO, T0, PriceSource.PROMO),
    ]
    assert lowest_prior_price(history, T0) == 1000
    exactly_at_start = [PricePoint(1, 1000, PriceKind.REGULAR, start, PriceSource.IMPORT)]
    assert lowest_prior_price(exactly_at_start, T0) == 1000
    assert lowest_prior_price([], T0) is None


def test_resolve_prior_price_prefers_history_over_manual_entry() -> None:
    price = PriceState(1000, 800, T0, T0 + timedelta(days=7), promo_prior_cents=1500)
    assert resolve_prior_price([_point(1000, 60)], price) == PriorPrice(1000, True)


@pytest.mark.parametrize(
    "price",
    [
        PriceState(1000, 800, T0, T0),  # empty period
        PriceState(1000, 800, T0, T0 - timedelta(days=1)),
        PriceState(1000),  # no promo at all
    ],
)
def test_validate_promo_invalid_period(price: PriceState) -> None:
    with pytest.raises(PromoRefused) as exc:
        validate_promo(_product(), price, [_point(1000, 60)])
    assert exc.value.reason is PromoRefusal.INVALID_PERIOD


def test_validate_promo_checks_rx_first() -> None:
    with pytest.raises(PromoRefused) as exc:
        validate_promo(_product(is_rx=True), PriceState(1000, 1200, T0, T0), [])
    assert exc.value.reason is PromoRefusal.RX_PRODUCT


def test_promo_active_boundaries() -> None:
    price = PriceState(1000, 800, T0, T0 + timedelta(days=7))
    assert not promo_active(price, T0 - timedelta(microseconds=1))
    assert promo_active(price, T0)
    assert not promo_active(price, T0 + timedelta(days=7))  # promo_to is exclusive
    assert not promo_active(PriceState(1000), T0)


def test_next_label_change_after_the_promo() -> None:
    price = PriceState(1000, 800, T0, T0 + timedelta(days=7))
    assert next_label_change(price, T0 + timedelta(days=7)) is None


def test_choose_template() -> None:
    week = PriceState(1000, 800, T0, T0 + timedelta(days=7))
    unit = _product(unit_price_required=True)
    assert choose_template(_product(), week, T0) is TemplateKey.PROMO
    assert choose_template(_product(), week, T0 - timedelta(days=1)) is TemplateKey.STANDARD
    assert choose_template(unit, PriceState(1000), T0) is TemplateKey.UNIT_PRICE
    assert choose_template(_product(is_rx=True, unit_price_required=True), week, T0) is (
        TemplateKey.UNIT_PRICE
    )


def test_effective_template_cases() -> None:
    week = PriceState(1000, 800, T0, T0 + timedelta(days=7))
    history = [_point(1000, 60)]
    unit = _product(unit_price_required=True)
    during, before = T0 + timedelta(hours=1), T0 - timedelta(days=1)
    assert effective_template(TemplateKey.STANDARD, _product(), week, history, during) == (
        TemplateChoice(TemplateKey.PROMO)
    )
    assert effective_template(TemplateKey.PROMO, unit, week, history, before) == (
        TemplateChoice(TemplateKey.UNIT_PRICE)
    )
    assert effective_template(TemplateKey.STANDARD, unit, week, history, before) == (
        TemplateChoice(TemplateKey.UNIT_PRICE)
    )
    assert effective_template(TemplateKey.STANDARD, _product(), week, history, before) == (
        TemplateChoice(TemplateKey.STANDARD)
    )
    assert effective_template(TemplateKey.PROMO, _product(is_rx=True), week, history, during) == (
        TemplateChoice(TemplateKey.STANDARD, PromoRefusal.RX_PRODUCT)
    )
    assert effective_template(TemplateKey.PROMO, _product(), week, [], during) == (
        TemplateChoice(TemplateKey.STANDARD, PromoRefusal.NO_PRIOR_PRICE)
    )


def test_history_rows_for_active_and_plain_prices() -> None:
    running = PriceState(1000, 800, T0 - timedelta(days=1), T0 + timedelta(days=2))
    rows = history_rows_for_change(running, T0, PriceSource.IMPORT)
    assert [(r.effective_cents, r.kind, r.valid_from, r.source) for r in rows] == [
        (800, PriceKind.PROMO, T0, PriceSource.IMPORT),
        (1000, PriceKind.REGULAR, T0 + timedelta(days=2), PriceSource.PROMO),
    ]
    starts_now = PriceState(1000, 800, T0, T0 + timedelta(days=2))
    assert [r.kind for r in history_rows_for_change(starts_now, T0, PriceSource.MANUAL)] == [
        PriceKind.PROMO,
        PriceKind.REGULAR,
    ]
    plain = history_rows_for_change(PriceState(1200), T0, PriceSource.IMPORT)
    assert [(r.effective_cents, r.kind, r.valid_from) for r in plain] == [
        (1200, PriceKind.REGULAR, T0)
    ]


def test_new_scheduled_promo_is_accepted_with_its_rows() -> None:
    start, end = T0 + timedelta(days=2), T0 + timedelta(days=9)
    change = apply_price_change(_product(), PriceState(1000), PriceState(1000, 800, start, end),
                                [_point(1000, 60)], T0, PriceSource.MANUAL)  # fmt: skip
    assert change.state == PriceState(1000, 800, start, end)
    assert [(r.effective_cents, r.kind, r.valid_from) for r in change.history_rows] == [
        (1000, PriceKind.REGULAR, T0),
        (800, PriceKind.PROMO, start),
        (1000, PriceKind.REGULAR, end),
    ]
    assert change.events == []


def test_first_price_of_a_new_product() -> None:
    change = apply_price_change(_product(), None, PriceState(1290), [], T0, PriceSource.IMPORT)
    assert change.state == PriceState(1290)
    assert [(r.effective_cents, r.source) for r in change.history_rows] == [
        (1290, PriceSource.IMPORT)
    ]
    assert change.events == []


def test_new_promo_without_prior_price_is_refused_and_entered_prior_rescues_it() -> None:
    requested = PriceState(1000, 800, T0, T0 + timedelta(days=7))
    with pytest.raises(PromoRefused) as exc:
        apply_price_change(_product(), PriceState(1000), requested, [_point(1000, 10)], T0,
                           PriceSource.MANUAL)  # fmt: skip
    assert exc.value.reason is PromoRefusal.NO_PRIOR_PRICE
    change = apply_price_change(_product(), PriceState(1000), requested, [_point(1000, 10)], T0,
                                PriceSource.MANUAL, entered_prior_cents=1100)  # fmt: skip
    assert change.state.promo_prior_cents == 1100
    assert change.events == [
        PriceEvent(
            "promo.prior_set",
            {
                "product_id": 1,
                "prior_cents": 1100,
                "promo_cents": 800,
                "promo_from": T0.isoformat(),
                "promo_to": (T0 + timedelta(days=7)).isoformat(),
            },
        )
    ]


def test_rx_product_cannot_get_a_promo() -> None:
    requested = PriceState(1000, 800, T0, T0 + timedelta(days=7))
    with pytest.raises(PromoRefused) as exc:
        apply_price_change(_product(is_rx=True), PriceState(1000), requested, [_point(1000, 60)],
                           T0, PriceSource.MANUAL)  # fmt: skip
    assert exc.value.reason is PromoRefusal.RX_PRODUCT


def test_unchanged_promo_keeps_its_stored_manual_prior() -> None:
    week = T0 + timedelta(days=7)
    old = PriceState(1200, 999, T0, week, promo_prior_cents=1300)
    change = apply_price_change(_product(), old, PriceState(1100, 999, T0, week),
                                [_point(1200, 10)], T0, PriceSource.IMPORT)  # fmt: skip
    assert change.state == PriceState(1100, 999, T0, week, promo_prior_cents=1300)
    assert change.events == []


def test_changed_promo_with_history_logs_the_cleared_manual_prior() -> None:
    start, end = T0 + timedelta(days=1), T0 + timedelta(days=8)
    old = PriceState(1000, 900, start, end, promo_prior_cents=1200)
    change = apply_price_change(_product(), old, PriceState(1000, 850, start, end),
                                [_point(1000, 60)], T0, PriceSource.MANUAL)  # fmt: skip
    assert change.state == PriceState(1000, 850, start, end)
    assert [e.kind for e in change.events] == ["promo.prior_cleared"]


def test_requested_prior_field_is_ignored_as_input() -> None:
    requested = PriceState(1000, 800, T0, T0 + timedelta(days=7), promo_prior_cents=5000)
    change = apply_price_change(_product(), PriceState(1000), requested, [_point(1000, 60)], T0,
                                PriceSource.MANUAL)  # fmt: skip
    assert change.state.promo_prior_cents is None


def test_unchanged_scheduled_promo_survives_a_harmless_import() -> None:
    start, end = T0 + timedelta(days=3), T0 + timedelta(days=10)
    old = PriceState(1000, 800, start, end)
    change = apply_price_change(_product(), old, PriceState(1050, 800, start, end),
                                [_point(1000, 60)], T0, PriceSource.IMPORT)  # fmt: skip
    assert change.state == PriceState(1050, 800, start, end)
    assert [r.kind for r in change.history_rows] == [
        PriceKind.REGULAR, PriceKind.PROMO, PriceKind.REGULAR,
    ]  # fmt: skip


def test_cancelled_scheduled_promo_rows_never_lower_a_prior_price() -> None:
    """A promo of 5,00 was scheduled for day 1..3 and its rows written; it was then
    cancelled on day 0 by a write. A new promo from day 10 must not see 5,00."""
    history = [
        _point(1000, 60),
        PricePoint(next(_seq), 500, PriceKind.PROMO, T0 + timedelta(days=1), PriceSource.PROMO),
        PricePoint(next(_seq), 1000, PriceKind.REGULAR, T0 + timedelta(days=3), PriceSource.PROMO),
        PricePoint(next(_seq), 1000, PriceKind.REGULAR, T0, PriceSource.MANUAL),  # cancel
    ]
    start = T0 + timedelta(days=10)
    assert lowest_prior_price(history, start) == 1000
    change = apply_price_change(_product(), PriceState(1000),
                                PriceState(1000, 900, start, start + timedelta(days=7)), history,
                                T0 + timedelta(hours=1), PriceSource.MANUAL)  # fmt: skip
    assert change.state.promo_cents == 900


def test_naive_datetimes_are_refused() -> None:
    with pytest.raises(ValueError):
        apply_price_change(_product(), None, PriceState(1000), [], datetime(2026, 12, 1),  # noqa: DTZ001
                           PriceSource.IMPORT)  # fmt: skip


def test_negative_prices_are_refused() -> None:
    with pytest.raises(ValueError):
        apply_price_change(_product(), None, PriceState(-1), [], T0, PriceSource.IMPORT)


def test_apply_product_change_returns_none_without_price_consequence() -> None:
    week = PriceState(1000, 800, T0 - timedelta(days=1), T0 + timedelta(days=7))
    expired = PriceState(1000, 800, T0 - timedelta(days=9), T0 - timedelta(days=2))
    rx = _product(is_rx=True)
    assert apply_product_change(_product(), _product(name="Άλλο"), week, [], T0) is None
    assert apply_product_change(_product(), rx, PriceState(1000), [], T0) is None
    assert apply_product_change(_product(), rx, None, [], T0) is None
    assert apply_product_change(_product(), rx, expired, [], T0) is None


def test_setting_rx_ends_scheduled_promo_and_clears_manual_prior() -> None:
    start = T0 + timedelta(days=2)
    price = PriceState(1000, 800, start, start + timedelta(days=7), promo_prior_cents=1100)
    change = apply_product_change(_product(), _product(is_rx=True), price, [], T0)
    assert change is not None
    assert change.state == PriceState(1000)
    assert [(r.effective_cents, r.kind, r.valid_from, r.source) for r in change.history_rows] == [
        (1000, PriceKind.REGULAR, T0, PriceSource.MANUAL)
    ]
    assert [e.kind for e in change.events] == ["promo.ended_rx", "promo.prior_cleared"]


def test_build_label_data_regular_templates() -> None:
    product = _product(unit_price_required=True, pack_qty_milli=200_000, pack_unit=PackUnit.GRAM)
    price = PriceState(450)
    standard = build_label_data(product, price, [], TemplateKey.STANDARD, T0)
    assert (standard.template, standard.price_text, standard.unit_price_text) == (
        TemplateKey.STANDARD, "4,50 €", None,
    )  # fmt: skip
    unit = build_label_data(product, price, [], TemplateKey.UNIT_PRICE, T0)
    assert (unit.price_text, unit.unit_price_text) == ("4,50 €", "22,50 €/kg")
    same_name = unit.name == product.name and unit.brand == product.brand
    assert same_name


def test_build_label_data_promo_fields() -> None:
    price = PriceState(1000, 800, T0, T0 + timedelta(days=7))
    label = build_label_data(_product(), price, [_point(1000, 60)], TemplateKey.PROMO, T0)
    assert (label.price_text, label.prior_price_text, label.unit_price_text) == (
        "8,00 €", "10,00 €", None,
    )  # fmt: skip
    assert label.promo_until_text == "08/12"  # T0 + 7 days is 08/12 10:00 Athens, exclusive


def test_build_label_data_errors() -> None:
    no_pack = _product(pack_qty_milli=None, pack_unit=None)
    with pytest.raises(ValueError):
        build_label_data(no_pack, PriceState(100), [], TemplateKey.UNIT_PRICE, T0)
    with pytest.raises(ValueError):
        build_label_data(_product(), PriceState(100), [], TemplateKey.UNASSIGNED, T0)
    rx_promo = PriceState(1000, 800, T0, T0 + timedelta(days=7))
    with pytest.raises(PromoRefused):
        build_label_data(_product(is_rx=True), rx_promo, [_point(1000, 60)], TemplateKey.PROMO, T0)


def test_unassigned_label_data() -> None:
    label = unassigned_label_data("A7K3", "gicisky 2.9")
    assert (label.template, label.short_code, label.tag_model) == (
        TemplateKey.UNASSIGNED, "A7K3", "gicisky 2.9",
    )  # fmt: skip

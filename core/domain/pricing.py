"""Price display rules (PLAN.md section 6). Owned by workstream B.

All functions are pure. Money is integer cents; datetimes are timezone-aware UTC.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from core.domain.types import (
    PackUnit,
    PriceKind,
    PricePoint,
    PriceSource,
    PriceState,
    ProductFacts,
    UnitBasis,
)
from core.render.label import LabelData, TemplateKey

#: Window before ``promo_from`` in which the lowest earlier price is looked up.
PRIOR_PRICE_WINDOW = timedelta(days=30)

#: Time zone in which dates are shown on labels.
DISPLAY_TZ = ZoneInfo("Europe/Athens")


class PromoRefusal(StrEnum):
    """Why a promo cannot be activated or rendered."""

    RX_PRODUCT = "rx_product"
    NO_PRIOR_PRICE = "no_prior_price"
    INVALID_PERIOD = "invalid_period"
    STARTS_IN_PAST = "starts_in_past"
    NOT_A_DISCOUNT = "not_a_discount"  # promo price not strictly below the prior price
    NOT_BELOW_REGULAR = "not_below_regular"  # promo price not strictly below regular


class PromoRefused(Exception):
    """Raised when a promo breaks a price display rule."""

    def __init__(self, reason: PromoRefusal, message: str = "") -> None:
        super().__init__(message or reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class UnitPrice:
    cents: int
    basis: UnitBasis


@dataclass(frozen=True, slots=True)
class PriorPrice:
    """The prior price for a promo and where it came from."""

    cents: int
    from_history: bool  # False when an admin entered it by hand


def format_price(cents: int) -> str:
    """Format VAT-inclusive cents for a label: ``1290`` -> ``"12,90 €"``.

    Two decimals, decimal comma, no thousands separator, a space, then the euro sign.
    Raises ``ValueError`` for negative amounts.
    """
    if cents < 0:
        raise ValueError(f"price must not be negative: {cents}")
    return f"{cents // 100},{cents % 100:02d} €"


def unit_price(price_cents: int, pack_qty_milli: int, pack_unit: PackUnit) -> UnitPrice:
    """Price per kg (for g), per litre (for ml) or per piece (for pieces).

    ``pack_qty_milli`` is the pack size in thousandths of ``pack_unit``. Integer
    arithmetic only, rounded half up to whole cents. Raises ``ValueError`` if
    ``pack_qty_milli`` is not positive.
    Examples: 450 cents for 200 g (200000) -> 2250 cents per kg; 390 cents for 4.8 g
    (4800) -> 81250 cents per kg.
    """
    if pack_qty_milli <= 0:
        raise ValueError(f"pack size must be positive: {pack_qty_milli}")
    if price_cents < 0:
        raise ValueError(f"price must not be negative: {price_cents}")
    if pack_unit is PackUnit.PIECE:
        numerator, basis = price_cents * 1_000, UnitBasis.PIECE
    else:
        # 1 kg = 1000 g and 1 l = 1000 ml; pack_qty_milli is in thousandths of a g or ml.
        numerator = price_cents * 1_000_000
        basis = UnitBasis.KILOGRAM if pack_unit is PackUnit.GRAM else UnitBasis.LITRE
    return UnitPrice(_div_round_half_up(numerator, pack_qty_milli), basis)


def format_unit_price(unit: UnitPrice) -> str:
    """Format a unit price for a label, e.g. ``"22,50 €/kg"``."""
    return f"{format_price(unit.cents)}/{_UNIT_SUFFIX[unit.basis]}"


@dataclass(frozen=True, slots=True)
class NewPricePoint:
    """A ``price_history`` row to append; the database assigns its ``seq`` (id)."""

    effective_cents: int
    kind: PriceKind
    valid_from: datetime
    source: PriceSource


@dataclass(frozen=True, slots=True)
class PriceEvent:
    """An ``event_log`` row a price change must write (kind and payload).

    Payload keys: every event has ``product_id``; ``promo.refused`` has ``reason`` (a
    ``PromoRefusal``); ``promo.prior_set`` has ``prior_cents`` and the promo's
    ``promo_cents``, ``promo_from`` and ``promo_to``.
    """

    # "promo.prior_set", "promo.prior_cleared", "promo.refused", "promo.ended_rx"
    kind: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class PriceChange:
    """Everything one price write must store, computed without I/O."""

    state: PriceState  # the new ``price`` row
    history_rows: list[NewPricePoint]  # ``price_history`` rows to append, in order
    events: list[PriceEvent]  # ``event_log`` rows to write


def apply_price_change(
    product: ProductFacts,
    old: PriceState | None,
    requested: PriceState,
    history: Sequence[PricePoint],
    now: datetime,
    source: PriceSource,
    entered_prior_cents: int | None = None,
) -> PriceChange:
    """The one entry point for every write to a ``price`` row: import, manual edit,
    promo edit. Callers store exactly what it returns, in one transaction, with the
    ``price`` row locked (see ``history_rows_for_change``). Rules, in order:

    How callers express intent: ``requested`` is the full new ``price`` row. To leave
    the promo alone (an import, or an edit of the regular price only) the caller copies
    ``old``'s promo fields into ``requested`` unchanged. Leaving them out means "end the
    promo". The promo in ``requested`` is *unchanged* when ``promo_cents``,
    ``promo_from`` and ``promo_to`` all equal ``old``'s, otherwise *changed*. A
    partial promo (some of ``promo_cents``, ``promo_from``, ``promo_to`` set, not all)
    raises ``ValueError``.

    0. Housekeeping: an unchanged promo that has expired (``promo_to <= now``) is
       dropped from ``state`` with no refusal and no ``promo.refused`` event (only
       ``promo.prior_cleared`` if it had a manual prior price, per rule 2). An expired
       promo in ``old`` counts as no promo for the rules below.
    1. Promo start and end (``promo_from`` is a timestamp, not a date):

       a. No promo in ``requested`` while ``old`` has a running or scheduled one: the
          promo ends now.
       b. A promo running at ``now`` in ``old`` (``old.promo_from <= now <
          old.promo_to``) whose ``promo_from`` stays the same may have ``promo_to`` or
          ``promo_cents`` changed. ``requested.promo_to <= now`` ends it now (it is
          removed from ``state``). Its prior price is still computed for its original
          ``promo_from``, so editing a running promo never pulls its own price into its
          30-day window. A new ``promo_cents`` must stay strictly below that original
          prior price and the current regular price, or the edit is refused.
       c. Any other changed promo is a new one: it needs ``promo_from >= now`` (a promo
          "from today" starts when it is saved: the caller passes ``promo_from = now``),
          else ``STARTS_IN_PAST``; and ``promo_to > promo_from``, else
          ``INVALID_PERIOD``.

    2. The manual prior price. ``requested.promo_prior_cents`` is ignored as input.
       ``entered_prior_cents`` is what an admin typed in this request: when given it is
       always accepted and stored, and always logged as ``promo.prior_set`` with the
       amount and the promo it belongs to, even if it equals the stored value. When not
       given, the stored ``old.promo_prior_cents`` is kept only while the promo is
       unchanged; if ``promo_cents``, ``promo_from`` or ``promo_to`` change, or the
       promo ends or is removed by any rule here (0, 1 or 3), it is cleared and
       ``promo.prior_cleared`` is logged. ``entered_prior_cents`` is checked first:
       a negative value always raises ``ValueError``, and so does a value when
       ``requested`` itself has no promo (rule 1a). Otherwise, whenever ``requested``
       has a promo but the resulting ``state`` does not (removed by rule 0, 1b or 3),
       ``entered_prior_cents`` is ignored: nothing is stored, no ``promo.prior_set``
       is logged, and the events are those of the removal (``promo.refused`` for rule
       3, plus ``promo.prior_cleared`` if a manual prior price was stored). An
       entered prior price is stored even when history covers the 30-day window;
       ``resolve_prior_price`` then uses history.
    3. Any promo left in ``state``, running or scheduled, is re-validated with
       ``validate_promo`` against ``history`` plus the rows this change appends. This
       covers changes the request did not make to the promo itself, such as an import
       that lowers ``regular_cents`` below a running promo price (``NOT_BELOW_REGULAR``)
       or lowers the 30-day low before a scheduled promo (``NOT_A_DISCOUNT``). If it is
       refused, the promo is removed from ``state`` at ``now`` and a ``promo.refused``
       event carries the reason. So a refused promo never reaches ``price_history`` as a
       shelf price. The order is safe: the history row this change writes at ``now`` is
       the regular price for a scheduled promo either way, and lies after
       ``promo_from`` (outside the window) for a running one, so validating before or
       after deciding to keep the promo gives the same result. A promo removed here
       on an import or a regular-price edit is not an error; the caller shows the
       ``promo.refused`` event to the admin.
    4. ``history_rows`` come from ``history_rows_for_change(state, now, source)``.

    Raises ``PromoRefused`` when ``requested`` itself changes the promo (rule 1b or 1c)
    in a way that is refused: ``STARTS_IN_PAST``, ``INVALID_PERIOD`` or any
    ``validate_promo`` reason. An edit is rejected, not silently dropped. An unchanged
    promo is never a reason to raise: rules 0 and 3 remove it instead. Every
    validation in this function, of the requested promo and of a scheduled one, uses
    the same history: ``history`` plus the rows this change appends, read with the
    override rule of ``PricePoint`` (so rows of a cancelled scheduled promo never lower
    a prior price).
    """
    _require_aware(now)
    requested_promo = _promo_of(requested)
    old_promo = _promo_of(old) if old is not None else None
    if entered_prior_cents is not None:
        if entered_prior_cents < 0:
            raise ValueError(f"prior price must not be negative: {entered_prior_cents}")
        if requested_promo is None:
            raise ValueError("a prior price can only be entered for a promo")
    if requested.regular_cents < 0 or (requested_promo is not None and requested_promo[0] < 0):
        raise ValueError("prices must not be negative")

    product_id = product.product_id
    stored_prior = old.promo_prior_cents if old is not None else None

    def without_promo(refusal: PromoRefusal | None) -> PriceChange:
        """The promo is gone from ``state`` (rules 0, 1a, 1b and 3)."""
        state = PriceState(requested.regular_cents)
        events: list[PriceEvent] = []
        if refusal is not None:
            payload: dict[str, object] = {"product_id": product_id, "reason": refusal}
            events.append(PriceEvent("promo.refused", payload))
        if stored_prior is not None:
            events.append(PriceEvent("promo.prior_cleared", {"product_id": product_id}))
        return PriceChange(state, history_rows_for_change(state, now, source), events)

    if requested_promo is None:  # rule 1a
        return without_promo(None)
    cents, start, end = requested_promo
    changed = requested_promo != old_promo
    if not changed:
        if end <= now:  # rule 0
            return without_promo(None)
        prior = entered_prior_cents if entered_prior_cents is not None else stored_prior
    else:
        old_start = old_promo[1] if old_promo is not None else None
        running = old_promo is not None and old_promo[1] <= now < old_promo[2]
        if running and start == old_start:  # rule 1b
            if end <= now:
                return without_promo(None)
        else:  # rule 1c
            if start < now:
                raise PromoRefused(PromoRefusal.STARTS_IN_PAST)
            if end <= start:
                raise PromoRefused(PromoRefusal.INVALID_PERIOD)
        prior = entered_prior_cents  # rule 2: a carried-over prior price is cleared

    # Rule 3, against the history this change appends (rule 4).
    state = replace(requested, promo_prior_cents=prior)
    rows = history_rows_for_change(state, now, source)
    try:
        validate_promo(product, state, _extend_history(history, rows))
    except PromoRefused as refused:
        if changed:
            raise
        return without_promo(refused.reason)

    events: list[PriceEvent] = []
    if entered_prior_cents is not None:
        payload: dict[str, object] = {
            "product_id": product_id,
            "prior_cents": entered_prior_cents,
            "promo_cents": cents,
            "promo_from": _iso_utc(start),
            "promo_to": _iso_utc(end),
        }
        events.append(PriceEvent("promo.prior_set", payload))
    elif changed and stored_prior is not None:
        events.append(PriceEvent("promo.prior_cleared", {"product_id": product_id}))
    return PriceChange(state, rows, events)


def apply_product_change(
    old: ProductFacts,
    new: ProductFacts,
    price: PriceState | None,
    history: Sequence[PricePoint],
    now: datetime,
) -> PriceChange | None:
    """Price consequences of a product edit; product edits use the same write path.

    When ``new.is_rx`` is set and ``price`` has a promo, active or scheduled, the
    promo ends at ``now``: the returned state has no promo and no manual prior price,
    history gets a ``regular`` row at ``now`` with source ``manual`` (only a person
    sets ``is_rx``), and ``promo.ended_rx`` is logged, plus ``promo.prior_cleared``
    if a manual prior price is dropped. A scheduled promo ends too: an Rx product may
    not carry one at all. Returns ``None`` when the edit changes nothing about the
    price; an already expired promo is not cleared by this function. The caller
    stores the result exactly as for ``apply_price_change``, in the same transaction
    as the product update, with the ``price`` row locked.
    """
    _require_aware(now)
    if not new.is_rx or price is None:
        return None
    promo = _promo_of(price)
    if promo is None or promo[2] <= now:
        return None
    state = PriceState(price.regular_cents)
    events = [PriceEvent("promo.ended_rx", {"product_id": new.product_id})]
    if price.promo_prior_cents is not None:
        events.append(PriceEvent("promo.prior_cleared", {"product_id": new.product_id}))
    return PriceChange(state, history_rows_for_change(state, now, PriceSource.MANUAL), events)


def next_label_change(price: PriceState, now: datetime) -> datetime | None:
    """The next moment after ``now`` at which the label for this price changes by
    itself: ``promo_from`` if it is in the future, else ``promo_to`` if in the future,
    else ``None``. The reconciler re-computes ``desired_hash`` of the bound tags at
    that moment, so a scheduled promo appears and ends on time."""
    promo = _promo_of(price)
    if promo is None:
        return None
    _, start, end = promo
    if start > now:
        return start
    if end > now:
        return end
    return None


def history_rows_for_change(
    new: PriceState, now: datetime, source: PriceSource
) -> list[NewPricePoint]:
    """The ``price_history`` rows to append after any write to a ``price`` row.

    Called only by ``apply_price_change`` and ``apply_product_change``, so every caller
    follows the same rules (see ``PricePoint`` for how rows are read).
    Returned in the order they must be inserted:

    1. At ``now``: the price in effect now under ``new`` (``promo`` kind if the promo is
       active, else ``regular``), with ``source``.
    2. If the promo starts later: a ``promo`` row at ``promo_from``, source ``promo``.
    3. If the promo ends later: a ``regular`` row at ``promo_to``, source ``promo``.

    Because later rows override the timeline from their ``valid_from`` on, row 1 also
    cancels any previously scheduled promo rows that ``new`` no longer contains.

    Concurrency: the caller must lock the ``price`` row (``SELECT ... FOR UPDATE``)
    before reading it, then update it and insert these rows in the same transaction,
    so history id order always matches the order of writes to ``price``.
    """
    promo = _promo_of(new)
    if promo is not None and promo[1] <= now < promo[2]:
        rows = [NewPricePoint(promo[0], PriceKind.PROMO, now, source)]
    else:
        rows = [NewPricePoint(new.regular_cents, PriceKind.REGULAR, now, source)]
    if promo is not None:
        cents, start, end = promo
        if start > now:
            rows.append(NewPricePoint(cents, PriceKind.PROMO, start, PriceSource.PROMO))
        if end > now:
            rows.append(NewPricePoint(new.regular_cents, PriceKind.REGULAR, end, PriceSource.PROMO))
    return rows


def price_timeline(
    history: Sequence[PricePoint],
) -> list[tuple[datetime, int, PriceKind]]:
    """Resolve history into ``(start, effective_cents, kind)`` segments, sorted by
    start, applying the override rule in ``PricePoint``. Each segment runs until the
    next one starts; the last one is open-ended. Adjacent equal prices are kept."""
    segments: list[tuple[datetime, int, PriceKind]] = []
    for point in sorted(history, key=lambda p: p.seq):
        # The row overrides everything from its valid_from on. Segments stay sorted by
        # start, so the overridden ones are all at the end.
        while segments and segments[-1][0] >= point.valid_from:
            segments.pop()
        segments.append((point.valid_from, point.effective_cents, point.kind))
    return segments


def lowest_prior_price(history: Sequence[PricePoint], promo_from: datetime) -> int | None:
    """Lowest price in effect during the 30 days before ``promo_from``.

    The window is ``[promo_from - 30 days, promo_from)``, evaluated on
    ``price_timeline(history)``. A price counts if it was in effect at any moment of the
    window, including the price already in effect when the window opens; both regular
    and earlier promo prices count. Returns ``None`` when the timeline starts after the
    window start, because then the lowest price of the previous 30 days is not known.
    """
    timeline = price_timeline(history)
    window_start = promo_from - PRIOR_PRICE_WINDOW
    if not timeline or timeline[0][0] > window_start:
        return None
    lowest: int | None = None
    for index, (start, cents, _) in enumerate(timeline):
        if start >= promo_from:
            break
        if index + 1 < len(timeline) and timeline[index + 1][0] <= window_start:
            continue  # ended before the window opened
        lowest = cents if lowest is None else min(lowest, cents)
    return lowest


def resolve_prior_price(history: Sequence[PricePoint], price: PriceState) -> PriorPrice:
    """The prior price to show on a promo label.

    Uses ``lowest_prior_price`` when history covers the window; otherwise the
    admin-entered ``price.promo_prior_cents``. Raises ``PromoRefused(NO_PRIOR_PRICE)``
    when neither exists. A promo label is never rendered without a prior price.
    """
    if price.promo_from is None:
        raise ValueError("price has no promo")
    lowest = lowest_prior_price(history, price.promo_from)
    if lowest is not None:
        return PriorPrice(lowest, from_history=True)
    if price.promo_prior_cents is not None:
        return PriorPrice(price.promo_prior_cents, from_history=False)
    raise PromoRefused(PromoRefusal.NO_PRIOR_PRICE)


def validate_promo(
    product: ProductFacts, price: PriceState, history: Sequence[PricePoint]
) -> PriorPrice:
    """Check that ``price``'s promo may be activated, and return its prior price.

    Refuses (raises ``PromoRefused``) when the product is prescription-only, the
    period is missing or ``promo_to <= promo_from``, there is no prior price, the promo
    price is not strictly below the prior price (``NOT_A_DISCOUNT``), or it is not
    strictly below ``price.regular_cents`` (``NOT_BELOW_REGULAR``); docs/decisions/0009.
    Checked in that order, so the first failing rule is the reason given. The regular
    check matters when a manual prior price is higher than the current regular price.

    The prior price is the lowest price in effect in the 30 days before the moment the
    promo takes effect (``promo_from``), up to that moment, so a price drop earlier the
    same day counts.
    """
    if product.is_rx:
        raise PromoRefused(PromoRefusal.RX_PRODUCT)
    cents, start, end = price.promo_cents, price.promo_from, price.promo_to
    if cents is None or start is None or end is None or end <= start:
        raise PromoRefused(PromoRefusal.INVALID_PERIOD)
    prior = resolve_prior_price(history, price)
    if cents >= prior.cents:
        raise PromoRefused(PromoRefusal.NOT_A_DISCOUNT)
    if cents >= price.regular_cents:
        raise PromoRefused(PromoRefusal.NOT_BELOW_REGULAR)
    return prior


def promo_active(price: PriceState, now: datetime) -> bool:
    """True when a promo is set and ``promo_from <= now < promo_to`` (``promo_to`` is
    exclusive)."""
    if price.promo_cents is None or price.promo_from is None or price.promo_to is None:
        return False
    return price.promo_from <= now < price.promo_to


def choose_template(product: ProductFacts, price: PriceState, now: datetime) -> TemplateKey:
    """Default template offered when binding: ``promo`` if a promo is active (never for
    Rx), else ``unit_price`` if ``unit_price_required``, else ``standard``."""
    if not product.is_rx and promo_active(price, now):
        return TemplateKey.PROMO
    return _regular_template(product)


@dataclass(frozen=True, slots=True)
class TemplateChoice:
    """The template a tag shows now, and why a promo is not shown if it is refused."""

    template: TemplateKey
    promo_refused: PromoRefusal | None = None


def effective_template(
    bound: TemplateKey,
    product: ProductFacts,
    price: PriceState,
    history: Sequence[PricePoint],
    now: datetime,
) -> TemplateChoice:
    """The template to render for a tag bound with template ``bound``, at ``now``.

    - A promo is active and ``validate_promo`` accepts it: ``promo``.
    - A promo is active but refused: the regular template (``unit_price`` if
      ``unit_price_required``, else ``standard``) showing ``price.regular_cents``, with
      ``promo_refused`` set. The tag never shows an unlawful promo; the caller logs
      ``promo.refused`` once per promo and monitoring lists it. ``apply_price_change``
      removes promos that become refused before they start, so this is a safety net:
      reaching it means a write bypassed ``apply_price_change``.
    - No promo active and ``bound`` is ``promo``: the regular template, no refusal.
    - Otherwise ``bound``; ``unit_price`` wins over ``standard`` when required.
    """
    regular = _regular_template(product)
    if promo_active(price, now):
        try:
            validate_promo(product, price, history)
        except PromoRefused as refused:
            return TemplateChoice(regular, refused.reason)
        return TemplateChoice(TemplateKey.PROMO)
    if bound is TemplateKey.PROMO:
        return TemplateChoice(regular)
    if bound is TemplateKey.STANDARD and product.unit_price_required:
        return TemplateChoice(TemplateKey.UNIT_PRICE)
    return TemplateChoice(bound)


def build_label_data(
    product: ProductFacts,
    price: PriceState,
    history: Sequence[PricePoint],
    template: TemplateKey,
    now: datetime,
) -> LabelData:
    """Everything a label shows, already formatted.

    For ``promo`` this calls ``validate_promo`` and so raises ``PromoRefused`` rather
    than build a promo label without a prior price. For ``unit_price`` it raises
    ``ValueError`` if pack size or unit is missing.

    Unit price: set ``unit_price_text`` for ``unit_price`` and also for ``promo`` when
    ``product.unit_price_required``. On a promo label it is computed from the promo
    price, and the template draws it under the same size rule as ``unit_price``.

    ``promo_until_text`` is the last day the promo is in effect, as ``DD/MM`` in
    ``DISPLAY_TZ``: the date of ``promo_to - 1 microsecond``, because ``promo_to`` is
    exclusive. A promo through 30/11 inclusive has ``promo_to`` = 01/12 00:00 Athens
    time and shows ``"30/11"``. Wording around it ("έως") belongs to the template.
    Not for ``unassigned``; use ``unassigned_label_data``.
    """
    if template is TemplateKey.UNASSIGNED:
        raise ValueError("use unassigned_label_data for the unassigned template")
    if template is TemplateKey.STANDARD:
        return LabelData(
            template,
            name=product.name,
            brand=product.brand,
            price_text=format_price(price.regular_cents),
        )
    if template is TemplateKey.UNIT_PRICE:
        return LabelData(
            template,
            name=product.name,
            brand=product.brand,
            price_text=format_price(price.regular_cents),
            unit_price_text=_unit_price_text(product, price.regular_cents),
        )
    prior = validate_promo(product, price, history)
    cents, end = price.promo_cents, price.promo_to
    if cents is None or end is None:  # validate_promo has checked; this narrows the types
        raise PromoRefused(PromoRefusal.INVALID_PERIOD)
    last_day = (end - timedelta(microseconds=1)).astimezone(DISPLAY_TZ)
    return LabelData(
        template,
        name=product.name,
        brand=product.brand,
        price_text=format_price(cents),
        unit_price_text=_unit_price_text(product, cents) if product.unit_price_required else None,
        prior_price_text=format_price(prior.cents),
        promo_until_text=last_day.strftime("%d/%m"),
    )


def unassigned_label_data(short_code: str, tag_model: str) -> LabelData:
    """Label for an unbound tag: its ``short_code`` in large type and the tag model."""
    return LabelData(TemplateKey.UNASSIGNED, short_code=short_code, tag_model=tag_model)


# --- private helpers -------------------------------------------------------------

#: Label suffix after the euro sign for each unit basis ("22,50 €/kg").
_UNIT_SUFFIX: dict[UnitBasis, str] = {
    UnitBasis.KILOGRAM: "kg",
    UnitBasis.LITRE: "l",
    UnitBasis.PIECE: "τεμ.",
}


def _div_round_half_up(numerator: int, denominator: int) -> int:
    """``numerator / denominator`` rounded half up, for non-negative integers."""
    return (2 * numerator + denominator) // (2 * denominator)


def _require_aware(when: datetime) -> None:
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("datetimes must be timezone-aware")


def _iso_utc(when: datetime) -> str:
    return when.astimezone(UTC).isoformat()


def _promo_of(state: PriceState) -> tuple[int, datetime, datetime] | None:
    """``(promo_cents, promo_from, promo_to)``, ``None`` without a promo.

    Raises ``ValueError`` for a partial promo."""
    cents, start, end = state.promo_cents, state.promo_from, state.promo_to
    if cents is None and start is None and end is None:
        return None
    if cents is None or start is None or end is None:
        raise ValueError("a promo needs promo_cents, promo_from and promo_to together")
    _require_aware(start)
    _require_aware(end)
    return cents, start, end


def _extend_history(
    history: Sequence[PricePoint], rows: Sequence[NewPricePoint]
) -> list[PricePoint]:
    """``history`` plus ``rows`` as if appended now, with ``seq`` after every row."""
    seq = max((point.seq for point in history), default=0)
    extended = list(history)
    for offset, row in enumerate(rows, start=1):
        extended.append(
            PricePoint(seq + offset, row.effective_cents, row.kind, row.valid_from, row.source)
        )
    return extended


def _regular_template(product: ProductFacts) -> TemplateKey:
    return TemplateKey.UNIT_PRICE if product.unit_price_required else TemplateKey.STANDARD


def _unit_price_text(product: ProductFacts, price_cents: int) -> str:
    if product.pack_qty_milli is None or product.pack_unit is None:
        raise ValueError(f"product {product.product_id} has no pack size or unit")
    return format_unit_price(unit_price(price_cents, product.pack_qty_milli, product.pack_unit))

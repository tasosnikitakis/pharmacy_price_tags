"""Price display rules (PLAN.md section 6). Owned by workstream B.

All functions are pure. Money is integer cents; datetimes are timezone-aware UTC.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    raise NotImplementedError


def unit_price(price_cents: int, pack_qty_milli: int, pack_unit: PackUnit) -> UnitPrice:
    """Price per kg (for g), per litre (for ml) or per piece (for pieces).

    ``pack_qty_milli`` is the pack size in thousandths of ``pack_unit``. Integer
    arithmetic only, rounded half up to whole cents. Raises ``ValueError`` if
    ``pack_qty_milli`` is not positive.
    Examples: 450 cents for 200 g (200000) -> 2250 cents per kg; 390 cents for 4.8 g
    (4800) -> 81250 cents per kg.
    """
    raise NotImplementedError


def format_unit_price(unit: UnitPrice) -> str:
    """Format a unit price for a label, e.g. ``"22,50 €/kg"``."""
    raise NotImplementedError


@dataclass(frozen=True, slots=True)
class NewPricePoint:
    """A ``price_history`` row to append; the database assigns its ``seq`` (id)."""

    effective_cents: int
    kind: PriceKind
    valid_from: datetime
    source: PriceSource


@dataclass(frozen=True, slots=True)
class PriceEvent:
    """An ``event_log`` row a price change must write (kind and payload)."""

    kind: str  # "promo.prior_set", "promo.prior_cleared", "promo.refused"
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

    1. ``promo_from`` is a timestamp, not a date. A promo added or changed by this
       request must have ``promo_from >= now`` (a promo "from today" starts when it is
       saved: the caller passes ``promo_from = now``); otherwise ``STARTS_IN_PAST``.
    2. The manual prior price. ``requested.promo_prior_cents`` is ignored as input.
       ``entered_prior_cents`` is what an admin typed in this request: when given it is
       always accepted and stored, and always logged as ``promo.prior_set`` with the
       amount and the promo it belongs to, even if it equals the stored value. When not
       given, the stored ``old.promo_prior_cents`` is kept only while the promo is
       unchanged; if ``promo_cents``, ``promo_from`` or ``promo_to`` change, or the
       promo ends, it is cleared and ``promo.prior_cleared`` is logged.
    3. A promo that has not started yet is re-validated with ``validate_promo`` against
       ``history`` plus the rows this change appends. If it would now be refused (say an
       import lowered the price inside its 30-day window below the promo price), the
       promo is removed from ``state`` and a ``promo.refused`` event carries the reason.
       So a refused promo never reaches ``price_history`` as a shelf price.
    4. ``history_rows`` come from ``history_rows_for_change(state, now, source)``.

    Raises ``PromoRefused`` when ``requested`` itself adds or changes a promo that
    ``validate_promo`` refuses, or one that starts in the past: an edit is rejected,
    not silently dropped.
    """
    raise NotImplementedError


def apply_product_change(
    old: ProductFacts,
    new: ProductFacts,
    price: PriceState | None,
    history: Sequence[PricePoint],
    now: datetime,
) -> PriceChange | None:
    """Price consequences of a product edit; product edits use the same write path.

    When ``new.is_rx`` is set and ``price`` has a promo, active or scheduled, the
    promo ends at ``now``: the returned state has no promo (and no manual prior price),
    history gets a row at ``now`` with the regular price, and ``promo.ended_rx`` is
    logged. Returns ``None`` when the edit changes nothing about the price. The caller
    stores the result exactly as for ``apply_price_change``, in the same transaction
    as the product update, with the ``price`` row locked.
    """
    raise NotImplementedError


def next_label_change(price: PriceState, now: datetime) -> datetime | None:
    """The next moment after ``now`` at which the label for this price changes by
    itself: ``promo_from`` if it is in the future, else ``promo_to`` if in the future,
    else ``None``. The reconciler re-computes ``desired_hash`` of the bound tags at
    that moment, so a scheduled promo appears and ends on time."""
    raise NotImplementedError


def history_rows_for_change(
    new: PriceState, now: datetime, source: PriceSource
) -> list[NewPricePoint]:
    """The ``price_history`` rows to append after any write to a ``price`` row.

    Called only by ``apply_price_change``, so every caller follows the same rules (see
    ``PricePoint`` for how rows are read).
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
    raise NotImplementedError


def price_timeline(
    history: Sequence[PricePoint],
) -> list[tuple[datetime, int, PriceKind]]:
    """Resolve history into ``(start, effective_cents, kind)`` segments, sorted by
    start, applying the override rule in ``PricePoint``. Each segment runs until the
    next one starts; the last one is open-ended. Adjacent equal prices are kept."""
    raise NotImplementedError


def lowest_prior_price(history: Sequence[PricePoint], promo_from: datetime) -> int | None:
    """Lowest price in effect during the 30 days before ``promo_from``.

    The window is ``[promo_from - 30 days, promo_from)``, evaluated on
    ``price_timeline(history)``. A price counts if it was in effect at any moment of the
    window, including the price already in effect when the window opens; both regular
    and earlier promo prices count. Returns ``None`` when the timeline starts after the
    window start, because then the lowest price of the previous 30 days is not known.
    """
    raise NotImplementedError


def resolve_prior_price(history: Sequence[PricePoint], price: PriceState) -> PriorPrice:
    """The prior price to show on a promo label.

    Uses ``lowest_prior_price`` when history covers the window; otherwise the
    admin-entered ``price.promo_prior_cents``. Raises ``PromoRefused(NO_PRIOR_PRICE)``
    when neither exists. A promo label is never rendered without a prior price.
    """
    raise NotImplementedError


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
    raise NotImplementedError


def promo_active(price: PriceState, now: datetime) -> bool:
    """True when a promo is set and ``promo_from <= now < promo_to`` (``promo_to`` is
    exclusive)."""
    raise NotImplementedError


def choose_template(product: ProductFacts, price: PriceState, now: datetime) -> TemplateKey:
    """Default template offered when binding: ``promo`` if a promo is active (never for
    Rx), else ``unit_price`` if ``unit_price_required``, else ``standard``."""
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


def unassigned_label_data(short_code: str, tag_model: str) -> LabelData:
    """Label for an unbound tag: its ``short_code`` in large type and the tag model."""
    raise NotImplementedError

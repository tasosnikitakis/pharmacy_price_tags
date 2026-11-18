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
    NOT_A_DISCOUNT = "not_a_discount"


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


def unit_price(price_cents: int, pack_qty: int, pack_unit: PackUnit) -> UnitPrice:
    """Price per kg (for g), per litre (for ml) or per piece (for pieces).

    Rounded half up to whole cents. Raises ``ValueError`` if ``pack_qty`` is not positive.
    Example: 450 cents for 200 g -> 2250 cents per kg.
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


def history_rows_for_change(
    new: PriceState, now: datetime, source: PriceSource
) -> list[NewPricePoint]:
    """The ``price_history`` rows to append after any write to a ``price`` row.

    The single writer of history, used by imports, manual edits and promo edits alike,
    so every caller follows the same rules (see ``PricePoint`` for how rows are read).
    Returned in the order they must be inserted:

    1. At ``now``: the price in effect now under ``new`` (``promo`` kind if the promo is
       active, else ``regular``), with ``source``.
    2. If the promo starts later: a ``promo`` row at ``promo_from``, source ``promo``.
    3. If the promo ends later: a ``regular`` row at ``promo_to``, source ``promo``.

    Because later rows override the timeline from their ``valid_from`` on, row 1 also
    cancels any previously scheduled promo rows that ``new`` no longer contains.
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
    period is missing or ``promo_to <= promo_from``, there is no prior price, or the
    promo price is not below the prior price (``NOT_A_DISCOUNT``, docs/decisions/0009).
    """
    raise NotImplementedError


def promo_active(price: PriceState, now: datetime) -> bool:
    """True when a promo is set and ``promo_from <= now < promo_to`` (``promo_to`` is
    exclusive)."""
    raise NotImplementedError


def choose_template(product: ProductFacts, price: PriceState, now: datetime) -> TemplateKey:
    """Default template for a product: ``promo`` if a promo is active (never for Rx),
    else ``unit_price`` if ``unit_price_required``, else ``standard``."""
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

"""Price display rules (PLAN.md section 6). Owned by workstream B.

All functions are pure. Money is integer cents; datetimes are timezone-aware UTC.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from core.domain.types import PackUnit, PricePoint, PriceState, ProductFacts, UnitBasis
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


def lowest_prior_price(history: Sequence[PricePoint], promo_from: datetime) -> int | None:
    """Lowest price in effect during the 30 days before ``promo_from``.

    The window is ``[promo_from - 30 days, promo_from)``. A price counts if it was in
    effect at any moment of the window, including the price already in effect when
    the window opens. Returns ``None`` when the history does not cover the whole
    window (the earliest entry is later than the window start), because then the
    lowest price of the previous 30 days is not known.
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
    period is missing or ``promo_to <= promo_from``, the promo price is not below the
    prior price, or there is no prior price.
    """
    raise NotImplementedError


def promo_active(price: PriceState, now: datetime) -> bool:
    """True when a promo is set and ``promo_from <= now < promo_to``."""
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
    ``ValueError`` if pack size or unit is missing. Dates are shown in ``DISPLAY_TZ``.
    Not for ``unassigned``; use ``unassigned_label_data``.
    """
    raise NotImplementedError


def unassigned_label_data(short_code: str, tag_model: str) -> LabelData:
    """Label for an unbound tag: its ``short_code`` in large type and the tag model."""
    raise NotImplementedError

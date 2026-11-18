"""Plain value types the domain rules work on.

They mirror the database rows the rules need, without depending on SQLAlchemy.
Money is integer cents; datetimes are timezone-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class PackUnit(StrEnum):
    GRAM = "g"
    MILLILITRE = "ml"
    PIECE = "pieces"


class UnitBasis(StrEnum):
    """What a unit price is quoted per."""

    KILOGRAM = "kg"
    LITRE = "l"
    PIECE = "piece"


class PriceKind(StrEnum):
    """``price_history.kind``: which price became effective."""

    REGULAR = "regular"
    PROMO = "promo"


class PriceSource(StrEnum):
    """``price_history.source``: what caused the change."""

    IMPORT = "import"
    MANUAL = "manual"
    PROMO = "promo"


@dataclass(frozen=True, slots=True)
class ProductFacts:
    """The product fields the price and label rules need."""

    product_id: int
    ean: str
    name: str
    brand: str | None
    pack_qty_milli: int | None  # thousandths of pack_unit: 4.8 g -> 4800
    pack_unit: PackUnit | None
    unit_price_required: bool
    is_rx: bool


@dataclass(frozen=True, slots=True)
class PriceState:
    """Current price of one product (the ``price`` row).

    A promo is either fully set (``promo_cents``, ``promo_from``, ``promo_to``) or
    fully absent; ``promo_to`` is exclusive. ``promo_prior_cents`` is the prior price
    an admin entered by hand for this promo, used only when there is less than 30 days
    of history. ``core.domain.pricing.apply_price_change`` owns it: a prior price an
    admin enters in a request is always stored and logged (``promo.prior_set``), even if
    unchanged; one carried over from before is cleared when the promo ends or a promo
    field changes, unless the same request enters one. See rule 2 there.
    """

    regular_cents: int
    promo_cents: int | None = None
    promo_from: datetime | None = None
    promo_to: datetime | None = None
    promo_prior_cents: int | None = None


@dataclass(frozen=True, slots=True)
class PricePoint:
    """One ``price_history`` row.

    Meaning (docs/decisions/0008): the row says "from ``valid_from`` on, the shelf
    price is ``effective_cents``". Rows are applied in ``seq`` order (the row id), and
    each row overrides the timeline from its ``valid_from`` onward, replacing anything
    earlier rows said about that period. So the price in effect at time ``t`` comes
    from the row with the highest ``seq`` among rows whose ``valid_from <= t`` and that
    no later row has overridden at ``t``. ``valid_from`` may be in the future (a
    scheduled promo start or end). Both kinds count towards the 30-day lowest price.

    Rows are only ever produced by ``core.domain.pricing.apply_price_change`` and
    ``core.domain.pricing.apply_product_change``.
    """

    seq: int
    effective_cents: int
    kind: PriceKind
    valid_from: datetime
    source: PriceSource

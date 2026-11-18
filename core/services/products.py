"""Creating a product by hand, from the phone binding flow (PLAN.md section 9, step 1:
"Unknown EAN: offer to create the product").

Imports create most products (``core.services.catalogue.apply_import``); this is the
only other way in. It follows the import's rules and lock order (decisions 0011, 0014):

- The EAN is checked with ``core.domain.catalogue.normalise_ean``, the name with the
  import's text rules, and the price is integer cents above zero.
- The ``store`` row is locked first, ``FOR NO KEY UPDATE``, exactly as an import locks
  it. Product creation is therefore serialised with imports: a create that starts
  while an import is open waits for it to commit and then finds the EAN the import may
  have added, and an import that starts while a create is open waits for it and then
  sees the new product in its diff (as matched, never as a second new row). No other
  row is locked before the store row, so the create cannot be part of a lock cycle
  with an import or a bind (a bind never locks the store).
- The price and its first history row go through ``apply_price_change`` with source
  ``manual``, in the same savepoint as the product row and the ``product.created``
  event. The caller commits.

A new product has no tags, so no label is refreshed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.db.models import (
    DEFAULT_STORE_ID,
    EventLog,
    Price,
    PriceHistory,
    Product,
    Store,
)
from core.domain import pricing
from core.domain.catalogue import (
    NAME_MAX_LEN,
    PACK_QTY_MILLI_MAX,
    PRICE_CENTS_MAX,
    normalise_ean,
    normalise_text,
)
from core.domain.types import PackUnit, PriceKind, PriceSource, PriceState
from core.services.catalogue import product_facts


class ProductCreateError(ValueError):
    """The new product is refused. ``code`` is a machine-readable error code; the UI
    shows its message (decision 0015)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class ProductExists(ProductCreateError):
    """A product with this EAN exists already (perhaps created meanwhile by an import)."""

    def __init__(self, product_id: int) -> None:
        super().__init__("product.exists", str(product_id))
        self.product_id = product_id


@dataclass(frozen=True, slots=True)
class NewProduct:
    """What the binding flow asks for. ``ean`` is as typed or scanned; the rest is
    already parsed (cents and thousandths, never floats)."""

    ean: str
    name: str
    regular_cents: int
    pack_qty_milli: int | None = None
    pack_unit: PackUnit | None = None
    unit_price_required: bool = False
    is_rx: bool = False


def check_new_product(new: NewProduct) -> tuple[str, str]:
    """The normalised EAN and name, or ``ProductCreateError``. The EAN's own reason
    (``CellError`` from ``normalise_ean``) is raised as is, so the caller can show it."""
    ean = normalise_ean(new.ean)
    name = normalise_text(new.name)
    if not name:
        raise ProductCreateError("product.name_empty")
    if len(name) > NAME_MAX_LEN:
        raise ProductCreateError("product.name_too_long")
    cents = new.regular_cents
    if isinstance(cents, bool) or not isinstance(cents, int) or cents < 0:
        raise ProductCreateError("price.invalid")
    if cents == 0:
        raise ProductCreateError("price.zero")
    if cents > PRICE_CENTS_MAX:
        raise ProductCreateError("price.too_large")
    if (new.pack_qty_milli is None) != (new.pack_unit is None):
        raise ProductCreateError("product.pack_invalid")
    if new.pack_qty_milli is not None and not 0 < new.pack_qty_milli <= PACK_QTY_MILLI_MAX:
        raise ProductCreateError("product.pack_invalid")
    if new.unit_price_required and new.pack_qty_milli is None:
        raise ProductCreateError("product.unit_price_needs_pack")
    return ean, name


def find_by_ean(session: Session, ean: str, store_id: int = DEFAULT_STORE_ID) -> Product | None:
    return session.scalars(
        select(Product).where(Product.store_id == store_id, Product.ean == ean)
    ).one_or_none()


def create_product(
    session: Session,
    new: NewProduct,
    *,
    actor: str,
    now: datetime,
    store_id: int = DEFAULT_STORE_ID,
) -> Product:
    """Create a product with its price and first history row, in one savepoint.

    Raises ``CellError`` for an invalid EAN, ``ProductCreateError`` for other invalid
    input and ``ProductExists`` when the EAN is taken. Logs ``product.created`` (and
    whatever events ``apply_price_change`` returns). The product is marked as manually
    changed, so the next import preview shows it if the ERP disagrees.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    ean, name = check_new_product(new)
    with session.begin_nested():
        # Lock order (decisions 0011, 0014): the store row first, FOR NO KEY UPDATE, as
        # an import does. This serialises the create with imports and other creates.
        session.execute(
            select(Store.id).where(Store.id == store_id).with_for_update(key_share=True)
        )
        existing = find_by_ean(session, ean, store_id)
        if existing is not None:
            raise ProductExists(existing.id)
        product = Product(
            store_id=store_id,
            ean=ean,
            name=name,
            brand=None,
            pack_qty_milli=new.pack_qty_milli,
            pack_unit=new.pack_unit.value if new.pack_unit is not None else None,
            unit_price_required=new.unit_price_required,
            is_rx=new.is_rx,
            active=True,
            manually_changed_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(product)
        session.flush()
        change = pricing.apply_price_change(
            product_facts(product),
            None,
            PriceState(regular_cents=new.regular_cents),
            [],
            now,
            PriceSource.MANUAL,
        )
        state = change.state
        session.add(
            Price(
                product_id=product.id,
                store_id=store_id,
                regular_cents=state.regular_cents,
                promo_cents=state.promo_cents,
                promo_from=state.promo_from,
                promo_to=state.promo_to,
                promo_prior_cents=state.promo_prior_cents,
                updated_at=now,
            )
        )
        session.add_all(
            PriceHistory(
                store_id=store_id,
                product_id=product.id,
                effective_cents=point.effective_cents,
                kind=PriceKind(point.kind).value,
                valid_from=point.valid_from,
                source=PriceSource(point.source).value,
                created_at=now,
            )
            for point in change.history_rows
        )
        for event in change.events:
            _log(session, event.kind, event.payload, actor, now, store_id)
        _log(
            session,
            "product.created",
            {
                "product_id": product.id,
                "ean": ean,
                "name": name,
                "regular_cents": new.regular_cents,
                "pack_qty_milli": new.pack_qty_milli,
                "pack_unit": new.pack_unit.value if new.pack_unit is not None else None,
                "unit_price_required": new.unit_price_required,
                "is_rx": new.is_rx,
            },
            actor,
            now,
            store_id,
        )
        session.flush()
    return product


def _log(
    session: Session,
    kind: str,
    payload: Mapping[str, object],
    actor: str,
    now: datetime,
    store_id: int,
) -> None:
    stored = _jsonable(payload)
    session.add(EventLog(store_id=store_id, at=now, actor=actor, kind=kind, payload=stored))


def _jsonable(value: Any) -> Any:
    """An event payload as JSONB values (datetimes as ISO 8601 UTC), as the catalogue
    stores them."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value

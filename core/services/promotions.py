"""Promo edits from the Products page (PLAN.md sections 6 and 9, decisions 0008, 0009).

``edit_promo`` is the promo counterpart of ``core.services.catalogue.edit_price`` and
follows the same write path: in one savepoint it locks the product, then its ``price``
row, both ``FOR NO KEY UPDATE``; calls ``core.domain.pricing.apply_price_change`` with
the admin's entered prior price; stores exactly what that returns (``price`` row,
``price_history`` rows, ``event_log`` rows); logs ``promo.edited``; and refreshes the
labels of the product's bound tags with the same ``now`` (decision 0014). The caller
commits.

Refusals come from the pricing rules as ``PromoRefused`` (with a ``PromoRefusal``
reason) and are re-raised unchanged; nothing is written then.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from core.db.models import DEFAULT_STORE_ID
from core.domain import pricing
from core.domain.catalogue import PRICE_CENTS_MAX
from core.domain.pricing import PriceChange, PromoRefusal, PromoRefused
from core.domain.types import PriceSource, PriceState
from core.services.catalogue import (
    _check_now,
    _lock_price,
    _lock_product,
    _log,
    _refresh_labels,
    _store_change,
    load_history,
    price_state,
    product_facts,
)
from core.services.tags import LabelPipeline


class NoPrice(ValueError):
    """The product has no price row yet, so it cannot get a promo."""


@dataclass(frozen=True, slots=True)
class PromoInput:
    """The promo an admin asks for. ``promo_to`` is exclusive (decision 0008)."""

    promo_cents: int
    promo_from: datetime
    promo_to: datetime


@dataclass(frozen=True, slots=True)
class PromoEdit:
    """What an edit did. ``change`` is ``None`` when nothing was written."""

    change: PriceChange | None

    @property
    def prior_saved(self) -> bool:
        """True only when the entered prior price was stored (``promo.prior_set``);
        ``apply_price_change`` ignores it when the same edit removes the promo."""
        return self.change is not None and any(
            e.kind == "promo.prior_set" for e in self.change.events
        )

    @property
    def promo_removed(self) -> bool:
        return self.change is not None and self.change.state.promo_cents is None


def _check_cents(cents: int, what: str) -> None:
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise TypeError(f"{what} must be an integer number of cents")
    if cents <= 0:
        raise ValueError(f"{what} must be above zero")
    if cents > PRICE_CENTS_MAX:
        raise ValueError(f"{what} must be at most {PRICE_CENTS_MAX}")


def edit_promo(
    session: Session,
    product_id: int,
    promo: PromoInput | None,
    *,
    entered_prior_cents: int | None = None,
    actor: str,
    now: datetime | None = None,
    store_id: int = DEFAULT_STORE_ID,
    pipeline: LabelPipeline | None = None,
) -> PromoEdit:
    """Set, change or (with ``promo=None``) end a product's promo.

    ``entered_prior_cents`` is the prior price the admin typed in this request, if any
    (section 6: needed when history covers less than 30 days). It is passed to
    ``apply_price_change`` only with a promo; when ending a promo it is dropped.

    Raises ``PromoRefused`` (for example ``RX_PRODUCT``, ``STARTS_IN_PAST``,
    ``NO_PRIOR_PRICE``, ``NOT_A_DISCOUNT``), ``NoPrice``, ``NotFound`` for an unknown
    product, and ``ValueError`` for amounts out of range. Writes nothing and returns
    ``PromoEdit(None)`` when the promo is already as asked and no prior was entered.
    """
    if promo is not None:
        _check_cents(promo.promo_cents, "promo_cents")
    if entered_prior_cents is not None:
        _check_cents(entered_prior_cents, "entered_prior_cents")
    if promo is None:
        entered_prior_cents = None
    now = _check_now(now)
    with session.begin_nested():
        # Product before price, as in every writer (decision 0011 and 0014).
        product = _lock_product(session, product_id, store_id)
        price = _lock_price(session, product_id, store_id)
        if price is None:
            raise NoPrice(f"product {product_id} has no price")
        facts = product_facts(product)
        if promo is not None and facts.is_rx:
            raise PromoRefused(PromoRefusal.RX_PRODUCT)
        old = price_state(price)
        if promo is None:
            requested = PriceState(regular_cents=old.regular_cents)
        else:
            requested = replace(
                old,
                promo_cents=promo.promo_cents,
                promo_from=promo.promo_from,
                promo_to=promo.promo_to,
            )
        same = (requested.promo_cents, requested.promo_from, requested.promo_to) == (
            old.promo_cents,
            old.promo_from,
            old.promo_to,
        )
        if same and entered_prior_cents is None:
            return PromoEdit(None)
        history = load_history(session, [product_id])[product_id]
        change = pricing.apply_price_change(
            facts, old, requested, history, now, PriceSource.MANUAL, entered_prior_cents
        )
        _store_change(session, product, price, change, actor=actor, now=now)
        _log(
            session,
            "promo.edited",
            {
                "product_id": product.id,
                "ean": product.ean,
                "old": _promo_payload(old),
                "new": _promo_payload(change.state),
            },
            actor,
            now,
            store_id,
        )
        session.flush()
        _refresh_labels(session, pipeline, now, [product.id])
    return PromoEdit(change)


def end_promo(
    session: Session,
    product_id: int,
    *,
    actor: str,
    now: datetime | None = None,
    store_id: int = DEFAULT_STORE_ID,
    pipeline: LabelPipeline | None = None,
) -> PromoEdit:
    """End the product's running or scheduled promo now."""
    return edit_promo(
        session, product_id, None, actor=actor, now=now, store_id=store_id, pipeline=pipeline
    )


def _promo_payload(state: PriceState) -> dict[str, object] | None:
    if state.promo_cents is None:
        return None
    return {
        "promo_cents": state.promo_cents,
        "promo_from": state.promo_from,
        "promo_to": state.promo_to,
        "promo_prior_cents": state.promo_prior_cents,
    }


# --- Dates from the form ---------------------------------------------------------------


def local_midnight(day: date, tz: ZoneInfo) -> datetime:
    """``day`` at 00:00 in ``tz``, as an aware datetime."""
    return datetime.combine(day, time(0, 0), tzinfo=tz)


def promo_start(
    old: PriceState | None, start_day: date | None, now: datetime, tz: ZoneInfo
) -> datetime:
    """The ``promo_from`` for a start date typed in the form (dates are in ``tz``).

    - The running promo's own start date (or no date) keeps its ``promo_from``, so its
      end or price can be edited (decision 0009).
    - Today, or no date without a running promo: ``now`` (a promo "from today" starts
      when it is saved).
    - A later date: 00:00 of that day.
    - An earlier date is returned as its midnight, which the pricing rules refuse as
      ``STARTS_IN_PAST``.
    """
    running = (
        old is not None
        and old.promo_from is not None
        and old.promo_to is not None
        and old.promo_from <= now < old.promo_to
    )
    if running:
        assert old is not None and old.promo_from is not None
        if start_day is None or start_day == old.promo_from.astimezone(tz).date():
            return old.promo_from
    today = now.astimezone(tz).date()
    if start_day is None or start_day == today:
        return now
    return local_midnight(start_day, tz)


def promo_end(last_day: date, tz: ZoneInfo) -> datetime:
    """``promo_to`` for the last day the promo is in effect: the next day's 00:00 in
    ``tz``, because ``promo_to`` is exclusive and labels show its previous day."""
    return local_midnight(last_day + timedelta(days=1), tz)


def last_day(promo_to: datetime, tz: ZoneInfo) -> date:
    """The last day a promo ending at ``promo_to`` is in effect, in ``tz``."""
    return (promo_to - timedelta(microseconds=1)).astimezone(tz).date()

"""Helpers for the wave 1 integration tests: a counting label pipeline, the fake tag
type, and a promo write until workstream E's promo editor exists."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from core.db.models import DEFAULT_STORE_ID, TagTypeRow
from core.domain import pricing
from core.domain.types import PriceSource, PriceState
from core.services import catalogue as svc
from core.services.tag_types import add_tag_type
from core.services.tags import (
    LabelOutcome,
    LabelPipeline,
    LabelRequest,
    RefreshResult,
    real_label_pipeline,
    refresh_desired_hashes,
)
from shared.tag_types import Palette, TagType


@dataclass
class CountingPipeline:
    """Wraps a pipeline and records the tag id of every label it builds."""

    inner: LabelPipeline = real_label_pipeline
    calls: list[int] = field(default_factory=list)

    def __call__(self, request: LabelRequest, now: datetime) -> LabelOutcome:
        self.calls.append(request.tag_id)
        return self.inner(request, now)


def fake_tag_type() -> TagType:
    """The fake driver's 2.9" BWR type, with a unique key and model per test."""
    suffix = uuid.uuid4().hex[:8]
    return TagType(
        key=f"fake-2.9-bwr-{suffix}",
        vendor="fake",
        model=f"2.9-bwr-{suffix}",
        width=296,
        height=128,
        palette=Palette.BWR,
    )


def add_fake_tag_type(db: Session, now: datetime) -> TagTypeRow:
    return add_tag_type(db, fake_tag_type(), actor="test", now=now).row


def write_promo(
    db: Session,
    product_id: int,
    promo_cents: int,
    promo_from: datetime,
    promo_to: datetime,
    *,
    prior_cents: int | None,
    now: datetime,
    pipeline: LabelPipeline,
) -> RefreshResult:
    """Store a promo through ``apply_price_change`` and refresh the product's tags, as
    workstream E's promo editor will. ``prior_cents`` is the admin-entered prior price
    (needed while the product has less than 30 days of history)."""
    product = svc._lock_product(db, product_id, DEFAULT_STORE_ID)
    price = svc._lock_price(db, product_id, DEFAULT_STORE_ID)
    assert price is not None
    old = svc.price_state(price)
    requested = PriceState(
        regular_cents=old.regular_cents,
        promo_cents=promo_cents,
        promo_from=promo_from,
        promo_to=promo_to,
    )
    change = pricing.apply_price_change(
        svc.product_facts(product),
        old,
        requested,
        svc.history_of(db, product_id),
        now,
        PriceSource.MANUAL,
        entered_prior_cents=prior_cents,
    )
    svc._store_change(db, product, price, change, actor="test", now=now)
    return refresh_desired_hashes(db, pipeline, now, product_ids=[product_id])

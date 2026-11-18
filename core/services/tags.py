"""Tags on the database: discovery reports, heartbeats and each tag's ``desired_hash``.

``refresh_desired_hashes`` renders what every affected tag should show through a
``LabelPipeline`` and reconciles the queue. The pipeline is injectable so tests (and
the gateway API's tests) can use a fake renderer; ``real_label_pipeline`` uses
``core.domain.pricing`` and ``core.render``.
"""

from __future__ import annotations

import logging
import secrets
from collections import defaultdict
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.db.models import (
    DEFAULT_STORE_ID,
    Binding,
    EventLog,
    GatewayRow,
    Price,
    PriceHistory,
    Product,
    Tag,
    TagTypeRow,
    Template,
)
from core.domain.pricing import PromoRefusal, PromoRefused
from core.domain.types import (
    PackUnit,
    PriceKind,
    PricePoint,
    PriceSource,
    PriceState,
    ProductFacts,
)
from core.render.label import Render, TemplateKey
from core.services.queue import reconcile, tag_type_of
from core.services.render_store import save_render
from shared.gateway_api import SeenTagReport, TagsSeenResponse
from shared.tag_types import TagType

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Label pipeline


@dataclass(frozen=True, slots=True)
class BoundProduct:
    """What a binding gives the pipeline: the bound template key and the product."""

    template: TemplateKey
    product: ProductFacts
    price: PriceState | None
    history: Sequence[PricePoint]


@dataclass(frozen=True, slots=True)
class LabelRequest:
    """One tag's inputs. ``bound`` is ``None`` for an unbound tag."""

    tag_id: int
    tag_type: TagType
    short_code: str
    bound: BoundProduct | None


@dataclass(frozen=True, slots=True)
class LabelOutcome:
    render: Render
    #: When the label changes by itself next (a promo starting or ending), if ever.
    next_change: datetime | None = None
    #: Set when an active promo is refused and the regular label is shown instead.
    promo_refused: PromoRefusal | None = None


#: Turns a tag's inputs into the render it should show at ``now``. May raise
#: ``ValueError`` or ``PromoRefused`` for a label that cannot be built; the tag is
#: then left as it is and the problem logged.
LabelPipeline = Callable[[LabelRequest, datetime], LabelOutcome]


def real_label_pipeline(request: LabelRequest, now: datetime) -> LabelOutcome:
    """The production pipeline: pricing rules, then the renderer (section 7)."""
    from core.domain.pricing import (
        build_label_data,
        effective_template,
        next_label_change,
        unassigned_label_data,
    )
    from core.render import render

    bound = request.bound
    if bound is None:
        label = unassigned_label_data(request.short_code, request.tag_type.model)
        return LabelOutcome(render(label, request.tag_type))
    if bound.price is None:
        raise ValueError(f"product {bound.product.product_id} has no price")
    choice = effective_template(bound.template, bound.product, bound.price, bound.history, now)
    label = build_label_data(bound.product, bound.price, bound.history, choice.template, now)
    return LabelOutcome(
        render(label, request.tag_type),
        next_change=next_label_change(bound.price, now),
        promo_refused=choice.promo_refused,
    )


# ---------------------------------------------------------------------------
# desired_hash


@dataclass(slots=True)
class RefreshResult:
    #: Tags whose ``desired_hash`` changed.
    changed: list[int] = field(default_factory=list)
    #: Tags whose label could not be built; left unchanged.
    failed: list[int] = field(default_factory=list)
    #: Earliest moment a refreshed label changes by itself; refresh again then.
    next_change: datetime | None = None


def refresh_desired_hashes(
    session: Session,
    pipeline: LabelPipeline,
    now: datetime,
    *,
    tag_ids: Collection[int] | None = None,
    product_ids: Collection[int] | None = None,
) -> RefreshResult:
    """Recompute ``desired_hash`` for the given tags, or the tags bound to the given
    products, or every tag when both are ``None``; then reconcile those tags.

    Stores new renders, sets ``desired_hash_changed_at = now`` on every change, and
    logs ``promo.refused`` once per refused promo. Run it after a price change, a
    binding change, and at ``next_change``.
    """
    query = select(Tag).order_by(Tag.id).with_for_update(key_share=True, of=Tag)
    if tag_ids is not None:
        query = query.where(Tag.id.in_(tag_ids))
    if product_ids is not None:
        bound_tags = select(Binding.tag_id).where(Binding.product_id.in_(product_ids))
        query = query.where(Tag.id.in_(bound_tags))
    tags = list(session.scalars(query))
    result = RefreshResult()
    if not tags:
        return result
    requests = _label_requests(session, tags)
    for tag in tags:
        request = requests[tag.id]
        try:
            outcome = pipeline(request, now)
        except (ValueError, PromoRefused) as exc:
            log.warning("label for tag %s cannot be built: %s", tag.id, exc)
            result.failed.append(tag.id)
            continue
        rendered = outcome.render
        if rendered.tag_type_key != request.tag_type.key:
            raise ValueError(f"render for tag {tag.id} is for another tag type")
        save_render(session, rendered, tag.tag_type_id)
        if rendered.hash != tag.desired_hash:
            tag.desired_hash = rendered.hash
            tag.desired_hash_changed_at = now
            result.changed.append(tag.id)
        if outcome.next_change is not None and (
            result.next_change is None or outcome.next_change < result.next_change
        ):
            result.next_change = outcome.next_change
        if outcome.promo_refused is not None and request.bound is not None:
            _log_promo_refused(session, request.bound, outcome.promo_refused, now)
    session.flush()
    reconcile(session, now, [t.id for t in tags])
    return result


def _log_promo_refused(
    session: Session, bound: BoundProduct, reason: PromoRefusal, now: datetime
) -> None:
    price = bound.price
    assert price is not None and price.promo_from is not None
    payload = {
        "product_id": bound.product.product_id,
        "reason": reason.value,
        "promo_from": price.promo_from.isoformat(),
    }
    seen = session.scalar(
        select(EventLog.id).where(
            EventLog.kind == "promo.refused", EventLog.payload.contains(payload)
        )
    )
    if seen is None:
        session.add(EventLog(at=now, actor="system", kind="promo.refused", payload=payload))


def _label_requests(session: Session, tags: Sequence[Tag]) -> dict[int, LabelRequest]:
    ids = [t.id for t in tags]
    tag_types = {
        row.id: tag_type_of(row)
        for row in session.scalars(
            select(TagTypeRow).where(TagTypeRow.id.in_({t.tag_type_id for t in tags}))
        )
    }
    bindings = {
        b.tag_id: (b, key)
        for b, key in session.execute(
            select(Binding, Template.key)
            .join(Template, Binding.template_id == Template.id)
            .where(Binding.tag_id.in_(ids))
        ).tuples()
    }
    product_ids = {b.product_id for b, _ in bindings.values()}
    products = {
        p.id: p for p in session.scalars(select(Product).where(Product.id.in_(product_ids)))
    }
    prices = {
        p.product_id: p
        for p in session.scalars(select(Price).where(Price.product_id.in_(product_ids)))
    }
    history: dict[int, list[PricePoint]] = defaultdict(list)
    for h in session.scalars(
        select(PriceHistory)
        .where(PriceHistory.product_id.in_(product_ids))
        .order_by(PriceHistory.id)
    ):
        history[h.product_id].append(
            PricePoint(
                seq=h.id,
                effective_cents=h.effective_cents,
                kind=PriceKind(h.kind),
                valid_from=h.valid_from,
                source=PriceSource(h.source),
            )
        )
    requests = {}
    for tag in tags:
        bound = None
        if tag.id in bindings:
            binding, key = bindings[tag.id]
            product = products[binding.product_id]
            price = prices.get(product.id)
            bound = BoundProduct(
                template=TemplateKey(key),
                product=_facts(product),
                price=_price_state(price) if price is not None else None,
                history=tuple(history[product.id]),
            )
        requests[tag.id] = LabelRequest(tag.id, tag_types[tag.tag_type_id], tag.short_code, bound)
    return requests


def _facts(p: Product) -> ProductFacts:
    return ProductFacts(
        product_id=p.id,
        ean=p.ean,
        name=p.name,
        brand=p.brand,
        pack_qty_milli=p.pack_qty_milli,
        pack_unit=PackUnit(p.pack_unit) if p.pack_unit is not None else None,
        unit_price_required=p.unit_price_required,
        is_rx=p.is_rx,
    )


def _price_state(p: Price) -> PriceState:
    return PriceState(
        regular_cents=p.regular_cents,
        promo_cents=p.promo_cents,
        promo_from=p.promo_from,
        promo_to=p.promo_to,
        promo_prior_cents=p.promo_prior_cents,
    )


# ---------------------------------------------------------------------------
# Discovery and heartbeat

#: Short codes avoid characters that are easy to misread on a small screen.
SHORT_CODE_ALPHABET = "ACDEFGHJKLMNPQRTUVWXY34679"
SHORT_CODE_LENGTH = 4


def new_short_code(session: Session) -> str:
    """A ``short_code`` not used by any tag of the store."""
    for _ in range(100):
        code = "".join(secrets.choice(SHORT_CODE_ALPHABET) for _ in range(SHORT_CODE_LENGTH))
        taken = session.scalar(
            select(Tag.id).where(Tag.store_id == DEFAULT_STORE_ID, Tag.short_code == code)
        )
        if taken is None:
            return code
    raise RuntimeError("no free short code found")  # pragma: no cover


def record_seen_tags(
    session: Session,
    reports: Sequence[SeenTagReport],
    pipeline: LabelPipeline,
    now: datetime,
) -> TagsSeenResponse:
    """Apply a discovery batch (``POST /tags/seen``).

    A known ``vendor_id`` updates last seen, RSSI, battery and firmware. An unknown one
    whose ``(vendor, model)`` matches a ``tag_type`` becomes a new unbound tag with a
    unique ``short_code``, and its "unassigned" label is queued. Anything else is
    counted as ``ignored`` and stored nowhere.
    """
    known = created = ignored = 0
    new_tag_ids: list[int] = []
    tag_types = {
        (row.vendor, row.model): row.id
        for row in session.scalars(
            select(TagTypeRow).where(TagTypeRow.store_id == DEFAULT_STORE_ID)
        )
    }
    # Lock the known tags up front in id order, the lock order used by core.services.queue.
    existing = {
        t.vendor_id: t
        for t in session.scalars(
            select(Tag)
            .where(Tag.vendor_id.in_({r.vendor_id for r in reports}))
            .order_by(Tag.id)
            .with_for_update(key_share=True)
        )
    }
    for report in reports:
        tag = existing.get(report.vendor_id)
        if tag is None:
            type_id = tag_types.get((report.vendor, report.model)) if report.model else None
            if type_id is None:
                ignored += 1
                continue
            tag = Tag(
                store_id=DEFAULT_STORE_ID,
                vendor_id=report.vendor_id,
                tag_type_id=type_id,
                short_code=new_short_code(session),
                created_at=now,
            )
            session.add(tag)
            existing[report.vendor_id] = tag
            created += 1
        else:
            known += 1
        if tag.last_seen_at is None or report.seen_at >= tag.last_seen_at:
            tag.last_seen_at = report.seen_at
            tag.rssi = report.rssi
            tag.battery_mv = report.battery_mv
            tag.battery_pct = report.battery_pct
            if report.firmware is not None:
                tag.firmware = report.firmware
        session.flush()
        if tag.desired_hash is None and tag.id not in new_tag_ids:
            new_tag_ids.append(tag.id)
    if new_tag_ids:
        refresh_desired_hashes(session, pipeline, now, tag_ids=new_tag_ids)
    return TagsSeenResponse(known=known, created=created, ignored=ignored)


def record_heartbeat(
    session: Session, gateway_id: int, version: str, driver: str, now: datetime
) -> None:
    gateway = session.get_one(GatewayRow, gateway_id)
    gateway.last_heartbeat_at = now
    gateway.version = version
    gateway.driver = driver
    session.flush()

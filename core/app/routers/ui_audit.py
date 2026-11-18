"""Audit (PLAN.md section 9: "Walk the shelf: scan a tag code, see what it should
show"). Read-only; staff and admins.

One code field, typed or scanned, decides what to look up (decision 0018):

- an input that passes the EAN rules (``normalise_ean``, as import and bind) is a
  **product** lookup: every tag bound to that product, by short code, each with what it
  should show (the desired label, enlarged), what it last confirmed (``displayed_hash``,
  shown when it differs and its PNG is still in the render store), its status, and when
  the gateway last saw it, with battery and signal;
- an EAN that matches no product falls back to the tag lookup, so a tag whose code or
  ``vendor_id`` happens to look like an EAN is still found;
- anything else is a **tag** lookup: its ``short_code`` in any case, or the
  ``vendor_id`` printed on the tag.

The code field stays at the top, empty and focused, so the next one can be scanned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from core.app.i18n import error_message, t
from core.app.previews import Preview, product_preview
from core.app.web import CurrentUser, NowDep, SessionDep, check_csrf, render
from core.db.models import Price, Product, Tag
from core.domain.catalogue import CellError, normalise_ean
from core.render.label import TemplateKey
from core.services import admin_queries
from core.services.catalogue import history_of

router = APIRouter(prefix="/audit", dependencies=[Depends(check_csrf)], include_in_schema=False)


class Confirmed(StrEnum):
    """What the page can say about the label a tag last confirmed."""

    #: ``displayed_hash = desired_hash``: the same image as "should show".
    SAME = "same"
    #: A different label, whose PNG is still in the render store: shown.
    IMAGE = "image"
    #: A different label whose PNG is no longer in the render store.
    NOT_KEPT = "not_kept"
    #: ``displayed_hash`` is NULL: nothing confirmed (never, or the last push unconfirmed).
    UNKNOWN = "unknown"


def confirmed_state(tag: Tag, stored: set[str]) -> Confirmed:
    displayed = tag.displayed_hash
    if displayed is None:
        return Confirmed.UNKNOWN
    if displayed == tag.desired_hash:
        return Confirmed.SAME
    return Confirmed.IMAGE if displayed in stored else Confirmed.NOT_KEPT


@dataclass(frozen=True, slots=True)
class AuditEntry:
    row: admin_queries.TagRow
    preview: Preview | None
    confirmed: Confirmed


def as_ean(code: str) -> str | None:
    """``code`` as a normalised EAN, or ``None`` when it does not pass the EAN rules."""
    try:
        return normalise_ean(code)
    except CellError:
        return None


def promo_running(price: Price | None, now: datetime) -> bool:
    return (
        price is not None
        and price.promo_from is not None
        and price.promo_to is not None
        and price.promo_from <= now < price.promo_to
    )


def _product_entries(
    session: Session, product: Product, price: Price | None, now: datetime
) -> list[AuditEntry]:
    """A product's tags, read with a fixed number of queries whatever their number: one
    for the tags, one for the price history, one for the stored renders."""
    rows = admin_queries.product_tag_rows(session, product.id)
    if not rows:
        return []
    history = history_of(session, product.id)
    stored = admin_queries.stored_renders(session, (row.tag.displayed_hash for row in rows))
    previews: dict[tuple[int, str], Preview] = {}
    entries = []
    for row in rows:
        preview = None
        if row.template is not None:
            key = (row.tag_type.id, row.template)
            if key not in previews:
                previews[key] = product_preview(
                    product, price, history, row.tag_type, TemplateKey(row.template), now
                )
            preview = previews[key]
        entries.append(AuditEntry(row, preview, confirmed_state(row.tag, stored)))
    return entries


def _tag_entry(session: Session, tag: Tag, now: datetime) -> AuditEntry | None:
    row = admin_queries.tag_row(session, tag.id)
    if row is None:
        return None
    preview = None
    price = None
    if row.product is not None and row.template is not None:
        found = admin_queries.get_product(session, row.product.id)
        price = found[1] if found is not None else None
        preview = product_preview(
            row.product,
            price,
            history_of(session, row.product.id),
            row.tag_type,
            TemplateKey(row.template),
            now,
        )
    stored = admin_queries.stored_renders(session, [row.tag.displayed_hash])
    return AuditEntry(row, preview, confirmed_state(row.tag, stored))


@router.get("")
def audit_page(
    request: Request, session: SessionDep, user: CurrentUser, now: NowDep, code: str = ""
) -> Response:
    code = code.strip()[:64]
    ctx: dict[str, Any] = {
        "code": code,
        "mode": None,
        "product": None,
        "price": None,
        "entries": [],
        "error": None,
    }
    if not code:
        return render(request, "audit/index.html", ctx)
    ean = as_ean(code)
    if ean is not None:
        found = admin_queries.product_by_ean(session, ean)
        if found is not None:
            product, price = found
            ctx.update(
                mode="product",
                product=product,
                price=price,
                promo_running=promo_running(price, now),
                entries=_product_entries(session, product, price, now),
            )
            return render(request, "audit/index.html", ctx)
    tag = admin_queries.find_tag(session, code)
    entry = _tag_entry(session, tag, now) if tag is not None else None
    if entry is not None:
        ctx.update(mode="tag", entries=[entry])
    elif ean is not None:
        ctx["error"] = t("audit.ean_unknown", ean=ean)
    else:
        ctx["error"] = error_message("bind.tag_unknown", code=code)
    return render(request, "audit/index.html", ctx)

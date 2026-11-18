"""Products page (section 9): search, price, product fields, promo, label preview and
price history. Every write goes through the catalogue and promotions services."""

from __future__ import annotations

import dataclasses
import logging
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response

from core.app import formatting
from core.app.i18n import N_, error_message, t
from core.app.previews import enlarge, product_preview, render_png
from core.app.web import (
    AdminUser,
    CurrentUser,
    NowDep,
    PipelineDep,
    SessionDep,
    actor,
    check_csrf,
    flash,
    not_found,
    redirect,
    render,
)
from core.db.models import Price, Product, TagTypeRow
from core.domain.catalogue import (
    BRAND_MAX_LEN,
    NAME_MAX_LEN,
    PACK_QTY_MILLI_MAX,
    PRICE_CENTS_MAX,
)
from core.domain.pricing import PriceChange, PromoRefused, promo_active
from core.domain.types import PackUnit
from core.render.label import TemplateKey
from core.services import admin_queries
from core.services.catalogue import (
    NotFound,
    ProductFields,
    edit_price,
    edit_product,
    history_of,
    price_state,
)
from core.services.promotions import (
    NoPrice,
    PromoInput,
    edit_promo,
    end_promo,
    last_day,
    promo_end,
    promo_start,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/products", dependencies=[Depends(check_csrf)], include_in_schema=False)

#: How far ahead a typed promo date may lie; later dates are refused as out of range.
PROMO_MAX_AHEAD = timedelta(days=5 * 365 + 1)
#: How far back a typed date may lie (an earlier start is refused as in the past anyway).
PROMO_MAX_BEHIND = timedelta(days=366)


class FormError(Exception):
    """A form value is invalid. ``form`` names the form on the page; ``text`` is the
    message for an error code of ``ERROR_CODES``, or ``refusal``'s message."""

    def __init__(self, code: str, form: str, refusal: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.form = form
        self.refusal = refusal

    @property
    def text(self) -> str:
        if self.refusal is not None:
            return t(f"promo.refusal.{self.refusal}")
        return error_message(self.code)


# --- Pages ----------------------------------------------------------------------------


@router.get("")
def product_list(request: Request, session: SessionDep, user: CurrentUser, q: str = "") -> Response:
    rows = admin_queries.search_products(session, q[:200])
    return render(request, "products/list.html", {"q": q, "rows": rows})


def _tag_type(session: SessionDep, key: str | None) -> tuple[list[TagTypeRow], TagTypeRow | None]:
    types = admin_queries.tag_type_rows(session)
    chosen = next((tt for tt in types if tt.key == key), types[0] if types else None)
    return types, chosen


def _load(session: SessionDep, product_id: int) -> tuple[Product, Price | None]:
    found = admin_queries.get_product(session, product_id)
    if found is None:
        raise not_found()
    return found


def _product_page(
    request: Request,
    session: SessionDep,
    product_id: int,
    now: datetime,
    tag_type_key: str | None = None,
    *,
    error: FormError | None = None,
    values: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    if error is not None:
        log.info(
            "ui error %s (%s form, product %s)", error.refusal or error.code, error.form, product_id
        )
    product, price = _load(session, product_id)
    types, tag_type = _tag_type(session, tag_type_key)
    tags = admin_queries.product_tags(session, product_id)
    history = list(history_of(session, product_id))
    preview = None
    if tag_type is not None:
        bound = TemplateKey(tags[0].template) if tags else None
        preview = product_preview(product, price, history, tag_type, bound, now)
    tz = formatting.display_tz()
    promo = _promo_form(price, now, tz) if price is not None else None
    context = {
        "product": product,
        "price": price,
        "tags": tags,
        "tag_types": types,
        "tag_type": tag_type,
        "preview": preview,
        "history": admin_queries.price_history_rows(session, product_id),
        "promo": promo,
        "units": [u.value for u in PackUnit],
        "error": error,
        "values": values or {},
    }
    return render(request, "products/detail.html", context, status_code=status_code)


def _promo_form(price: Price, now: datetime, tz: Any) -> dict[str, Any]:
    state = price_state(price)
    has_promo = state.promo_cents is not None and state.promo_to is not None
    current = has_promo and state.promo_to is not None and state.promo_to > now
    today = now.astimezone(tz).date()
    return {
        "max_day": today + PROMO_MAX_AHEAD,
        "has_promo": current,
        "running": current and promo_active(state, now),
        "cents": state.promo_cents if current else None,
        "start": state.promo_from.astimezone(tz).date() if current and state.promo_from else None,
        "last_day": last_day(state.promo_to, tz) if current and state.promo_to else None,
        "prior_cents": state.promo_prior_cents if current else None,
        "today": today,
    }


@router.get("/{product_id}")
def product_detail(
    request: Request,
    product_id: int,
    session: SessionDep,
    user: CurrentUser,
    now: NowDep,
    tag_type: str | None = None,
) -> Response:
    return _product_page(request, session, product_id, now, tag_type)


@router.get("/{product_id}/preview.png")
def product_preview_png(
    product_id: int,
    session: SessionDep,
    user: CurrentUser,
    now: NowDep,
    tag_type: str | None = None,
) -> Response:
    product, price = _load(session, product_id)
    _, chosen = _tag_type(session, tag_type)
    if chosen is None:
        raise not_found()
    tags = admin_queries.product_tags(session, product_id)
    bound = TemplateKey(tags[0].template) if tags else None
    preview = product_preview(product, price, history_of(session, product_id), chosen, bound, now)
    if preview.label is None:
        raise not_found()
    try:
        png = render_png(preview.label, chosen)
    except ValueError:
        raise not_found() from None
    # The URL carries the label hash (?v=...), so a changed label is a new URL.
    return Response(enlarge(png), media_type="image/png", headers={"Cache-Control": "no-cache"})


# --- Writes ---------------------------------------------------------------------------


def _price_cents(text: str, form: str) -> int:
    cents = formatting.parse_cents(text)
    if cents is None:
        raise FormError("price.invalid", form)
    if cents == 0:
        raise FormError("price.zero", form)
    if cents > PRICE_CENTS_MAX:
        raise FormError("price.too_large", form)
    return cents


def _flash_price_events(request: Request, change: PriceChange | None) -> None:
    if change is None:
        return
    for event in change.events:
        if event.kind == "promo.refused":
            reason = t(f"promo.refusal.{event.payload.get('reason')}")
            flash(request, "warning", N_("products.promo.removed_refused"), reason=reason)
        elif event.kind == "promo.ended_rx":
            flash(request, "warning", N_("products.promo.ended_rx"))


@router.post("/{product_id}/price")
def update_price(
    request: Request,
    product_id: int,
    session: SessionDep,
    user: AdminUser,
    now: NowDep,
    pipeline: PipelineDep,
    regular_price: Annotated[str, Form(max_length=40)] = "",
) -> Response:
    try:
        cents = _price_cents(regular_price, "price")
        change = edit_price(
            session, product_id, cents, actor=actor(user), now=now, pipeline=pipeline
        )
    except FormError as exc:
        return _product_page(
            request, session, product_id, now, error=exc,
            values={"regular_price": regular_price}, status_code=422,
        )  # fmt: skip
    except NotFound:
        raise not_found() from None
    session.commit()
    if change is None:
        flash(request, "info", N_("products.price.unchanged"))
    else:
        flash(request, "success", N_("products.price.saved"), price=formatting.money(cents))
        _flash_price_events(request, change)
    return redirect(f"/products/{product_id}")


def _fields(
    current: Product,
    name: str,
    brand: str,
    pack_qty: str,
    pack_unit: str,
    unit_price_required: bool,
    is_rx: bool,
    active: bool,
) -> ProductFields:
    if not name.strip():
        raise FormError("product.name_empty", "fields")
    if len(name.strip()) > NAME_MAX_LEN:
        raise FormError("product.name_too_long", "fields")
    if len(brand.strip()) > BRAND_MAX_LEN:
        raise FormError("product.brand_too_long", "fields")
    qty: int | None = None
    unit: PackUnit | None = None
    if pack_qty.strip() or pack_unit:
        qty = formatting.parse_milli(pack_qty)
        units = {u.value for u in PackUnit}
        if qty is None or not 0 < qty <= PACK_QTY_MILLI_MAX or pack_unit not in units:
            raise FormError("product.pack_invalid", "fields")
        unit = PackUnit(pack_unit)
    if unit_price_required and (qty is None or unit is None):
        raise FormError("product.unit_price_needs_pack", "fields")
    return dataclasses.replace(
        ProductFields.of(current),
        name=name,
        brand=brand or None,
        pack_qty_milli=qty,
        pack_unit=unit,
        unit_price_required=unit_price_required,
        is_rx=is_rx,
        active=active,
    )


@router.post("/{product_id}/fields")
def update_fields(
    request: Request,
    product_id: int,
    session: SessionDep,
    user: AdminUser,
    now: NowDep,
    pipeline: PipelineDep,
    name: Annotated[str, Form(max_length=1000)] = "",
    brand: Annotated[str, Form(max_length=1000)] = "",
    pack_qty: Annotated[str, Form(max_length=40)] = "",
    pack_unit: Annotated[str, Form(max_length=10)] = "",
    unit_price_required: Annotated[bool, Form()] = False,
    is_rx: Annotated[bool, Form()] = False,
    active: Annotated[bool, Form()] = False,
) -> Response:
    product, _ = _load(session, product_id)
    try:
        fields = _fields(
            product, name, brand, pack_qty, pack_unit, unit_price_required, is_rx, active
        )
        change = edit_product(
            session, product_id, fields, actor=actor(user), now=now, pipeline=pipeline
        )
    except FormError as exc:
        values = {"name": name, "brand": brand, "pack_qty": pack_qty, "pack_unit": pack_unit}
        return _product_page(
            request, session, product_id, now, error=exc, values=values, status_code=422
        )
    except ValueError as exc:
        log.info("product edit refused: %s", exc)
        error = FormError("invalid_input", "fields")
        return _product_page(request, session, product_id, now, error=error, status_code=422)
    session.commit()
    flash(request, "success", N_("products.fields.saved"))
    _flash_price_events(request, change)
    return redirect(f"/products/{product_id}")


def _date(text: str, required: bool, today: date) -> date | None:
    """A date from the form, within ``PROMO_MAX_BEHIND`` and ``PROMO_MAX_AHEAD`` of
    ``today`` (a far-off date would overflow the datetime range)."""
    if not text.strip():
        if required:
            raise FormError("promo.dates_invalid", "promo")
        return None
    try:
        day = date.fromisoformat(text.strip())
    except ValueError:
        raise FormError("promo.dates_invalid", "promo") from None
    if not today - PROMO_MAX_BEHIND <= day <= today + PROMO_MAX_AHEAD:
        raise FormError("promo.date_out_of_range", "promo")
    return day


@router.post("/{product_id}/promo")
def update_promo(
    request: Request,
    product_id: int,
    session: SessionDep,
    user: AdminUser,
    now: NowDep,
    pipeline: PipelineDep,
    promo_price: Annotated[str, Form(max_length=40)] = "",
    start: Annotated[str, Form(max_length=20)] = "",
    last: Annotated[str, Form(max_length=20)] = "",
    prior_price: Annotated[str, Form(max_length=40)] = "",
) -> Response:
    product, price = _load(session, product_id)
    values = {"promo_price": promo_price, "start": start, "last": last, "prior_price": prior_price}
    tz = formatting.display_tz()
    try:
        if product.is_rx:
            raise FormError("promo.rx", "promo")
        cents = formatting.parse_cents(promo_price)
        if cents is None or cents == 0 or cents > PRICE_CENTS_MAX:
            raise FormError("promo.price_invalid", "promo")
        prior: int | None = None
        if prior_price.strip():
            prior = formatting.parse_cents(prior_price)
            if prior is None or prior == 0 or prior > PRICE_CENTS_MAX:
                raise FormError("promo.prior_invalid", "promo")
        today = now.astimezone(tz).date()
        start_day = _date(start, required=False, today=today)
        last_day_ = _date(last, required=True, today=today)
        assert last_day_ is not None
        old = price_state(price) if price is not None else None
        promo = PromoInput(cents, promo_start(old, start_day, now, tz), promo_end(last_day_, tz))
        result = edit_promo(
            session, product_id, promo, entered_prior_cents=prior,
            actor=actor(user), now=now, pipeline=pipeline,
        )  # fmt: skip
    except FormError as exc:
        return _product_page(
            request, session, product_id, now, error=exc, values=values, status_code=422
        )
    except PromoRefused as refused:
        log.info("promo for product %s refused: %s", product_id, refused.reason.value)
        error = FormError("promo.refused", "promo", refusal=refused.reason.value)
        return _product_page(
            request, session, product_id, now, error=error, values=values, status_code=422
        )
    except NoPrice:
        error = FormError("promo.no_price", "promo")
        return _product_page(
            request, session, product_id, now, error=error, values=values, status_code=422
        )
    session.commit()
    removed_refused = result.change is not None and any(
        e.kind == "promo.refused" for e in result.change.events
    )
    if result.change is None:
        flash(request, "info", N_("products.promo.unchanged"))
    elif removed_refused:
        # Rule 3 removed the promo on re-validation: show why, not "ended".
        _flash_price_events(request, result.change)
    elif result.promo_removed:
        flash(request, "success", N_("products.promo.ended"))
    else:
        flash(request, "success", N_("products.promo.saved"))
        if result.prior_saved:
            flash(request, "success", N_("products.promo.prior_saved"))
    return redirect(f"/products/{product_id}")


@router.post("/{product_id}/promo/end")
def stop_promo(
    request: Request,
    product_id: int,
    session: SessionDep,
    user: AdminUser,
    now: NowDep,
    pipeline: PipelineDep,
) -> Response:
    try:
        result = end_promo(session, product_id, actor=actor(user), now=now, pipeline=pipeline)
    except (NotFound, NoPrice):
        raise not_found() from None
    session.commit()
    if result.change is None:
        flash(request, "info", N_("products.promo.unchanged"))
    else:
        flash(request, "success", N_("products.promo.ended"))
    return redirect(f"/products/{product_id}")

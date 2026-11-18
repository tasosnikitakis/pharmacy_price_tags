"""Binding a tag to a product from the phone (PLAN.md section 9).

1. Scan or type the product's EAN (``GET /bind?ean=``). An unknown EAN: an admin can
   create the product here (``POST /bind/product``); staff are told an admin must.
2. Type the tag's ``short_code``, which an unbound tag shows on its screen
   (``&code=``). A tag that is already bound says to what, and binding it again needs
   a confirmation (``&rebind=<product id it is bound to>``).
3. Choose a template (the default follows the product's flags) and see the preview for
   the tag's own type; confirm (``POST /bind``, through ``core.services.tags.bind_tag``).
4. ``GET /bind/tags/<id>`` shows pending, then done (the tag shows its desired label),
   failed (with the push error; retried) or dead. Its progress box polls
   ``/bind/tags/<id>/progress`` every few seconds with HTMX until it is final.

The steps are plain GET forms, so the browser's back button and a USB scanner (which
types into the focused field and presses Enter) work; the camera fills the same field.
Staff and admins may bind; only admins may create products.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response

from core.app.binding import pick_template
from core.app.i18n import N_, error_message
from core.app.import_messages import row_reason
from core.app.previews import Preview, enlarge, product_preview, render_png
from core.app.web import (
    AdminUser,
    CurrentUser,
    NowDep,
    PipelineDep,
    SessionDep,
    actor,
    check_csrf,
    flash,
    flash_error,
    not_found,
    redirect,
    render,
    templates,
)
from core.db.models import Price, Product
from core.domain.catalogue import CellError, normalise_ean, parse_pack, parse_price_cents
from core.domain.types import PackUnit
from core.render.label import TemplateKey
from core.services import admin_queries
from core.services.admin_queries import TagRow, TagStatus
from core.services.catalogue import history_of
from core.services.products import (
    NewProduct,
    ProductCreateError,
    ProductExists,
    create_product,
    find_by_ean,
)
from core.services.tags import BindingChanged, BindingError, bind_tag

log = logging.getLogger(__name__)

router = APIRouter(prefix="/bind", dependencies=[Depends(check_csrf)], include_in_schema=False)

#: Seconds between two polls of the progress box.
POLL_SECONDS = 3
#: Statuses after which the progress box stops polling.
FINAL = frozenset({TagStatus.UP_TO_DATE, TagStatus.DEAD, TagStatus.NO_LABEL})


def bind_url(ean: str = "", code: str = "", **extra: str) -> str:
    params = {k: v for k, v in {"ean": ean, "code": code, **extra}.items() if v}
    return f"/bind?{urlencode(params)}" if params else "/bind"


# --- Steps ----------------------------------------------------------------------------


def _page(
    request: Request,
    session: SessionDep,
    now: datetime,
    *,
    ean: str = "",
    code: str = "",
    template: str = "",
    rebind: str = "",
    error: str | None = None,
    create_values: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    """The binding page at the step its inputs reach. ``error`` is a ready message."""
    ctx: dict[str, Any] = {
        "ean": ean.strip(),
        "code": code.strip(),
        "step": "ean",
        "error": error,
        "units": [u.value for u in PackUnit],
        "create": create_values or {},
        "poll_seconds": POLL_SECONDS,
    }

    def done(step: str) -> Response:
        ctx["step"] = step
        return render(request, "bind/index.html", ctx, status_code=status_code)

    if not ctx["ean"]:
        return done("ean")
    try:
        ean_value = normalise_ean(ctx["ean"])
    except CellError as exc:
        ctx["error"] = row_reason(str(exc))
        return done("ean")
    ctx["ean"] = ean_value
    product = find_by_ean(session, ean_value)
    if product is None:
        return done("create")
    found = admin_queries.get_product(session, product.id)
    assert found is not None
    _, price = found
    ctx["product"], ctx["price"] = product, price
    if price is None:
        ctx["error"] = error_message("bind.no_price")
        return done("code")
    if not ctx["code"]:
        return done("code")
    tag = admin_queries.find_tag(session, ctx["code"])
    if tag is None:
        ctx["error"] = error_message("bind.tag_unknown", code=ctx["code"])
        return done("code")
    row = admin_queries.tag_row(session, tag.id)
    assert row is not None
    ctx["row"], ctx["code"] = row, row.tag.short_code
    bound_to = row.product.id if row.product is not None else None
    ctx["rebind_from"] = "" if bound_to is None else str(bound_to)
    if bound_to is not None and rebind != str(bound_to):
        return done("rebind")
    options, chosen = pick_template(template, product, price, row.tag_type, now)
    ctx["options"], ctx["template"] = options, chosen
    ctx["preview"] = _preview(session, product, price, row, chosen, now)
    return done("confirm")


def _preview(
    session: SessionDep,
    product: Product,
    price: Price | None,
    row: TagRow,
    template: TemplateKey,
    now: datetime,
) -> Preview:
    return product_preview(
        product, price, history_of(session, product.id), row.tag_type, template, now
    )


@router.get("")
def bind_page(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    now: NowDep,
    ean: str = "",
    code: str = "",
    template: str = "",
    rebind: str = "",
) -> Response:
    return _page(
        request, session, now,
        ean=ean[:40], code=code[:64], template=template[:20], rebind=rebind[:20],
    )  # fmt: skip


def _bind_target(
    session: SessionDep, product_id: int, tag_id: int
) -> tuple[Product, Price | None, TagRow]:
    found = admin_queries.get_product(session, product_id)
    row = admin_queries.tag_row(session, tag_id)
    if found is None or row is None:
        raise not_found()
    return found[0], found[1], row


@router.get("/preview")
def preview_fragment(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    now: NowDep,
    product_id: int,
    tag_id: int,
    template: str = "",
) -> Response:
    """The preview box for another template choice (HTMX, on change)."""
    product, price, row = _bind_target(session, product_id, tag_id)
    _, chosen = pick_template(template[:20], product, price, row.tag_type, now)
    ctx = {
        "product": product,
        "row": row,
        "template": chosen,
        "preview": _preview(session, product, price, row, chosen, now),
    }
    return _fragment(request, "bind/_preview.html", ctx)


@router.get("/preview.png")
def preview_png(
    session: SessionDep,
    user: CurrentUser,
    now: NowDep,
    product_id: int,
    tag_id: int,
    template: str = "",
) -> Response:
    product, price, row = _bind_target(session, product_id, tag_id)
    _, chosen = pick_template(template[:20], product, price, row.tag_type, now)
    preview = _preview(session, product, price, row, chosen, now)
    if preview.label is None:
        raise not_found()
    try:
        png = render_png(preview.label, row.tag_type)
    except ValueError:
        raise not_found() from None
    # The URL carries the label hash (?v=...), so a changed label is a new URL.
    return Response(enlarge(png), media_type="image/png", headers={"Cache-Control": "no-cache"})


# --- Confirm --------------------------------------------------------------------------


@router.post("")
def confirm(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    now: NowDep,
    pipeline: PipelineDep,
    product_id: Annotated[int, Form()],
    tag_id: Annotated[int, Form()],
    template: Annotated[str, Form(max_length=20)] = "",
    rebind_from: Annotated[str, Form(max_length=20)] = "",
) -> Response:
    product, price, row = _bind_target(session, product_id, tag_id)
    back = bind_url(product.ean, row.tag.short_code)
    bound_to = row.product.id if row.product is not None else None
    if rebind_from != ("" if bound_to is None else str(bound_to)):
        # The tag was bound or rebound since the page was shown: ask again.
        flash(request, "warning", N_("bind.changed_meanwhile"))
        return redirect(back)
    _, chosen = pick_template(template, product, price, row.tag_type, now)
    if chosen.value != template:
        flash_error(request, "bind.template_invalid")
        return redirect(back)
    if price is None:
        flash_error(request, "bind.no_price")
        return redirect(back)
    try:
        result = bind_tag(
            session, tag_id, product_id, actor=actor(user), now=now, template=chosen,
            bound_by=user.id, pipeline=pipeline, expect_bound_to=bound_to,
        )  # fmt: skip
    except BindingChanged:
        # A bind or unbind committed between the check above and the tag lock.
        session.rollback()
        flash(request, "warning", N_("bind.changed_meanwhile"))
        return redirect(back)
    except BindingError:
        raise not_found() from None
    session.commit()
    log.info("user %s bound tag %s to product %s (%s)", user.id, tag_id, product_id, chosen)
    flash(request, "success", N_("bind.bound"), code=row.tag.short_code, name=product.name)
    if result.failed:
        flash_error(request, "bind.label_failed")
    return redirect(f"/bind/tags/{tag_id}")


# --- Progress -------------------------------------------------------------------------


def _fragment(request: Request, name: str, ctx: dict[str, Any]) -> Response:
    """A partial for HTMX: no layout and no flash messages (they wait for a page)."""
    response = templates.TemplateResponse(request, name, ctx)
    response.headers["Cache-Control"] = "no-store"
    return response


def _progress_context(session: SessionDep, tag_id: int) -> dict[str, Any]:
    progress = admin_queries.tag_progress(session, tag_id)
    if progress is None:
        raise not_found()
    status = progress.status
    return {
        "tag_id": tag_id,
        "progress": progress,
        "state": status.value,
        "polling": status not in FINAL,
        "poll_seconds": POLL_SECONDS,
    }


@router.get("/tags/{tag_id}")
def bound_page(request: Request, tag_id: int, session: SessionDep, user: CurrentUser) -> Response:
    row = admin_queries.tag_row(session, tag_id)
    if row is None:
        raise not_found()
    ctx = {"row": row, **_progress_context(session, tag_id)}
    return render(request, "bind/bound.html", ctx)


@router.get("/tags/{tag_id}/progress")
def progress(request: Request, tag_id: int, session: SessionDep, user: CurrentUser) -> Response:
    """Polled every ``POLL_SECONDS`` while pending or failed: one tag row and its
    newest job, nothing else."""
    return _fragment(request, "bind/_progress.html", _progress_context(session, tag_id))


# --- Creating an unknown product (admin) ------------------------------------------------


@router.post("/product")
def new_product(
    request: Request,
    session: SessionDep,
    user: AdminUser,
    now: NowDep,
    ean: Annotated[str, Form(max_length=40)] = "",
    name: Annotated[str, Form(max_length=1000)] = "",
    regular_price: Annotated[str, Form(max_length=40)] = "",
    pack_qty: Annotated[str, Form(max_length=40)] = "",
    pack_unit: Annotated[str, Form(max_length=10)] = "",
    unit_price_required: Annotated[bool, Form()] = False,
    is_rx: Annotated[bool, Form()] = False,
) -> Response:
    values = {
        "name": name, "regular_price": regular_price, "pack_qty": pack_qty,
        "pack_unit": pack_unit, "unit_price_required": unit_price_required, "is_rx": is_rx,
    }  # fmt: skip

    def refused(message: str) -> Response:
        return _page(
            request, session, now, ean=ean, error=message, create_values=values, status_code=422
        )

    try:
        ean_value = normalise_ean(ean)
    except CellError:
        # The page itself shows why the EAN is refused.
        return _page(request, session, now, ean=ean, create_values=values, status_code=422)
    try:
        # Price and pack size are parsed exactly as an import parses them (decision 0011).
        cents = parse_price_cents(regular_price)
        pack = parse_pack(pack_qty, pack_unit) if pack_qty.strip() or pack_unit else None
        if pack_unit and pack is None:
            raise ProductCreateError("product.pack_invalid")
    except CellError as exc:
        return refused(row_reason(str(exc)))
    except ProductCreateError as exc:
        return refused(error_message(exc.code))
    new = NewProduct(
        ean=ean_value,
        name=name,
        regular_cents=cents,
        pack_qty_milli=pack[0] if pack is not None else None,
        pack_unit=pack[1] if pack is not None else None,
        unit_price_required=unit_price_required,
        is_rx=is_rx,
    )
    try:
        product = create_product(session, new, actor=actor(user), now=now)
    except ProductExists:
        flash(request, "info", N_("bind.product_exists"))
        return redirect(bind_url(ean_value))
    except ProductCreateError as exc:
        log.info("product create refused: %s", exc.code)
        return refused(error_message(exc.code))
    session.commit()
    log.info("user %s created product %s from the binding page", user.id, product.id)
    flash(request, "success", N_("bind.product_created"), name=product.name)
    return redirect(bind_url(product.ean))

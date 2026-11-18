"""Audit (PLAN.md section 9: "Walk the shelf: scan a tag code, see what it should
show"). Read-only; staff and admins.

Type or scan a tag's code (its ``short_code`` in any case, or the ``vendor_id`` printed
on the tag). The page shows the bound product and the price the tag should show now,
the desired label enlarged, whether the tag is up to date, pending or failed, and when
the gateway last saw it, with battery and signal. The code field stays at the top,
empty and focused, so the next tag can be scanned at once.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from core.app.i18n import error_message
from core.app.previews import product_preview
from core.app.web import CurrentUser, NowDep, SessionDep, check_csrf, render
from core.render.label import TemplateKey
from core.services import admin_queries
from core.services.catalogue import history_of

router = APIRouter(prefix="/audit", dependencies=[Depends(check_csrf)], include_in_schema=False)


@router.get("")
def audit_page(
    request: Request, session: SessionDep, user: CurrentUser, now: NowDep, code: str = ""
) -> Response:
    code = code.strip()[:64]
    ctx: dict[str, Any] = {"code": code, "row": None, "error": None, "preview": None}
    if code:
        tag = admin_queries.find_tag(session, code)
        if tag is None:
            ctx["error"] = error_message("bind.tag_unknown", code=code)
        else:
            row = admin_queries.tag_row(session, tag.id)
            ctx["row"] = row
            if row is not None and row.product is not None and row.template is not None:
                found = admin_queries.get_product(session, row.product.id)
                price = found[1] if found is not None else None
                ctx["price"] = price
                ctx["preview"] = product_preview(
                    row.product,
                    price,
                    history_of(session, row.product.id),
                    row.tag_type,
                    TemplateKey(row.template),
                    now,
                )
    return render(request, "audit/index.html", ctx)

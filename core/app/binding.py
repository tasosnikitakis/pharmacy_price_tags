"""Template choice for the phone binding flow (PLAN.md section 9, step 3).

Only templates that can work for the product and the tag are offered:

- ``standard``, unless the product needs a unit price (a ``standard`` binding would be
  drawn as ``unit_price`` anyway, ``effective_template``);
- ``unit_price`` only when the pack size and unit are set;
- ``promo`` never for prescription medicines (``is_rx``), and not on a tag type that
  has no red (a promo label needs it, ``core.render``). A ``promo`` binding shows the
  regular label whenever no promo is running.

The default is ``choose_template`` for the product, or the first option when that one
is not offered.
"""

from __future__ import annotations

from datetime import datetime

from core.db.models import Price, Product, TagTypeRow
from core.domain.pricing import choose_template
from core.render.label import TemplateKey
from core.services.catalogue import price_state, product_facts
from shared.tag_types import PALETTE_COLOURS, Palette

#: Red as ``shared.tag_types.PALETTE_COLOURS`` defines it; promo labels need it.
_RED = (255, 0, 0)


def has_red(tag_type: TagTypeRow) -> bool:
    return _RED in PALETTE_COLOURS[Palette(tag_type.palette)]


def template_options(product: Product, tag_type: TagTypeRow) -> list[TemplateKey]:
    options: list[TemplateKey] = []
    if not product.unit_price_required:
        options.append(TemplateKey.STANDARD)
    if product.pack_qty_milli is not None and product.pack_unit is not None:
        options.append(TemplateKey.UNIT_PRICE)
    if not product.is_rx and has_red(tag_type):
        options.append(TemplateKey.PROMO)
    return options


def default_template(
    product: Product, price: Price | None, options: list[TemplateKey], now: datetime
) -> TemplateKey:
    regular = TemplateKey.UNIT_PRICE if product.unit_price_required else TemplateKey.STANDARD
    key = choose_template(product_facts(product), price_state(price), now) if price else regular
    if key in options:
        return key
    return regular if regular in options else options[0]


def pick_template(
    requested: str, product: Product, price: Price | None, tag_type: TagTypeRow, now: datetime
) -> tuple[list[TemplateKey], TemplateKey]:
    """The offered templates and the chosen one: ``requested`` if it is offered, else
    the default."""
    options = template_options(product, tag_type)
    chosen = next((o for o in options if o.value == requested), None)
    return options, chosen or default_template(product, price, options, now)

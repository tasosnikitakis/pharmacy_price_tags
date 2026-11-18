"""Label previews: the rendered PNG, enlarged with nearest-neighbour scaling (section 7).

The Products page renders the product's label with ``core.render`` for a chosen tag
type, without storing anything; the Tags page shows renders already in the render
store. Every pixel stays one palette colour, so the preview is exactly what the tag
receives, only bigger.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from PIL import Image

from core.app.i18n import N_
from core.db.models import Price, Product, TagTypeRow
from core.domain.pricing import (
    PromoRefused,
    build_label_data,
    choose_template,
    effective_template,
)
from core.domain.types import PricePoint
from core.render import LabelData, label_hash, render
from core.render.label import TemplateKey
from core.services.catalogue import price_state, product_facts
from core.services.queue import tag_type_of

#: How much a preview is enlarged; 296x128 becomes 888x384.
PREVIEW_SCALE = 3


@dataclass(frozen=True, slots=True)
class Preview:
    """A label that can be shown, or why not: ``error`` is a UI string key, ``refusal``
    a ``PromoRefusal`` value."""

    label: LabelData | None
    digest: str | None
    template: TemplateKey | None
    error: str | None = None
    refusal: str | None = None


def product_preview(
    product: Product,
    price: Price | None,
    history: Sequence[PricePoint],
    tag_type: TagTypeRow,
    bound: TemplateKey | None,
    now: datetime,
) -> Preview:
    """What a tag of ``tag_type`` bound to ``product`` shows at ``now``.

    ``bound`` is the template of the product's binding, if it has one; otherwise the
    template the binding flow would offer (``choose_template``).
    """
    if price is None:
        return Preview(None, None, None, error=N_("preview.no_price"))
    facts = product_facts(product)
    state = price_state(price)
    key = bound or choose_template(facts, state, now)
    choice = effective_template(key, facts, state, history, now)
    try:
        label = build_label_data(facts, state, history, choice.template, now)
    except PromoRefused as refused:
        return Preview(None, None, choice.template, refusal=refused.reason.value)
    except ValueError:
        return Preview(None, None, choice.template, error=N_("preview.unavailable"))
    refusal = choice.promo_refused.value if choice.promo_refused is not None else None
    digest = label_hash(label, tag_type_of(tag_type))
    return Preview(label, digest, choice.template, refusal=refusal)


def render_png(label: LabelData, tag_type: TagTypeRow) -> bytes:
    """The label's PNG at the tag's native size. Raises ``ValueError`` if the label
    cannot be drawn on this tag type (a promo on a black-and-white tag)."""
    return render(label, tag_type_of(tag_type)).png


def enlarge(png: bytes, scale: int = PREVIEW_SCALE) -> bytes:
    """``png`` scaled up by ``scale`` with nearest-neighbour sampling, palette kept."""
    with Image.open(io.BytesIO(png)) as image:
        image.load()
        big = image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)
    out = io.BytesIO()
    big.save(out, format="PNG", optimize=False)
    return out.getvalue()

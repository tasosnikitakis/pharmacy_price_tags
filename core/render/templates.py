"""The four code-defined label templates (PLAN.md section 7).

Each template turns ``LabelData`` into placed items for one tag size. Coordinates are
designed for 296x128 and scaled to other sizes. Bump ``TEMPLATE_VERSIONS`` in
``core/render/label.py`` on any visual change: the version is part of the render hash.

Colour rule: red, and yellow on ``BWRY``, appear only in promo elements.
"""

from __future__ import annotations

from collections.abc import Callable

from core.render._layout import (
    Align,
    Box,
    Item,
    LineItem,
    RectItem,
    fit_line,
    fit_name,
    fit_price_with_unit,
)
from core.render._palette import Colour, has_colour
from core.render.label import LabelData, TemplateKey
from shared.tag_types import Palette, TagType

REF_WIDTH, REF_HEIGHT = 296, 128

#: Product-name sizes, largest first (section 7: shrink, then truncate).
NAME_SIZES = (24, 22, 20, 18, 16, 14, 13, 12)
PROMO_NAME_SIZES = (20, 18, 16, 14, 13, 12)
# Regular weight skips 13 and 14 px: at those sizes the hinted DejaVu Sans Condensed
# drops or fuses the tonos of accented capitals (Ή, Ί, Ώ, Έ). Bold keeps it at all sizes.
BRAND_SIZES = (16, 15, 12)
#: Price sizes, largest first; also the sizes the unit price may take.
PRICE_SIZES = (64, 60, 56, 52, 48, 46, 44, 42, 40, 38, 36, 34, 32, 30, 28, 26, 24, 22, 20,
               18, 16, 14, 12)  # fmt: skip
PRIOR_SIZES = (22, 20, 18, 16, 15, 12)
UNTIL_SIZES = (16, 15, 12)
BADGE_SIZES = (14, 13, 12)
SHORT_CODE_SIZES = (88, 80, 72, 64, 56, 48, 40, 32, 24)
TAG_MODEL_SIZES = (16, 15, 12)

PROMO_BADGE = "ΠΡΟΣΦΟΡΑ"
UNTIL_PREFIX = "έως"


class _Grid:
    """Scales the 296x128 design to the tag's size."""

    def __init__(self, tag_type: TagType) -> None:
        self.sx = tag_type.width / REF_WIDTH
        self.sy = tag_type.height / REF_HEIGHT

    def box(self, left: int, top: int, right: int, bottom: int) -> Box:
        return Box(
            round(left * self.sx), round(top * self.sy),
            round(right * self.sx), round(bottom * self.sy),
        )  # fmt: skip

    def sizes(self, sizes: tuple[int, ...]) -> tuple[int, ...]:
        scaled = (max(1, round(size * self.sy)) for size in sizes)
        return tuple(dict.fromkeys(scaled))  # keep order, drop duplicates

    def px(self, value: int) -> int:
        return max(1, round(value * self.sy))


def _need(label: LabelData, *names: str) -> None:
    missing = [name for name in names if not getattr(label, name)]
    if missing:
        raise ValueError(f"{label.template.value} label needs {', '.join(missing)}")


def _brand(label: LabelData, grid: _Grid, box: Box) -> list[Item]:
    if not label.brand:
        return []
    return [fit_line("brand", label.brand, False, grid.sizes(BRAND_SIZES), box, Colour.BLACK)]


def standard(label: LabelData, tag_type: TagType) -> list[Item]:
    """Name (two lines), brand, price."""
    _need(label, "name", "price_text")
    assert label.price_text is not None
    g = _Grid(tag_type)
    items: list[Item] = []
    items += fit_name(label.name, True, g.sizes(NAME_SIZES), g.box(4, 3, 292, 51))
    items += _brand(label, g, g.box(4, 52, 292, 69))
    items += fit_price_with_unit(
        label.price_text, None, g.sizes(PRICE_SIZES), g.box(4, 71, 292, 125),
        Colour.BLACK, Colour.BLACK, g.px(3),
    )  # fmt: skip
    return items


def unit_price(label: LabelData, tag_type: TagType) -> list[Item]:
    """Name, brand, price, and below it the unit price at >= 70% of its size."""
    _need(label, "name", "price_text", "unit_price_text")
    assert label.price_text is not None
    g = _Grid(tag_type)
    items: list[Item] = []
    items += fit_name(label.name, True, g.sizes(NAME_SIZES), g.box(4, 3, 292, 41))
    items += _brand(label, g, g.box(4, 41, 292, 57))
    items += fit_price_with_unit(
        label.price_text, label.unit_price_text, g.sizes(PRICE_SIZES), g.box(4, 58, 292, 125),
        Colour.BLACK, Colour.BLACK, g.px(3),
    )  # fmt: skip
    return items


def promo(label: LabelData, tag_type: TagType) -> list[Item]:
    """Name, a promo badge, the prior price struck through, the last day, and the promo
    price in red; the unit price (from the promo price) below it when given."""
    _need(label, "name", "price_text", "prior_price_text", "promo_until_text")
    assert label.price_text is not None
    assert label.prior_price_text is not None
    if not has_colour(tag_type.palette, Colour.RED):
        raise ValueError(f"a promo label needs red; palette {tag_type.palette.value} has none")
    g = _Grid(tag_type)
    items: list[Item] = []
    items += fit_name(label.name, True, g.sizes(PROMO_NAME_SIZES), g.box(4, 3, 292, 42))

    # Badge: white on red, or red on yellow where the tag has yellow.
    badge_box = g.box(4, 46, 104, 64)
    yellow = tag_type.palette is Palette.BWRY
    badge_fill, badge_ink = (Colour.YELLOW, Colour.RED) if yellow else (Colour.RED, Colour.WHITE)
    items.append(RectItem("badge", badge_box, badge_fill))
    inner = Box(badge_box.left + 1, badge_box.top + 1, badge_box.right - 1, badge_box.bottom - 1)
    items.append(
        fit_line("badge_text", PROMO_BADGE, True, g.sizes(BADGE_SIZES), inner, badge_ink,
                 Align.CENTRE, Align.CENTRE)
    )  # fmt: skip

    prior = fit_line(
        "prior_price", label.prior_price_text, False, g.sizes(PRIOR_SIZES), g.box(4, 68, 104, 94),
        Colour.BLACK, Align.START, Align.CENTRE, ellipsis=False,
    )  # fmt: skip
    items.append(prior)
    thickness = g.px(2)
    middle = (prior.ink.top + prior.ink.bottom) // 2
    strike = Box(prior.ink.left - 1, middle - thickness // 2, prior.ink.right + 1,
                 middle - thickness // 2 + thickness)  # fmt: skip
    items.append(LineItem("strike", strike, Colour.BLACK))

    until = f"{UNTIL_PREFIX} {label.promo_until_text}"
    items.append(fit_line("until", until, False, g.sizes(UNTIL_SIZES), g.box(4, 98, 104, 125),
                          Colour.BLACK, Align.START, Align.END))  # fmt: skip

    items += fit_price_with_unit(
        label.price_text, label.unit_price_text, g.sizes(PRICE_SIZES), g.box(110, 46, 292, 125),
        Colour.RED, Colour.BLACK, g.px(3),
    )  # fmt: skip
    return items


def unassigned(label: LabelData, tag_type: TagType) -> list[Item]:
    """The tag's short code in large type and the tag model, for binding."""
    _need(label, "short_code", "tag_model")
    assert label.short_code is not None
    assert label.tag_model is not None
    g = _Grid(tag_type)
    code = fit_line("short_code", label.short_code, True, g.sizes(SHORT_CODE_SIZES),
                    g.box(4, 6, 292, 98), Colour.BLACK, Align.CENTRE, Align.CENTRE,
                    ellipsis=False)  # fmt: skip
    model = fit_line("tag_model", label.tag_model, False, g.sizes(TAG_MODEL_SIZES),
                     g.box(4, 102, 292, 125), Colour.BLACK, Align.CENTRE, Align.END)  # fmt: skip
    return [code, model]


TEMPLATES: dict[TemplateKey, Callable[[LabelData, TagType], list[Item]]] = {
    TemplateKey.STANDARD: standard,
    TemplateKey.UNIT_PRICE: unit_price,
    TemplateKey.PROMO: promo,
    TemplateKey.UNASSIGNED: unassigned,
}


def layout(label: LabelData, tag_type: TagType) -> list[Item]:
    """Placed items for ``label`` on ``tag_type``, in drawing order."""
    return TEMPLATES[label.template](label, tag_type)

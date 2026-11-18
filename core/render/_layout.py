"""Layout primitives: boxes, placed text and the fitting rules of PLAN.md section 7.

Every text item is placed so that its 1-bit ink lies inside its box. Boxes of one
template never overlap, so a name can never run into the price area.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from core.render._fonts import ink_bbox, line_pitch
from core.render._palette import Colour

ELLIPSIS = "…"

#: The unit price's digits must be at least this fraction of the selling price's.
UNIT_PRICE_MIN_RATIO_NUM, UNIT_PRICE_MIN_RATIO_DEN = 7, 10


class Align(Enum):
    START = "start"
    CENTRE = "centre"
    END = "end"


@dataclass(frozen=True, slots=True)
class Box:
    """Pixel rectangle; ``right`` and ``bottom`` are exclusive."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def contains(self, other: Box) -> bool:
        return (
            self.left <= other.left
            and self.top <= other.top
            and other.right <= self.right
            and other.bottom <= self.bottom
        )

    def overlaps(self, other: Box) -> bool:
        return (
            self.left < other.right
            and other.left < self.right
            and self.top < other.bottom
            and other.top < self.bottom
        )


@dataclass(frozen=True, slots=True)
class TextItem:
    """One line of text. ``x``, ``y`` is the (left, ascender) origin it is drawn at;
    ``ink`` is where its pixels land; ``box`` is the region it was fitted into."""

    role: str
    text: str
    bold: bool
    size: int
    x: int
    y: int
    ink: Box
    box: Box
    colour: Colour


@dataclass(frozen=True, slots=True)
class RectItem:
    role: str
    box: Box
    colour: Colour


@dataclass(frozen=True, slots=True)
class LineItem:
    """A horizontal or vertical bar: a filled rectangle drawn after the text."""

    role: str
    box: Box
    colour: Colour


Item = TextItem | RectItem | LineItem


def _ink_width(text: str, bold: bool, size: int) -> int:
    left, _, right, _ = ink_bbox(text, bold, size)
    return right - min(left, 0)


def _fits_width(text: str, bold: bool, size: int, width: int) -> bool:
    return _ink_width(text, bold, size) <= width


def place(
    role: str,
    text: str,
    bold: bool,
    size: int,
    box: Box,
    colour: Colour,
    h_align: Align = Align.START,
    v_align: Align = Align.END,
) -> TextItem | None:
    """Place one line so its ink is aligned inside ``box``; ``None`` if it is too big."""
    left, top, right, bottom = ink_bbox(text, bold, size)
    width, height = right - left, bottom - top
    if width > box.width or height > box.height:
        return None
    ink_left = _aligned(box.left, box.width, width, h_align)
    ink_top = _aligned(box.top, box.height, height, v_align)
    ink = Box(ink_left, ink_top, ink_left + width, ink_top + height)
    return TextItem(role, text, bold, size, ink_left - left, ink_top - top, ink, box, colour)


def _aligned(start: int, room: int, size: int, align: Align) -> int:
    if align is Align.START:
        return start
    if align is Align.END:
        return start + room - size
    return start + (room - size) // 2


def truncate(text: str, bold: bool, size: int, width: int) -> str:
    """``text`` shortened with an ellipsis until it fits ``width``."""
    if _fits_width(text, bold, size, width):
        return text
    cut = text
    while cut:
        cut = cut[:-1].rstrip()
        if _fits_width(cut + ELLIPSIS, bold, size, width):
            return cut + ELLIPSIS
    return ELLIPSIS


def fit_line(
    role: str,
    text: str,
    bold: bool,
    sizes: Sequence[int],
    box: Box,
    colour: Colour,
    h_align: Align = Align.START,
    v_align: Align = Align.END,
    *,
    ellipsis: bool = True,
) -> TextItem:
    """The largest size in ``sizes`` (largest first) at which ``text`` fits ``box``.

    If none fits, the smallest size with the text truncated by an ellipsis, or
    ``ValueError`` when ``ellipsis`` is false (a price is never truncated)."""
    for size in sizes:
        item = place(role, text, bold, size, box, colour, h_align, v_align)
        if item is not None:
            return item
    smallest = sizes[-1]
    if ellipsis:
        item = place(
            role, truncate(text, bold, smallest, box.width), bold, smallest, box, colour,
            h_align, v_align,
        )  # fmt: skip
        if item is not None:
            return item
    raise ValueError(f"{role} does not fit its {box.width}x{box.height} area: {text!r}")


def _wrap(text: str, bold: bool, size: int, width: int) -> list[tuple[int, str]]:
    """Greedy word wrap of normalised ``text``: ``(start offset, line)`` pairs. A word
    wider than a line is broken between characters."""
    lines: list[tuple[int, str]] = []
    pos = 0
    while pos < len(text):
        rest = text[pos:]
        if _fits_width(rest, bold, size, width):
            lines.append((pos, rest))
            break
        cut = 0
        for index, char in enumerate(rest):
            if char == " ":
                if not _fits_width(rest[:index], bold, size, width):
                    break
                cut = index
        if cut > 0:
            lines.append((pos, rest[:cut]))
            pos += cut + 1
            continue
        chars = 1
        while chars < len(rest) and _fits_width(rest[: chars + 1], bold, size, width):
            chars += 1
        lines.append((pos, rest[:chars]))
        pos += chars
        while pos < len(text) and text[pos] == " ":
            pos += 1
    return lines


def _stack(
    role: str, lines: Sequence[str], bold: bool, size: int, box: Box, colour: Colour
) -> list[TextItem] | None:
    """Lines one pitch apart from the top of ``box``; ``None`` if the ink leaves it."""
    pitch = line_pitch(bold, size)
    items: list[TextItem] = []
    for number, line in enumerate(lines):
        left, top, right, bottom = ink_bbox(line, bold, size)
        x = box.left - min(left, 0)
        y = box.top + number * pitch
        ink = Box(x + left, y + top, x + right, y + bottom)
        items.append(TextItem(role, line, bold, size, x, y, ink, box, colour))
    if not items:
        return items
    shift = max(0, box.top - min(item.ink.top for item in items))
    if shift:
        items = [_moved(item, shift) for item in items]
    if all(box.contains(item.ink) for item in items):
        return items
    return None


def _moved(item: TextItem, dy: int) -> TextItem:
    ink = Box(item.ink.left, item.ink.top + dy, item.ink.right, item.ink.bottom + dy)
    return TextItem(
        item.role, item.text, item.bold, item.size, item.x, item.y + dy, ink, item.box,
        item.colour,
    )  # fmt: skip


def fit_name(
    text: str,
    bold: bool,
    sizes: Sequence[int],
    box: Box,
    colour: Colour = Colour.BLACK,
    max_lines: int = 2,
) -> list[TextItem]:
    """A product name in at most ``max_lines`` lines: the largest size in ``sizes``
    at which it fits whole, else the smallest size with the last line truncated by an
    ellipsis. The ink never leaves ``box``."""
    text = " ".join(text.split())
    if not text:
        return []
    for size in sizes:
        lines = _wrap(text, bold, size, box.width)
        if len(lines) <= max_lines:
            items = _stack("name", [line for _, line in lines], bold, size, box, colour)
            if items is not None:
                return items
    for lines_wanted in range(max_lines, 0, -1):
        smallest = sizes[-1]
        lines = _wrap(text, bold, smallest, box.width)
        kept = [line for _, line in lines[: lines_wanted - 1]]
        rest = text[lines[lines_wanted - 1][0] :] if len(lines) >= lines_wanted else ""
        if rest:
            kept.append(truncate(rest, bold, smallest, box.width))
        items = _stack("name", kept, bold, smallest, box, colour)
        if items is not None:
            return items
    raise ValueError(f"name does not fit its {box.width}x{box.height} area")


def digit_height(text: str, bold: bool, size: int) -> int:
    """Ink height of the integer digits of a price text ("12" of "12,90 €"): the
    character size the 70% unit-price rule compares."""
    digits = text.split(",", 1)[0].strip()
    _, top, _, bottom = ink_bbox(digits or text, bold, size)
    return bottom - top


def unit_ratio_ok(price_text: str, price_size: int, unit_text: str, unit_size: int) -> bool:
    price_h = digit_height(price_text, True, price_size)
    unit_h = digit_height(unit_text, True, unit_size)
    return unit_h * UNIT_PRICE_MIN_RATIO_DEN >= price_h * UNIT_PRICE_MIN_RATIO_NUM


def fit_price_with_unit(
    price_text: str,
    unit_text: str | None,
    sizes: Sequence[int],
    box: Box,
    price_colour: Colour,
    unit_colour: Colour,
    gap: int,
) -> list[TextItem]:
    """The selling price, and below it the unit price, both bold and right-aligned in
    ``box``. The price takes the largest size in ``sizes`` at which both fit while the
    unit price's digits stay at least 70% of the price's. Raises ``ValueError`` if no
    size fits: a price is never truncated."""
    if unit_text is None:
        return [
            fit_line(
                "price", price_text, True, sizes, box, price_colour, Align.END, Align.END,
                ellipsis=False,
            )
        ]  # fmt: skip
    for price_size in sizes:
        price_bbox = ink_bbox(price_text, True, price_size)
        price_h = price_bbox[3] - price_bbox[1]
        if price_bbox[2] - price_bbox[0] > box.width or price_h + gap > box.height:
            continue
        unit_size = next(
            (s for s in sorted(sizes) if unit_ratio_ok(price_text, price_size, unit_text, s)),
            None,
        )
        if unit_size is None or unit_size > price_size:
            continue
        unit_box = Box(box.left, box.top + price_h + gap, box.right, box.bottom)
        unit = place(
            "unit_price", unit_text, True, unit_size, unit_box, unit_colour, Align.END, Align.END
        )
        if unit is None:
            continue
        price_box = Box(box.left, box.top, box.right, unit.ink.top - gap)
        price = place(
            "price", price_text, True, price_size, price_box, price_colour, Align.END, Align.END
        )
        if price is not None:
            return [price, unit]
    raise ValueError(f"price and unit price do not fit {box.width}x{box.height}")

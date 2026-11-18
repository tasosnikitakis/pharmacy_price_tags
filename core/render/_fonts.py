"""Bundled fonts (``core/render/fonts/``), loaded by path relative to this package.

System fonts are never used: the same files render the same pixels on every machine.
The basic layout engine is forced, so the result does not depend on whether Pillow was
built with libraqm.
"""

from __future__ import annotations

import io
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(__file__).resolve().parent / "fonts"
REGULAR_FILE = "DejaVuSansCondensed.ttf"
BOLD_FILE = "DejaVuSansCondensed-Bold.ttf"

#: Text measured and drawn in 1-bit mode: no anti-aliasing.
MONO = "1"

Font = ImageFont.FreeTypeFont


@cache
def _font_bytes(bold: bool) -> bytes:
    return (FONT_DIR / (BOLD_FILE if bold else REGULAR_FILE)).read_bytes()


@cache
def font(bold: bool, size: int) -> Font:
    """The bundled font at ``size`` pixels. Loaded from bytes, so a non-ASCII install
    path on Windows never reaches FreeType."""
    if size <= 0:
        raise ValueError(f"font size must be positive: {size}")
    return ImageFont.truetype(
        io.BytesIO(_font_bytes(bold)), size, layout_engine=ImageFont.Layout.BASIC
    )


@cache
def ink_bbox(text: str, bold: bool, size: int) -> tuple[int, int, int, int]:
    """Exact bounding box of the 1-bit pixels of ``text`` drawn with its origin (left,
    ascender) at (0, 0); ``(0, 0, 0, 0)`` when nothing is drawn.

    ``FreeTypeFont.getbbox`` gives the glyph cells, which can be a few pixels larger
    than the ink, so the text is drawn into a scratch mask and measured."""
    face = font(bold, size)
    left, top, right, bottom = (int(v) for v in face.getbbox(text, mode=MONO, anchor="la"))
    pad = 4 + size // 4  # margin in case a glyph's ink leaves its cell
    mask = Image.new("1", (right - left + 2 * pad, bottom - top + 2 * pad), 0)
    draw = ImageDraw.Draw(mask)
    draw.fontmode = MONO
    draw.text((pad - left, pad - top), text, fill=1, font=face, anchor="la")
    ink = mask.getbbox()
    if ink is None:
        return 0, 0, 0, 0
    dx, dy = left - pad, top - pad
    return ink[0] + dx, ink[1] + dy, ink[2] + dx, ink[3] + dy


def line_pitch(bold: bool, size: int) -> int:
    """Distance between baselines of consecutive lines: ascent plus descent."""
    ascent, descent = font(bold, size).getmetrics()
    return int(ascent) + int(descent)

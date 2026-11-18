"""Tag palettes as indexed images.

Index order is ``shared.tag_types.PALETTE_COLOURS``; index 0 is white.
"""

from __future__ import annotations

from enum import Enum

from PIL import Image

from shared.tag_types import PALETTE_COLOURS, Palette


class Colour(Enum):
    """A colour a template asks for, by RGB value."""

    WHITE = (255, 255, 255)
    BLACK = (0, 0, 0)
    RED = (255, 0, 0)
    YELLOW = (255, 255, 0)


def has_colour(palette: Palette, colour: Colour) -> bool:
    return colour.value in PALETTE_COLOURS[palette]


def colour_index(palette: Palette, colour: Colour) -> int:
    """Palette index of ``colour``. Raises ``ValueError`` if the palette lacks it."""
    colours = PALETTE_COLOURS[palette]
    if colour.value not in colours:
        raise ValueError(f"palette {palette.value} has no {colour.name.lower()}")
    return colours.index(colour.value)


def new_image(palette: Palette, width: int, height: int) -> Image.Image:
    """A white ``P`` image whose palette holds exactly the tag's colours, in order."""
    image = Image.new("P", (width, height), 0)
    image.putpalette([channel for rgb in PALETTE_COLOURS[palette] for channel in rgb], "RGB")
    return image

"""Tag hardware description shared by the renderer (core) and the drivers (gateway).

The gateway must never import from ``core``, so the type both sides need lives here.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Palette(StrEnum):
    """Colours a tag can show. Order of colours in a palette PNG follows ``PALETTE_COLOURS``."""

    BW = "BW"
    BWR = "BWR"
    BWRY = "BWRY"


#: Exact RGB values, in palette-index order, for each palette. Index 0 is white.
PALETTE_COLOURS: dict[Palette, tuple[tuple[int, int, int], ...]] = {
    Palette.BW: ((255, 255, 255), (0, 0, 0)),
    Palette.BWR: ((255, 255, 255), (0, 0, 0), (255, 0, 0)),
    Palette.BWRY: ((255, 255, 255), (0, 0, 0), (255, 0, 0), (255, 255, 0)),
}


class TagType(BaseModel):
    """One kind of tag: vendor, model, native resolution and palette.

    Mirrors the ``tag_type`` table. ``key`` is a stable identifier used in render
    hashes and on the wire, e.g. ``"gicisky-2.9-bwr"``.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(min_length=1, max_length=64)
    vendor: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=64)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    palette: Palette

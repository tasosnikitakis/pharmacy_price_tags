"""Renderer contract (workstream B). Placeholders until implemented; golden-image
tests come with the templates."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from core.render import LabelData, TemplateKey, label_hash, render
from shared.tag_types import PALETTE_COLOURS, Palette, TagType

pytestmark = pytest.mark.xfail(raises=NotImplementedError, reason="workstream B")

TAG = TagType(
    key="gicisky-2.9-bwr", vendor="gicisky", model="2.9", width=296, height=128, palette=Palette.BWR
)
LABEL = LabelData(
    TemplateKey.STANDARD, name="ΑΣΠΙΡΊΝΗ Protect 100mg", brand="Bayer", price_text="4,50 €"
)


def test_same_label_same_hash_and_bytes() -> None:
    assert label_hash(LABEL, TAG) == label_hash(LABEL, TAG)
    a, b = render(LABEL, TAG), render(LABEL, TAG)
    assert (a.hash, a.png) == (b.hash, b.png)


def test_output_is_native_size_and_only_palette_colours() -> None:
    out = render(LABEL, TAG)
    image = Image.open(io.BytesIO(out.png)).convert("RGB")
    assert image.size == (296, 128)
    allowed = set(PALETTE_COLOURS[Palette.BWR])
    assert {rgb for _, rgb in image.getcolors(maxcolors=1 << 16) or []} <= allowed

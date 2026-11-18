"""Entry points of the renderer."""

from __future__ import annotations

from core.render.label import LabelData, Render
from shared.tag_types import TagType


def label_hash(label: LabelData, tag_type: TagType) -> str:
    """``sha256(template key + template version + tag type + LabelData)`` as hex.

    Uses a canonical serialisation (sorted keys, UTF-8, no whitespace) so the same
    inputs give the same hash on every machine and Python version.
    """
    raise NotImplementedError


def render(label: LabelData, tag_type: TagType) -> Render:
    """Draw ``label`` at the tag's native size onto an indexed image with exactly the
    tag's palette, no anti-aliasing and no dithering, and return it as PNG.

    Deterministic: the same inputs give the same hash and the same bytes.
    Raises ``ValueError`` if a field the template needs is missing, or if the template
    needs a colour the palette lacks (promo red on a ``BW`` tag).
    """
    raise NotImplementedError

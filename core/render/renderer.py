"""Entry points of the renderer."""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import fields
from enum import Enum

from PIL import Image, ImageDraw

from core.render._fonts import MONO, font
from core.render._layout import TextItem
from core.render._palette import colour_index, new_image
from core.render.label import TEMPLATE_VERSIONS, LabelData, Render
from core.render.templates import layout
from shared.tag_types import TagType


def label_hash(label: LabelData, tag_type: TagType) -> str:
    """``sha256(template key + template version + tag type + LabelData)`` as hex.

    Uses a canonical serialisation (sorted keys, UTF-8, no whitespace) so the same
    inputs give the same hash on every machine and Python version.
    """
    payload = {
        "template": label.template.value,
        "template_version": TEMPLATE_VERSIONS[label.template],
        "tag_type": tag_type.model_dump(mode="json"),
        "label": {f.name: _plain(getattr(label, f.name)) for f in fields(label)},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render(label: LabelData, tag_type: TagType) -> Render:
    """Draw ``label`` at the tag's native size onto an indexed image with exactly the
    tag's palette, no anti-aliasing and no dithering, and return it as PNG.

    Deterministic: the same inputs give the same hash and the same bytes.
    Raises ``ValueError`` if a field the template needs is missing, or if the template
    needs a colour the palette lacks (promo red on a ``BW`` tag).
    """
    palette = tag_type.palette
    image = new_image(palette, tag_type.width, tag_type.height)
    draw = ImageDraw.Draw(image)
    draw.fontmode = MONO  # 1-bit glyphs: every pixel is a palette index
    for item in layout(label, tag_type):
        index = colour_index(palette, item.colour)
        if isinstance(item, TextItem):
            draw.text(
                (item.x, item.y), item.text, fill=index, font=font(item.bold, item.size),
                anchor="la",
            )  # fmt: skip
        else:
            box = item.box
            draw.rectangle((box.left, box.top, box.right - 1, box.bottom - 1), fill=index)
    return Render(
        hash=label_hash(label, tag_type),
        tag_type_key=tag_type.key,
        width=tag_type.width,
        height=tag_type.height,
        png=encode_png(image),
    )


def encode_png(image: Image.Image) -> bytes:
    """PNG bytes with every option that affects the output set explicitly: no
    optimisation pass, fixed compression, no metadata chunks. The palette written is
    exactly the image's palette, so its length is the tag's number of colours."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False, compress_level=9)
    return buffer.getvalue()


def _plain(value: object) -> object:
    return value.value if isinstance(value, Enum) else value

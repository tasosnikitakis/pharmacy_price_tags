"""Label rendering (PLAN.md section 7). Owned by workstream B.

``render`` and the types in ``label`` are contracts: changes go through the lead agent.

``label_hash`` and ``render`` are imported lazily, so that ``core.domain`` can use the
types in ``core.render.label`` without loading Pillow, fonts or files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.render.label import TEMPLATE_VERSIONS, LabelData, Render, TemplateKey

if TYPE_CHECKING:
    from core.render.renderer import label_hash, render

__all__ = ["TEMPLATE_VERSIONS", "LabelData", "Render", "TemplateKey", "label_hash", "render"]


def __getattr__(name: str) -> Any:
    if name in ("label_hash", "render"):
        from core.render import renderer

        return getattr(renderer, name)
    raise AttributeError(name)

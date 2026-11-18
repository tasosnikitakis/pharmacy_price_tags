"""Label rendering (PLAN.md section 7). Owned by workstream B.

``render`` and the types in ``label`` are contracts: changes go through the lead agent.
"""

from core.render.label import TEMPLATE_VERSIONS, LabelData, Render, TemplateKey
from core.render.renderer import label_hash, render

__all__ = ["TEMPLATE_VERSIONS", "LabelData", "Render", "TemplateKey", "label_hash", "render"]

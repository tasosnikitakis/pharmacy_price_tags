"""What a label shows, and the rendered result."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TemplateKey(StrEnum):
    STANDARD = "standard"
    UNIT_PRICE = "unit_price"
    PROMO = "promo"
    UNASSIGNED = "unassigned"


#: Current version of each code-defined template. Bump on any visual change;
#: the version is part of the render hash.
TEMPLATE_VERSIONS: dict[TemplateKey, int] = {
    TemplateKey.STANDARD: 1,
    TemplateKey.UNIT_PRICE: 1,
    TemplateKey.PROMO: 1,
    TemplateKey.UNASSIGNED: 1,
}


@dataclass(frozen=True, slots=True)
class LabelData:
    """Everything one label shows, already formatted by ``core.domain.pricing``.

    The renderer only lays this out; it makes no price decisions. Which fields are
    required depends on ``template``:

    - ``standard``: ``name``, ``price_text``
    - ``unit_price``: as standard, plus ``unit_price_text``
    - ``promo``: ``name``, ``price_text`` (the promo price), ``prior_price_text``,
      ``promo_until_text``
    - ``unassigned``: ``short_code``, ``tag_model``
    """

    template: TemplateKey
    name: str = ""
    brand: str | None = None
    price_text: str | None = None  # "12,90 €"
    unit_price_text: str | None = None  # "22,50 €/kg"
    prior_price_text: str | None = None  # promo only, shown struck through
    promo_until_text: str | None = None  # promo only, last day in effect, "30/11"
    short_code: str | None = None  # unassigned only
    tag_model: str | None = None  # unassigned only


@dataclass(frozen=True, slots=True)
class Render:
    """A rendered label: an indexed PNG whose palette is exactly the tag's colours."""

    hash: str  # sha256 hex, see ``label_hash``
    tag_type_key: str
    width: int
    height: int
    png: bytes

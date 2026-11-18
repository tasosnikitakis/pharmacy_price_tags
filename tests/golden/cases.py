"""Golden-image cases: synthetic Greek fixtures, one PNG per case in this directory.

Covers every template; accented capitals; long names (wrapped, shrunk, truncated, and a
single word too long for a line); prices from 0,50 € to 999,99 €; BWR, BWRY and BW.
"""

# Greek fixtures mix Greek and Latin letters that look alike, on purpose.
# ruff: noqa: RUF001

from __future__ import annotations

from pathlib import Path

from core.render import LabelData, TemplateKey
from shared.tag_types import Palette, TagType

GOLDEN_DIR = Path(__file__).resolve().parent

BWR = TagType(
    key="gicisky-2.9-bwr", vendor="gicisky", model="2.9", width=296, height=128, palette=Palette.BWR
)
BWRY = TagType(
    key="gicisky-2.9-bwry",
    vendor="gicisky",
    model="2.9",
    width=296,
    height=128,
    palette=Palette.BWRY,
)
BW = TagType(
    key="gicisky-2.9-bw", vendor="gicisky", model="2.9", width=296, height=128, palette=Palette.BW
)

LONG_NAME = (
    "ΆΡΩΜΑ ΕΛΑΙΟΛΑΔΟΥ Ενυδατική κρέμα προσώπου για ξηρές επιδερμίδες "
    "με υαλουρονικό οξύ και βιταμίνη Ε 50ml"
)
LONG_WORD = "ΠΟΛΥΒΙΤΑΜΙΝΟΥΧΟΣΥΜΠΛΗΡΩΜΑΔΙΑΤΡΟΦΗΣΓΙΑΕΝΗΛΙΚΕΣΚΑΙΠΑΙΔΙΑ"
ACCENTED_CAPS = "ΈΛΑΙΟ ΑΜΥΓΔΑΛΟΥ ΚΑΘΑΡΌ Ϊ Ϋ Ά Ή Ί Ό Ύ Ώ"

CASES: dict[str, tuple[LabelData, TagType]] = {
    "standard_bwr_short": (
        LabelData(
            TemplateKey.STANDARD, name="ΑΣΠΙΡΊΝΗ Protect 100mg", brand="Bayer", price_text="4,50 €"
        ),
        BWR,
    ),
    "standard_bwr_min_price_no_brand": (
        LabelData(TemplateKey.STANDARD, name="Γάζες αποστειρωμένες 10x10", price_text="0,50 €"),
        BWR,
    ),
    "standard_bwr_long_name_max_price": (
        LabelData(
            TemplateKey.STANDARD,
            name=LONG_NAME,
            brand="Κορρές Φυσικά Προϊόντα",
            price_text="999,99 €",
        ),
        BWR,
    ),
    "standard_bwr_long_word": (
        LabelData(TemplateKey.STANDARD, name=LONG_WORD, brand="Ίον", price_text="12,90 €"),
        BWR,
    ),
    "standard_bwr_accented_caps": (
        LabelData(
            TemplateKey.STANDARD, name=ACCENTED_CAPS, brand="ΆΛΦΑ ΩΜΈΓΑ", price_text="99,90 €"
        ),
        BWR,
    ),
    "unit_price_bwr_grams": (
        LabelData(
            TemplateKey.UNIT_PRICE,
            name="Βρεφικό γάλα σε σκόνη Νο 1",
            brand="ΝΟΥΝΟΥ",
            price_text="0,50 €",
            unit_price_text="812,50 €/kg",
        ),
        BWR,
    ),
    "unit_price_bwr_long_name_max_price": (
        LabelData(
            TemplateKey.UNIT_PRICE,
            name=LONG_NAME,
            brand="Κορρές",
            price_text="999,99 €",
            unit_price_text="4999,95 €/l",
        ),
        BWR,
    ),
    "unit_price_bwr_pieces": (
        LabelData(
            TemplateKey.UNIT_PRICE,
            name="Επιθέματα ΆΜΕΣΗΣ ΒΟΉΘΕΙΑΣ 20 τεμ.",
            price_text="3,20 €",
            unit_price_text="0,16 €/τεμ.",
        ),
        BWR,
    ),
    "promo_bwr": (
        LabelData(
            TemplateKey.PROMO,
            name="Κρέμα χεριών Ελιά",
            brand="Korres",
            price_text="8,00 €",
            prior_price_text="10,00 €",
            promo_until_text="30/11",
        ),
        BWR,
    ),
    "promo_bwr_min_price": (
        LabelData(
            TemplateKey.PROMO,
            name="Μάσκα προσώπου ΎΦΑΣΜΑΤΟΣ",
            price_text="0,50 €",
            prior_price_text="0,99 €",
            promo_until_text="01/01",
        ),
        BWR,
    ),
    "promo_bwr_unit_long_name": (
        LabelData(
            TemplateKey.PROMO,
            name=LONG_NAME,
            price_text="999,99 €",
            prior_price_text="999,99 €",
            promo_until_text="31/12",
            unit_price_text="19999,80 €/l",
        ),
        BWR,
    ),
    "promo_bwr_unit_accented_caps": (
        LabelData(
            TemplateKey.PROMO,
            name=ACCENTED_CAPS,
            price_text="7,20 €",
            prior_price_text="9,00 €",
            promo_until_text="15/08",
            unit_price_text="36,00 €/kg",
        ),
        BWR,
    ),
    "unassigned_bwr": (
        LabelData(TemplateKey.UNASSIGNED, short_code="A7K3", tag_model="gicisky 2.9"),
        BWR,
    ),
    "promo_bwry": (
        LabelData(
            TemplateKey.PROMO,
            name="Αντηλιακό ΠΡΟΣΏΠΟΥ SPF50",
            price_text="14,90 €",
            prior_price_text="18,50 €",
            promo_until_text="31/08",
            unit_price_text="298,00 €/l",
        ),
        BWRY,
    ),
    "standard_bwry": (
        LabelData(
            TemplateKey.STANDARD, name="Θερμόμετρο ψηφιακό", brand="Ώμέγα", price_text="25,00 €"
        ),
        BWRY,
    ),
    "standard_bw": (
        LabelData(
            TemplateKey.STANDARD,
            name="Παυσίπονο ΈΝΤΟΝΟ 20 δισκία",
            brand="Ήπιο",
            price_text="5,40 €",
        ),
        BW,
    ),
    "unassigned_bw": (
        LabelData(TemplateKey.UNASSIGNED, short_code="Q2", tag_model="gicisky 2.9 BW"),
        BW,
    ),
}


def golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.png"

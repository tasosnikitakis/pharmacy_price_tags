"""Catalogue rules for imports (PLAN.md section 6). Owned by workstream A.

Pure functions: parsing cell values from a Greek ERP export, matching rows to the
catalogue by EAN and building the preview diff. No database, HTTP or file access;
``core.importers`` reads the file and ``core.services.catalogue`` writes the result.

Money is integer cents and pack sizes are integer thousandths of ``pack_unit``: no
value passes through a float.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from core.domain.types import PackUnit

#: Longest EAN/GTIN the ``product.ean`` column holds (GTIN-14).
EAN_MAX_LEN = 14
#: Shortest code accepted (EAN-8). Shorter codes are almost always a broken cell.
EAN_MIN_LEN = 8
NAME_MAX_LEN = 300
BRAND_MAX_LEN = 200
#: ``product.pack_qty_milli`` is a 32-bit integer.
PACK_QTY_MILLI_MAX = 2**31 - 1
#: ``price.regular_cents`` is a 32-bit integer.
PRICE_CENTS_MAX = 2**31 - 1


class CellError(ValueError):
    """A cell value cannot be used; the message is the reason shown to the admin."""


# --- Cell parsing ---------------------------------------------------------------------

_NUMBER = re.compile(r"^\d+(?:[.,]\d+)*$")


def _split_number(text: str, max_decimals: int, what: str) -> tuple[str, str]:
    """Split a decimal number into integer and fraction digits.

    Accepts a decimal comma (``12,90``) or point (``12.90``), and a thousands
    separator when both appear (``1.290,50`` or ``1,290.50``: the last one is the
    decimal separator). A lone ``.`` followed by exactly three digits (``1.000``) is
    refused as ambiguous, because Greek exports use ``.`` for thousands.
    """
    if not _NUMBER.match(text):
        raise CellError(f"{what} is not a number: {text!r}")
    commas, dots = text.count(","), text.count(".")
    if commas and dots:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        if text.count(decimal) > 1:
            raise CellError(f"{what} is not a number: {text!r}")
        whole, frac = text.split(decimal)
        groups = whole.split(thousands)
        if not 1 <= len(groups[0]) <= 3 or any(len(g) != 3 for g in groups[1:]):
            raise CellError(f"{what} has misplaced thousands separators: {text!r}")
        whole = "".join(groups)
    elif commas or dots:
        sep = "," if commas else "."
        if text.count(sep) > 1:
            raise CellError(f"{what} is not a number: {text!r}")
        whole, frac = text.split(sep)
        if sep == "." and len(frac) == 3 and whole.strip("0") != "":
            raise CellError(f"{what} is ambiguous (thousands or decimals?): {text!r}")
    else:
        whole, frac = text, ""
    if len(frac) > max_decimals:
        raise CellError(f"{what} has more than {max_decimals} decimals: {text!r}")
    return whole, frac


def parse_price_cents(text: str) -> int:
    """Parse a VAT-inclusive retail price into integer cents, exactly.

    ``"12,90"`` -> 1290, ``"12,9"`` -> 1290, ``"12"`` -> 1200, ``"1.290,50"`` -> 129050,
    ``"12,90 €"`` -> 1290. Raises ``CellError`` for an empty, unparseable, zero or
    negative price, or one with more than two decimals (never rounded silently). A
    zero price in an ERP export almost always means "no price set", so it is refused
    rather than put on a shelf label.
    """
    cleaned = text.replace("€", "").replace("\u00a0", " ").strip().replace(" ", "")
    if not cleaned:
        raise CellError("price is empty")
    if cleaned.startswith("-"):
        raise CellError(f"price is negative: {text.strip()!r}")
    cleaned = cleaned.removeprefix("+")
    whole, frac = _split_number(cleaned, 2, "price")
    cents = int(whole) * 100 + int(frac.ljust(2, "0") or "0")
    if cents == 0:
        raise CellError(f"zero price, probably no price in the ERP: {text.strip()!r}")
    if cents > PRICE_CENTS_MAX:
        raise CellError(f"price is too large: {text.strip()!r}")
    return cents


def _fold(text: str) -> str:
    """Case- and accent-insensitive form of ``text`` for matching labels and units."""
    decomposed = unicodedata.normalize("NFD", text.strip().casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


#: Unit spellings seen in Greek and English exports, folded with ``_fold``, mapped to
#: the stored unit and the factor from the written unit to that one.
_UNITS: dict[str, tuple[PackUnit, int]] = {
    **{u: (PackUnit.GRAM, 1) for u in ("g", "gr", "grs", "γρ", "γρμ", "γραμμαρια")},  # noqa: RUF001
    **{u: (PackUnit.GRAM, 1000) for u in ("kg", "kgr", "κιλο", "κιλα")},
    **{u: (PackUnit.MILLILITRE, 1) for u in ("ml", "μλ")},
    **{u: (PackUnit.MILLILITRE, 1000) for u in ("l", "lt", "ltr", "λτ", "λιτρο", "λιτρα")},
    **{
        u: (PackUnit.PIECE, 1)
        for u in ("pieces", "piece", "pcs", "pc", "τεμ", "τεμαχια", "τεμαχιο", "τμχ")
    },
}


def parse_pack_unit(text: str) -> tuple[PackUnit, int]:
    """Map a unit cell to ``(PackUnit, factor)``; ``kg`` and ``l`` scale by 1000.

    Case, accents and a trailing dot are ignored (upper-case Greek "tem." -> pieces).
    """
    key = _fold(text).rstrip(".")
    if key not in _UNITS:
        raise CellError(f"unknown pack unit: {text.strip()!r}")
    return _UNITS[key]


_QTY_WITH_UNIT = re.compile(r"^\s*(\d[\d.,]*)\s*([^\d\s.,][^\d]*)?$")


def parse_pack(qty_text: str, unit_text: str = "") -> tuple[int, PackUnit] | None:
    """Parse a pack size into ``(pack_qty_milli, pack_unit)``, exactly.

    ``("4,8", "g")`` -> ``(4800, g)``; ``("200 ml", "")`` -> ``(200000, ml)``;
    ``("1,5", "lt")`` -> ``(1500000, ml)``; ``("30", "τεμ")`` -> ``(30000, pieces)``.
    The unit may be in its own cell or follow the number. Returns ``None`` when the
    quantity cell is empty (a unit alone says nothing). Raises ``CellError`` for an
    unparseable or zero quantity, a missing or unknown unit, or more precision than
    thousandths of the stored unit.
    """
    qty_text = qty_text.replace("\u00a0", " ").strip()
    unit_text = unit_text.strip()
    if not qty_text:
        return None
    match = _QTY_WITH_UNIT.match(qty_text)
    if not match:
        raise CellError(f"pack size is not a number: {qty_text!r}")
    number, inline_unit = match.group(1), (match.group(2) or "").strip()
    if inline_unit and unit_text and parse_pack_unit(inline_unit) != parse_pack_unit(unit_text):
        raise CellError(f"pack size {qty_text!r} disagrees with unit {unit_text!r}")
    written_unit = unit_text or inline_unit
    if not written_unit:
        raise CellError(f"pack size has no unit: {qty_text!r}")
    unit, factor = parse_pack_unit(written_unit)
    whole, frac = _split_number(number, 6, "pack size")
    # Exact: thousandths of the written unit, times the unit factor, as integers.
    scale = 1000 * factor
    frac_digits = frac or "0"
    numerator = (int(whole) * 10 ** len(frac_digits) + int(frac_digits)) * scale
    milli, remainder = divmod(numerator, 10 ** len(frac_digits))
    if remainder:
        raise CellError(f"pack size is more precise than 1/1000 {unit.value}: {qty_text!r}")
    if milli <= 0:
        raise CellError(f"pack size must be positive: {qty_text!r}")
    if milli > PACK_QTY_MILLI_MAX:
        raise CellError(f"pack size is too large: {qty_text!r}")
    return milli, unit


def normalise_ean(text: str) -> str:
    """Strip spaces from an EAN cell and check it is 8 to 14 digits.

    Leading zeros are kept (the EAN is text). The check digit is not verified, so
    pharmacy-internal codes of that length are accepted. A spreadsheet's scientific
    notation (``5.20123E+12``) is refused, since the digits are already lost.
    """
    cleaned = text.replace("\u00a0", "").replace(" ", "").strip()
    if not cleaned:
        raise CellError("EAN is empty")
    if not cleaned.isascii() or not cleaned.isdigit():
        raise CellError(f"EAN is not all digits: {text.strip()!r}")
    if not EAN_MIN_LEN <= len(cleaned) <= EAN_MAX_LEN:
        raise CellError(f"EAN must have {EAN_MIN_LEN} to {EAN_MAX_LEN} digits: {cleaned!r}")
    return cleaned


def normalise_text(text: str) -> str:
    """Trim a text cell and collapse runs of whitespace, so re-exports compare equal."""
    return " ".join(unicodedata.normalize("NFC", text).split())


# --- Rows -----------------------------------------------------------------------------


class Column(StrEnum):
    """Logical columns an import profile maps to the file's header names."""

    EAN = "ean"
    NAME = "name"
    PRICE = "price"
    BRAND = "brand"
    PACK_QTY = "pack_qty"
    PACK_UNIT = "pack_unit"


REQUIRED_COLUMNS = (Column.EAN, Column.NAME, Column.PRICE)


@dataclass(frozen=True, slots=True)
class ImportRow:
    """One accepted row of an import file.

    ``brand`` and the pack fields are ``None`` when the file has no value for them;
    then the stored value is kept (an empty cell never clears catalogue data).
    """

    line: int
    ean: str
    name: str
    regular_cents: int
    brand: str | None = None
    pack_qty_milli: int | None = None
    pack_unit: PackUnit | None = None


@dataclass(frozen=True, slots=True)
class RejectedRow:
    """A row that is not imported, with the reason shown in the preview."""

    line: int
    reason: str
    ean: str | None = None  # the normalised EAN, when that cell itself was valid


def parse_row(line: int, cells: Mapping[Column, str]) -> ImportRow | RejectedRow:
    """Turn one row's cells (logical column -> raw text) into an accepted or rejected
    row. Columns the profile does not map are simply absent from ``cells``."""
    try:
        ean = normalise_ean(cells.get(Column.EAN, ""))
    except CellError as exc:
        return RejectedRow(line, str(exc))
    try:
        name = normalise_text(cells.get(Column.NAME, ""))
        if not name:
            raise CellError("name is empty")
        if len(name) > NAME_MAX_LEN:
            raise CellError(f"name is longer than {NAME_MAX_LEN} characters")
        cents = parse_price_cents(cells.get(Column.PRICE, ""))
        brand = normalise_text(cells.get(Column.BRAND, "")) or None
        if brand is not None and len(brand) > BRAND_MAX_LEN:
            raise CellError(f"brand is longer than {BRAND_MAX_LEN} characters")
        pack = parse_pack(cells.get(Column.PACK_QTY, ""), cells.get(Column.PACK_UNIT, ""))
    except CellError as exc:
        return RejectedRow(line, str(exc), ean)
    qty, unit = pack if pack else (None, None)
    return ImportRow(line, ean, name, cents, brand, qty, unit)


def reject_duplicates(
    rows: Iterable[ImportRow | RejectedRow],
) -> tuple[list[ImportRow], list[RejectedRow]]:
    """Split parsed rows into accepted and rejected, rejecting duplicate EANs.

    Every row of an EAN that appears more than once is rejected, naming all its
    lines: the file does not say which one is right, so none is applied. A rejected
    row with a valid EAN counts towards duplicates as well.
    """
    rows = list(rows)
    lines_by_ean: dict[str, list[int]] = {}
    for row in rows:
        if row.ean is not None:
            lines_by_ean.setdefault(row.ean, []).append(row.line)
    accepted: list[ImportRow] = []
    rejected: list[RejectedRow] = []
    for row in rows:
        lines = lines_by_ean.get(row.ean, []) if row.ean is not None else []
        if len(lines) > 1:
            listed = ", ".join(str(n) for n in lines)
            reason = f"duplicate EAN {row.ean} (lines {listed})"
            if isinstance(row, RejectedRow):
                reason = f"{reason}; {row.reason}"
            rejected.append(RejectedRow(row.line, reason, row.ean))
        elif isinstance(row, RejectedRow):
            rejected.append(row)
        else:
            accepted.append(row)
    return accepted, sorted(rejected, key=lambda r: r.line)


# --- Diff -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    """What the diff needs to know about one product already in the catalogue."""

    product_id: int
    ean: str
    name: str
    brand: str | None
    pack_qty_milli: int | None
    pack_unit: PackUnit | None
    regular_cents: int | None  # None if the product has no price row yet
    manually_changed_at: datetime | None = None
    active: bool = True


class RowStatus(StrEnum):
    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class FieldChange:
    """A product field the import changes (not the price)."""

    field: str  # "name", "brand", "pack_qty_milli" or "pack_unit"
    old: object
    new: object


@dataclass(frozen=True, slots=True)
class DiffEntry:
    """One accepted row matched against the catalogue."""

    status: RowStatus
    row: ImportRow
    product_id: int | None = None  # None for a new product
    old_regular_cents: int | None = None
    field_changes: tuple[FieldChange, ...] = ()
    # The product's price was edited by hand since the last applied import, so the
    # preview shows the difference from the ERP (section 6).
    manually_changed: bool = False

    @property
    def price_changed(self) -> bool:
        return self.status is RowStatus.NEW or self.old_regular_cents != self.row.regular_cents


@dataclass(frozen=True, slots=True)
class MissingProduct:
    """An active catalogue product that is not in the file. It is listed, never deleted."""

    product_id: int
    ean: str
    name: str


@dataclass(frozen=True, slots=True)
class ImportDiff:
    new: list[DiffEntry] = field(default_factory=list)
    changed: list[DiffEntry] = field(default_factory=list)
    unchanged: list[DiffEntry] = field(default_factory=list)
    rejected: list[RejectedRow] = field(default_factory=list)
    missing: list[MissingProduct] = field(default_factory=list)

    @property
    def manually_changed(self) -> list[DiffEntry]:
        """Matched products edited by hand since the last import, in file order."""
        entries = [*self.changed, *self.unchanged]
        return sorted((e for e in entries if e.manually_changed), key=lambda e: e.row.line)

    def counts(self) -> dict[str, int]:
        """The ``import_run.counts`` shape, plus price changes and manual flags."""
        return {
            "new": len(self.new),
            "changed": len(self.changed),
            "unchanged": len(self.unchanged),
            "rejected": len(self.rejected),
            "missing": len(self.missing),
            "price_changed": sum(1 for e in self.changed if e.price_changed),
            "manually_changed": len(self.manually_changed),
        }


def _field_changes(row: ImportRow, entry: CatalogueEntry) -> tuple[FieldChange, ...]:
    changes: list[FieldChange] = []
    if row.name != entry.name:
        changes.append(FieldChange("name", entry.name, row.name))
    if row.brand is not None and row.brand != entry.brand:
        changes.append(FieldChange("brand", entry.brand, row.brand))
    if row.pack_qty_milli is not None:
        if row.pack_qty_milli != entry.pack_qty_milli:
            changes.append(FieldChange("pack_qty_milli", entry.pack_qty_milli, row.pack_qty_milli))
        if row.pack_unit != entry.pack_unit:
            changes.append(FieldChange("pack_unit", entry.pack_unit, row.pack_unit))
    return tuple(changes)


def build_diff(
    rows: Sequence[ImportRow],
    rejected: Sequence[RejectedRow],
    catalogue: Iterable[CatalogueEntry],
) -> ImportDiff:
    """Match accepted rows to the catalogue by EAN and classify them.

    ``rows`` must already be free of duplicate EANs (``reject_duplicates``). A row is
    *unchanged* when its price and every field it carries equal the catalogue's, so a
    re-import of the same file shows no changes. Active catalogue products whose EAN
    appears nowhere in the file, not even in a rejected row, are listed as missing.
    """
    by_ean = {entry.ean: entry for entry in catalogue}
    new: list[DiffEntry] = []
    changed: list[DiffEntry] = []
    unchanged: list[DiffEntry] = []
    for row in rows:
        entry = by_ean.get(row.ean)
        if entry is None:
            new.append(DiffEntry(RowStatus.NEW, row))
            continue
        fields = _field_changes(row, entry)
        status = (
            RowStatus.UNCHANGED
            if not fields and entry.regular_cents == row.regular_cents
            else RowStatus.CHANGED
        )
        diff_entry = DiffEntry(
            status,
            row,
            product_id=entry.product_id,
            old_regular_cents=entry.regular_cents,
            field_changes=fields,
            manually_changed=entry.manually_changed_at is not None,
        )
        (unchanged if status is RowStatus.UNCHANGED else changed).append(diff_entry)
    in_file = {row.ean for row in rows} | {r.ean for r in rejected if r.ean is not None}
    missing = sorted(
        (
            MissingProduct(e.product_id, e.ean, e.name)
            for e in by_ean.values()
            if e.active and e.ean not in in_file
        ),
        key=lambda m: m.ean,
    )
    return ImportDiff(new, changed, unchanged, list(rejected), missing)

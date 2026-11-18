"""Import profiles: a saved column mapping plus file settings (section 6, step 1).

A profile maps the logical columns of ``core.domain.catalogue.Column`` to header
names in the ERP's export, e.g. ``{"ean": "Barcode", "name": "Περιγραφή",
"price": "Λιανική"}``. It is stored as ``import_profile.mapping`` (JSONB), with
``encoding`` and ``delimiter`` (``None``: detect).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from core.domain.catalogue import (
    REQUIRED_COLUMNS,
    Column,
    ImportRow,
    RejectedRow,
    fold_text,
    parse_row,
    reject_duplicates,
)
from core.importers.csv_file import (
    DELIMITERS,
    ImportFormatError,
    Table,
    canonical_encoding,
    read_table,
)


def _key(header: str) -> str:
    """Header names match ignoring case, surrounding and repeated whitespace, a stray
    byte-order mark and Greek accents (tonos, dialytika)."""
    return fold_text(header.replace("\ufeff", ""))


@dataclass(frozen=True, slots=True)
class ImportSettings:
    """Everything needed to read one ERP's export."""

    mapping: dict[Column, str]  # logical column -> header name in the file
    encoding: str | None = None  # "utf-8" or "cp1253"; None: detect
    delimiter: str | None = None  # ";", "," or tab; None: detect

    @classmethod
    def create(
        cls,
        mapping: Mapping[str, object],
        encoding: str | None = None,
        delimiter: str | None = None,
    ) -> ImportSettings:
        """Validate a mapping as stored in ``import_profile.mapping``.

        Raises ``ValueError`` for unknown or missing required columns, empty or
        repeated header names, or an unsupported encoding or delimiter.
        """
        unknown = sorted(set(mapping) - {c.value for c in Column})
        if unknown:
            raise ValueError(f"unknown columns in mapping: {', '.join(unknown)}")
        parsed: dict[Column, str] = {}
        for column in Column:
            value = mapping.get(column.value)
            if value is None or value == "":
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"column {column.value!r} must map to a header name")
            parsed[column] = value.strip()
        missing = [c.value for c in REQUIRED_COLUMNS if c not in parsed]
        if missing:
            raise ValueError(f"mapping lacks required columns: {', '.join(missing)}")
        headers = [_key(h) for h in parsed.values()]
        if len(set(headers)) != len(headers):
            raise ValueError("two columns map to the same header")
        if encoding is not None:
            encoding = canonical_encoding(encoding)
        if delimiter is not None and delimiter not in DELIMITERS:
            raise ValueError(f"unsupported delimiter {delimiter!r}")
        return cls(parsed, encoding, delimiter)

    def mapping_json(self) -> dict[str, str]:
        """The mapping as stored in ``import_profile.mapping``."""
        return {column.value: header for column, header in self.mapping.items()}


@dataclass(frozen=True, slots=True)
class ParsedImport:
    """A file read with a profile: accepted rows (unique EANs) and rejected ones."""

    encoding: str
    delimiter: str
    rows: list[ImportRow]
    rejected: list[RejectedRow]


def column_positions(table: Table, settings: ImportSettings) -> dict[Column, int]:
    """Find each mapped column in the header (trimmed, case-insensitive).

    Raises ``ImportFormatError`` naming every mapped header the file lacks.
    """
    positions: dict[str, int] = {}
    for index, header in enumerate(table.header):
        positions.setdefault(_key(header), index)
    absent = [h for h in settings.mapping.values() if _key(h) not in positions]
    if absent:
        found = ", ".join(repr(h) for h in table.header)
        raise ImportFormatError(
            f"the file lacks the column(s) {', '.join(repr(h) for h in absent)}; "
            f"its header is {found}"
        )
    return {column: positions[_key(h)] for column, h in settings.mapping.items()}


_BARE_INTEGER = re.compile(r"^\d+$")
_ONE_OR_TWO_DIGITS = re.compile(r"^\d{1,2}$")


#: Columns a stray price fragment can land in unnoticed: text fields, which accept a
#: bare number. A fragment in a numeric column (pack size) or an unmapped one is left.
_TEXT_COLUMNS = frozenset({Column.NAME, Column.BRAND})


def _split_decimal_comma(
    cells: list[str],
    quoted: list[bool],
    price_index: int,
    next_column: Column | None,
    next_header: str,
) -> bool:
    """True when a ``,``-delimited row shows the trace of an unquoted ``12,90``.

    All must hold: the price cell and the next cell were both unquoted in the file;
    the price is a bare integer and the next cell has 1 or 2 digits; and the next
    column is unnamed in the header (a trailing delimiter) or mapped to a text field
    (``name`` or ``brand``). A quoted ``"15","30"``, or a VAT or pack column holding
    ``24`` after the price, is a valid row and is accepted.
    """
    after = price_index + 1
    if after >= len(cells) or quoted[price_index] or quoted[after]:
        return False
    if not (_BARE_INTEGER.match(cells[price_index].strip()) and
            _ONE_OR_TWO_DIGITS.match(cells[after].strip())):  # fmt: skip
        return False
    return next_header == "" or next_column in _TEXT_COLUMNS


def parse_import(data: bytes, settings: ImportSettings) -> ParsedImport:
    """Read file bytes with a profile into accepted and rejected rows.

    Raises ``ImportFormatError`` when the file as a whole cannot be read (encoding,
    no header, mapped columns absent). Problems with single rows never raise: those
    rows are rejected with a reason, including every row of a duplicate EAN.

    In a ``,``-delimited file an unquoted decimal comma silently shifts columns, so
    there a row is also rejected when its cell count differs from the header's, or
    when it looks like a split price (see ``_split_decimal_comma``).
    """
    table = read_table(data, settings.encoding, settings.delimiter)
    positions = column_positions(table, settings)
    width = len(table.header)
    comma = table.delimiter == ","
    price_index = positions[Column.PRICE]
    column_at = {index: column for column, index in positions.items()}
    next_column = column_at.get(price_index + 1)
    next_header = table.header[price_index + 1] if price_index + 1 < width else ""
    parsed: list[ImportRow | RejectedRow] = []
    for (line, cells), quoted in zip(table.rows, table.quoted, strict=True):
        # With ',' an unquoted decimal comma ("12,90") splits the price into two cells
        # and shifts every column after it, so any width mismatch is suspect.
        too_long = len(cells) > width and any(c.strip() for c in cells[width:])
        if too_long or (comma and len(cells) != width):
            parsed.append(
                RejectedRow(line, f"row has {len(cells)} cells but the header has {width}")
            )
            continue
        if comma and _split_decimal_comma(cells, quoted, price_index, next_column, next_header):
            parsed.append(
                RejectedRow(
                    line,
                    "price looks like an unquoted decimal comma "
                    f"({cells[price_index].strip()},{cells[price_index + 1].strip()}); "
                    'quote prices in comma-delimited files ("12,90")',
                )
            )
            continue
        values = {
            column: (cells[index] if index < len(cells) else "")
            for column, index in positions.items()
        }
        parsed.append(parse_row(line, values))
    rows, rejected = reject_duplicates(parsed)
    return ParsedImport(table.encoding, table.delimiter, rows, rejected)

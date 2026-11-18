"""Greek text for the importer's reasons and file errors.

Workstream A's importer (``core.domain.catalogue``, ``core.importers``) gives a
rejected row or an unreadable file an English reason with no separate code. This
module recognises each known reason by pattern and looks up its UI string, with the
values the reason carries. An unknown reason falls back to a generic Greek message
followed by the English original, so nothing is ever hidden. The tests trigger every
reason the importer can produce and check that each one is recognised.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.app.i18n import t


@dataclass(frozen=True, slots=True)
class _Pattern:
    regex: re.Pattern[str]
    key: str
    names: tuple[str, ...]


def _p(pattern: str, key: str, *names: str) -> _Pattern:
    return _Pattern(re.compile(pattern, re.DOTALL), key, names)


_V = r"(.*)"  # a value, as the importer quotes it

ROW_PATTERNS: tuple[_Pattern, ...] = (
    _p(r"^price is empty$", "import.reason.price_empty"),
    _p(rf"^price is negative: {_V}$", "import.reason.price_negative", "value"),
    _p(rf"^zero price, probably no price in the ERP: {_V}$", "import.reason.price_zero", "value"),
    _p(rf"^price is too large: {_V}$", "import.reason.price_too_large", "value"),
    _p(rf"^price is not a number: {_V}$", "import.reason.price_not_number", "value"),
    _p(
        rf"^price is in English number format; use Greek order 1\.290,50: {_V}$",
        "import.reason.price_english_format",
        "value",
    ),
    _p(
        rf"^price has misplaced thousands separators: {_V}$",
        "import.reason.price_thousands",
        "value",
    ),
    _p(
        rf"^price has a leading zero before a thousands separator: {_V}$",
        "import.reason.price_leading_zero",
        "value",
    ),
    _p(
        rf"^price is ambiguous \(thousands or decimals\?\): {_V}$",
        "import.reason.price_ambiguous",
        "value",
    ),
    _p(
        rf"^price has more than (\d+) decimals: {_V}$",
        "import.reason.price_decimals",
        "decimals",
        "value",
    ),
    _p(rf"^pack size is not a number: {_V}$", "import.reason.pack_not_number", "value"),
    _p(
        rf"^pack size is in English number format; use Greek order 1\.290,50: {_V}$",
        "import.reason.pack_not_number",
        "value",
    ),
    _p(
        rf"^pack size has misplaced thousands separators: {_V}$",
        "import.reason.pack_not_number",
        "value",
    ),
    _p(
        rf"^pack size has a leading zero before a thousands separator: {_V}$",
        "import.reason.pack_not_number",
        "value",
    ),
    _p(
        rf"^pack size is ambiguous \(thousands or decimals\?\): {_V}$",
        "import.reason.pack_ambiguous",
        "value",
    ),
    _p(
        rf"^pack size has more than (\d+) decimals: {_V}$",
        "import.reason.pack_too_precise",
        "decimals",
        "value",
    ),
    _p(rf"^unknown pack unit: {_V}$", "import.reason.pack_unit_unknown", "value"),
    _p(
        r"^pack size ('.*'|\".*\") disagrees with unit ('.*'|\".*\")$",
        "import.reason.pack_unit_mismatch",
        "value",
        "unit",
    ),
    _p(rf"^pack size has no unit: {_V}$", "import.reason.pack_no_unit", "value"),
    _p(
        rf"^pack size is more precise than 1/1000 (\S+): {_V}$",
        "import.reason.pack_too_precise",
        "unit",
        "value",
    ),
    _p(rf"^pack size must be positive: {_V}$", "import.reason.pack_not_positive", "value"),
    _p(rf"^pack size is too large: {_V}$", "import.reason.pack_too_large", "value"),
    _p(r"^EAN is empty$", "import.reason.ean_empty"),
    _p(
        rf"^EAN is in scientific notation, its digits are lost: {_V}$",
        "import.reason.ean_scientific",
        "value",
    ),
    _p(
        rf"^not an EAN; internal codes are not supported yet: {_V}$",
        "import.reason.ean_internal",
        "value",
    ),
    _p(
        rf"^12 digits \(UPC-A\?\): not padded; use the full EAN-13: {_V}$",
        "import.reason.ean_12_digits",
        "value",
    ),
    _p(
        rf"^(\d+) digits: an EAN has 8 or 13 digits: {_V}$",
        "import.reason.ean_length",
        "digits",
        "value",
    ),
    _p(rf"^all-zero code, probably no barcode: {_V}$", "import.reason.ean_all_zero", "value"),
    _p(
        rf"^check digit is (\d), expected (\d): {_V}$",
        "import.reason.ean_check_digit",
        "found",
        "expected",
        "value",
    ),
    _p(r"^name is empty$", "import.reason.name_empty"),
    _p(r"^name is longer than (\d+) characters$", "import.reason.name_too_long", "max"),
    _p(r"^brand is longer than (\d+) characters$", "import.reason.brand_too_long", "max"),
    _p(
        r"^row has (\d+) cells but the header has (\d+)$",
        "import.reason.row_too_long",
        "cells",
        "width",
    ),
)

_DUPLICATE = re.compile(r"^duplicate EAN (\S+) \(lines ([\d, ]+)\)(?:; (.*))?$", re.DOTALL)

FILE_PATTERNS: tuple[_Pattern, ...] = (
    _p(r"^the file is UTF-16; save it as UTF-8 or Windows-1253 CSV$", "import.file.utf16"),
    _p(r"^the file is not valid (.+) \(byte (\d+)\)$", "import.file.encoding", "encoding", "byte"),
    _p(r"^the file is empty$", "import.file.empty"),
    _p(
        r"^cannot tell the delimiter: the header has a single column$",
        "import.file.single_column",
    ),
    _p(r"^line (\d+): a quoted cell is not closed$", "import.file.unclosed_quote", "line"),
    _p(
        r"^the file lacks the column\(s\) (.+); its header is (.*)$",
        "import.file.missing_columns",
        "columns",
        "header",
    ),
    _p(
        r"^the header (.+) appears more than once, in columns (.+); "
        r"rename or remove the extra column$",
        "import.file.repeated_header",
        "header",
        "columns",
    ),
    _p(
        r"^line (\d+): prices look like unquoted decimal commas \((.+)\); .*$",
        "import.file.split_decimal",
        "line",
        "value",
    ),
    _p(
        r"^line (\d+) has (\d+) cells but the header has (\d+); .*$",
        "import.file.cell_count",
        "line",
        "cells",
        "width",
    ),
)

#: Every UI string key this module can produce, for the key coverage test.
MESSAGE_KEYS: tuple[str, ...] = (
    *dict.fromkeys(p.key for p in (*ROW_PATTERNS, *FILE_PATTERNS)),
    "import.reason.duplicate_ean",
    "import.reason.unknown",
    "import.file.unknown",
)


def _unquote(value: str) -> str:
    """Drop the quotes ``repr`` put around one value; a list of values keeps them."""
    quote = value[:1]
    if len(value) >= 2 and quote in ("'", '"') and value[-1] == quote and quote not in value[1:-1]:
        return value[1:-1]
    return value


def _match(patterns: tuple[_Pattern, ...], text: str) -> str | None:
    for pattern in patterns:
        found = pattern.regex.match(text)
        if found is not None:
            values = {n: _unquote(v) for n, v in zip(pattern.names, found.groups(), strict=True)}
            return t(pattern.key, **values)
    return None


def row_reason(reason: str) -> str:
    """Greek text for a rejected row's reason."""
    duplicate = _DUPLICATE.match(reason)
    if duplicate is not None:
        ean, lines, inner = duplicate.groups()
        text = t("import.reason.duplicate_ean", ean=ean, lines=lines)
        return f"{text}; {row_reason(inner)}" if inner else text
    found = _match(ROW_PATTERNS, reason)
    return found if found is not None else t("import.reason.unknown", detail=reason)


def file_error(message: str) -> str:
    """Greek text for a file the importer refused as a whole."""
    found = _match(FILE_PATTERNS, message)
    return found if found is not None else t("import.file.unknown", detail=message)


def is_known_row_reason(reason: str) -> bool:
    if _DUPLICATE.match(reason):
        return True
    return any(p.regex.match(reason) for p in ROW_PATTERNS)


def is_known_file_error(message: str) -> bool:
    return any(p.regex.match(message) for p in FILE_PATTERNS)

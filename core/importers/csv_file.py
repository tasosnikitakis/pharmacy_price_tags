"""Reading ERP price-list CSV files: encoding and delimiter detection (section 6, step 2).

Greek exports are typically Windows-1253 with ``;`` and a decimal comma; newer ones are
UTF-8, sometimes with a byte-order mark. Bytes go in, a table of text cells comes out.
Interpreting the cells is ``core.domain.catalogue``'s job.
"""

from __future__ import annotations

import codecs
import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass, field

#: Encodings an import profile may name. ``None`` in a profile means "detect".
ENCODINGS = ("utf-8", "cp1253")
#: Delimiters an import profile may name, in the order detection prefers them.
DELIMITERS = (";", "\t", ",")

_ENCODING_ALIASES = {
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "utf-8-sig": "utf-8",
    "cp1253": "cp1253",
    "windows-1253": "cp1253",
    "win1253": "cp1253",
}

#: Lines looked at when guessing the delimiter.
_SNIFF_LINES = 20


class ImportFormatError(ValueError):
    """The file as a whole cannot be read; the message is shown to the admin."""


def canonical_encoding(name: str) -> str:
    """``"Windows-1253"`` -> ``"cp1253"``; raises ``ValueError`` for an unsupported one."""
    key = name.strip().lower().replace("_", "-")
    if key not in _ENCODING_ALIASES:
        raise ValueError(f"unsupported encoding {name!r}; use one of {', '.join(ENCODINGS)}")
    return _ENCODING_ALIASES[key]


def decode(data: bytes, encoding: str | None = None) -> tuple[str, str]:
    """Decode file bytes, returning ``(text, encoding_used)``.

    With ``encoding=None``: a UTF-8 byte-order mark or valid UTF-8 means UTF-8,
    anything else is read as Windows-1253. A UTF-8 BOM always means UTF-8, even when
    ``encoding`` names another, and is always dropped. UTF-16 is
    refused with a clear message (some spreadsheet tools save it as "Unicode text").
    """
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        raise ImportFormatError("the file is UTF-16; save it as UTF-8 or Windows-1253 CSV")
    if data.startswith(codecs.BOM_UTF8):
        # A byte-order mark is proof of UTF-8, whatever the profile says: reading it
        # as Windows-1253 would turn the BOM into "ï»¿" on the first header cell.
        candidates = ["utf-8"]
    elif encoding is not None:
        encoding = canonical_encoding(encoding)
        candidates = [encoding]
    else:
        candidates = list(ENCODINGS)
    for candidate in candidates:
        codec = "utf-8-sig" if candidate == "utf-8" else candidate
        try:
            return data.decode(codec), candidate
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ImportFormatError(
        f"the file is not valid {' or '.join(candidates)} (byte {last_error.start})"
    )


def detect_delimiter(text: str) -> str:
    """Guess the delimiter from the first lines of the file.

    The first of ``;``, tab and ``,`` that splits every sampled line into the same
    number of fields (more than one) wins; ``;`` comes first because a decimal comma
    makes ``,`` look plausible. Failing that, the candidate with the most fields in the
    header line wins. Raises ``ImportFormatError`` if no candidate splits the header.
    """
    lines = [line for line in text.splitlines() if line.strip()][:_SNIFF_LINES]
    if not lines:
        raise ImportFormatError("the file is empty")
    sample = "\n".join(lines)
    widths: dict[str, int] = {}
    for delimiter in DELIMITERS:
        counts = {len(r) for r in csv.reader(io.StringIO(sample), delimiter=delimiter) if r}
        if len(counts) == 1 and counts.pop() > 1:
            return delimiter
        header = next(csv.reader(io.StringIO(lines[0]), delimiter=delimiter))
        widths[delimiter] = len(header)
    best = max(DELIMITERS, key=lambda d: widths[d])  # ties keep DELIMITERS order
    if widths[best] <= 1:
        raise ImportFormatError("cannot tell the delimiter: the header has a single column")
    return best


@dataclass(frozen=True, slots=True)
class Table:
    """A decoded CSV file: header cells and data rows with their line numbers."""

    encoding: str
    delimiter: str
    header: list[str]
    rows: list[tuple[int, list[str]]]  # (1-based line number in the file, cells)
    #: For each row in ``rows``, whether each cell was quoted in the file. Quoting is
    #: what tells a quoted ``"15","30"`` from an unquoted decimal comma ``15,30``.
    quoted: list[list[bool]] = field(default_factory=list)


_LINE_ENDS = ("\r", "\n")


def split_records(text: str, delimiter: str) -> Iterator[tuple[int, list[str], list[bool]]]:
    """Split CSV text into records of ``(start line, cells, quoted flags)``.

    The usual CSV rules, as Python's ``csv`` module applies them by default: a cell
    that starts with ``"`` is quoted up to the closing ``"`` (``""`` is a literal quote)
    and may hold delimiters and line breaks; records end at CRLF, LF or CR outside
    quotes. Unlike ``csv`` it reports which cells were quoted. Raises
    ``ImportFormatError`` for a quoted cell that is never closed.
    """
    i, n, line = 0, len(text), 1
    stops = (delimiter, *_LINE_ENDS)
    while i < n:
        start_line = line
        cells: list[str] = []
        quoted: list[bool] = []
        while True:
            if i < n and text[i] == '"':
                i += 1
                buf: list[str] = []
                while True:
                    if i >= n:
                        raise ImportFormatError(f"line {start_line}: a quoted cell is not closed")
                    char = text[i]
                    if char == '"':
                        if text.startswith('""', i):
                            buf.append('"')
                            i += 2
                            continue
                        i += 1
                        break
                    if char == "\n" or (char == "\r" and not text.startswith("\r\n", i)):
                        line += 1
                    buf.append(char)
                    i += 1
                # Text after the closing quote is kept as it is, as ``csv`` does.
                while i < n and text[i] not in stops:
                    buf.append(text[i])
                    i += 1
                cells.append("".join(buf))
                quoted.append(True)
            else:
                j = i
                while j < n and text[j] not in stops:
                    j += 1
                cells.append(text[i:j])
                quoted.append(False)
                i = j
            if i < n and text[i] == delimiter:
                i += 1
                continue
            if i < n:  # a line end closes the record
                i += 2 if text.startswith("\r\n", i) else 1
                line += 1
            break
        yield start_line, cells, quoted


def read_table(data: bytes, encoding: str | None = None, delimiter: str | None = None) -> Table:
    """Decode and split a CSV file. ``None`` means detect for both settings.

    The first non-blank line is the header; blank lines are skipped. Header cells are
    trimmed and lose any byte-order mark. Handles CRLF, LF and quoted cells (a
    ``,``-delimited export must quote decimal-comma prices, ``"12,90"``).
    """
    text, used_encoding = decode(data, encoding)
    if delimiter is None:
        delimiter = detect_delimiter(text)
    elif delimiter not in DELIMITERS:
        raise ValueError(f"unsupported delimiter {delimiter!r}")
    header: list[str] | None = None
    rows: list[tuple[int, list[str]]] = []
    quoted_rows: list[list[bool]] = []
    for line, cells, quoted in split_records(text, delimiter):
        if not any(cell.strip() for cell in cells):
            continue
        if header is None:
            header = [cell.replace("\ufeff", "").strip() for cell in cells]
        else:
            rows.append((line, cells))
            quoted_rows.append(quoted)
    if header is None:
        raise ImportFormatError("the file is empty")
    return Table(used_encoding, delimiter, header, rows, quoted_rows)

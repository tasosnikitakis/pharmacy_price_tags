"""CSV import: reading ERP exports with a saved profile (PLAN.md section 6).

``read_table`` decodes and splits a file, ``ImportSettings`` is a validated profile,
and ``parse_import`` turns a file into accepted and rejected rows. The preview diff
is ``core.domain.catalogue.build_diff``; writing is ``core.services.catalogue``.
"""

from core.importers.csv_file import ImportFormatError, Table, decode, detect_delimiter, read_table
from core.importers.profile import ImportSettings, ParsedImport, parse_import

__all__ = [
    "ImportFormatError",
    "ImportSettings",
    "ParsedImport",
    "Table",
    "decode",
    "detect_delimiter",
    "parse_import",
    "read_table",
]

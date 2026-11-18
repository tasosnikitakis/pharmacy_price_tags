# 0011 CSV import rules

Workstream A's choices for parsing and applying ERP price-list exports (section 6).
They are conservative on purpose: when the file is ambiguous, the row is rejected with a
reason rather than guessed.

**Parsing**

- Encodings: UTF-8 (with or without BOM) and Windows-1253 are detected; UTF-16 is
  refused. A UTF-8 BOM wins over a profile that says Windows-1253, and is never left on
  the first header cell. Delimiters `;`, tab and `,` are detected.
- Headers, Greek or not, match the profile ignoring case, Greek tonos and dialytika,
  and repeated whitespace (Tasos, 18 November 2026). Two mapped headers that are equal
  after this folding are refused.
- Prices are parsed exactly into cents: decimal comma, `.` as thousands separator.
  Both separators in Greek order import exactly (`1.290,50` → 129050 cents, Tasos's
  rule). More than two decimals, a negative value, or an ambiguous `1.290` (a lone `.`
  followed by exactly three digits) is rejected, never rounded or guessed.
- A zero price is rejected ("zero price, probably no price in the ERP"): ERP exports
  often use 0 for "no price", and a 0,00 € label would be wrong.
- Comma-delimited files only: a row whose cell count differs from the header's is
  rejected. A row is also rejected as a split decimal (`12,90` read as `12` and `90`)
  when the price cell and the next cell were both unquoted, the price is a bare integer,
  the next cell has 1–2 digits, and the next column is empty in the header or mapped to
  the name or brand. A quoted price, or a VAT or pack column after the price, is fine.
  `;` and tab files are unaffected. An unclosed quote rejects the file with its line.
- Pack sizes go to `pack_qty_milli`; kg and l convert exactly to g and ml. Greek and
  English unit spellings are accepted, ignoring case, accents and a trailing dot.
- EANs are text, never numbers, so leading zeros survive (Tasos, 18 November 2026).
  Only EAN-13 and EAN-8 with a valid GS1 check digit are accepted; in-store codes
  (prefix 20–29) pass when their check digit is valid. Rejected, each with its reason:
  spreadsheet notation such as `5.20123E+12`; internal pharmacy codes ("not an EAN;
  internal codes are not supported yet"); 12-digit codes ("not padded; use the full
  EAN-13"), which are never padded; other lengths; and a wrong check digit ("check digit
  is X, expected Y"). Whether to support internal codes, and whether in-store codes and
  EAN-8 stay accepted, are open questions for Tasos.
- A duplicate EAN rejects every row with that EAN, naming all its lines, because the
  file does not say which row is right.

**Applying**

- One transaction. All the store's `price` rows are locked in `product_id` order, then
  the matched product rows. Every price write goes through `apply_price_change` with the
  stored promo carried through unchanged; product field changes go through
  `apply_product_change`.
- Name, brand and pack values from the file overwrite the stored ones. An empty brand or
  pack cell keeps the stored value, so an import can never break
  `unit_price_needs_pack`. Imports never set `is_rx`, `unit_price_required` or `active`;
  new products start with `false`, `false`, `true`.
- Products missing from the file are listed (active ones only) and never deleted.
- Applying clears the "manually changed" flag only on products that appear in the file.
- There is no stored preview: apply re-parses the file and rebuilds the diff under the
  locks, so a catalogue change between preview and apply can change the applied diff.
  M5 may add a check that the preview is still current.

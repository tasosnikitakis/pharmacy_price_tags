# 0011 CSV import rules

Workstream A's choices for parsing and applying ERP price-list exports (section 6).
They are conservative on purpose: when the file is ambiguous, the row is rejected with a
reason rather than guessed.

**Parsing**

- Encodings: UTF-8 (with or without BOM) and Windows-1253 are detected; UTF-16 is
  refused. Delimiters `;`, tab and `,` are detected.
- Prices are parsed exactly into cents: decimal comma, `.` as thousands separator.
  More than two decimals, a negative value, or an ambiguous `1.290` (a lone `.` followed
  by exactly three digits) is rejected, never rounded or guessed.
- Pack sizes go to `pack_qty_milli`; kg and l convert exactly to g and ml. Greek and
  English unit spellings are accepted, ignoring case, accents and a trailing dot.
- EANs: 8 to 14 digits, leading zeros kept, no check-digit test (internal codes pass).
  Spreadsheet notation such as `5.20123E+12` is rejected.
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

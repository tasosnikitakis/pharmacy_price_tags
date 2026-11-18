# erp_sample_synthetic.csv

Synthetic price list shaped like a Greek pharmacy ERP export. **It is not real data:** brands are real, prices and barcodes are invented. It does not satisfy the M1 criterion for the real ERP sample (`erp_sample.csv`).

## Format

- Encoding: Windows-1253, CRLF line endings, no BOM
- Delimiter: `;`
- Decimal comma; `.` as thousands separator only together with a comma (`1.290,00`)
- Header row in Greek

| Column | Meaning | Maps to |
| --- | --- | --- |
| `Barcode` | EAN-13 as text | `product.ean` |
| `Περιγραφή` | Product name | `product.name` |
| `Εταιρεία` | Brand | `product.brand` |
| `Λιανική με ΦΠΑ` | Retail price incl. VAT | `price.regular_cents` |
| `Ποσότητα` | Pack quantity | `product.pack_qty` |
| `Μον.Μέτρ.` | Pack unit: `ml`, `g` or `τεμ` (pieces) | `product.pack_unit` |

## Expected import result

37 data rows: **29 accepted, 8 rejected.**

| Rows | Expected | Why |
| --- | --- | --- |
| First 28 | Accepted | Normal products; accented, all-caps and very long names; `1.290,00` imports as 129000 cents |
| `2000000123455` | Accepted (decision needed) | Valid EAN-13 with an in-store prefix (20–29). Decide whether in-store codes are allowed |
| `ΕΣ-00417` | Rejected | Internal code, not an EAN |
| `5.20123E+12` | Rejected | Barcode mangled into scientific notation |
| `520333872624` | Rejected | 12 digits; must not be padded |
| `5207317810803` | Rejected | Wrong check digit |
| `1.290` (Hylo) | Rejected | Ambiguous number |
| `12,345` (Floradix) | Rejected | More than two decimals |
| `3337723430980` ×2 | Both rejected | Duplicate EAN in one file |

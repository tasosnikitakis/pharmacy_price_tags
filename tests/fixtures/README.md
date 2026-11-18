Synthetic test data only. The one real file allowed here is the trimmed ERP sample
`erp_sample.csv` (20–50 rows; EAN, name, price and pack columns), added by Tasos.

- `erp_synthetic_cp1253.csv`: invented products and EANs (prefix 200, reserved for
  in-store use), Windows-1253, `;` delimiter, decimal comma, CRLF. It has accented
  capitals, a non-numeric price (line 10), a negative price (line 11) and a duplicate
  EAN (lines 12 and 13). Tests read it as bytes.
- `erp_sample.csv` (when added) is imported by `tests/integration/test_catalogue_erp_sample.py`
  with the profile in `erp_sample.profile.json`, which must be added with it.

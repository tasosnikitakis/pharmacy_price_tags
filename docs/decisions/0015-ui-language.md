# 0015 UI language and formats

**Decision (Tasos, 18 November 2026).** The admin UI and the phone pages are Greek only
in v1.

- Every user-facing string of the UI lives in one translation file,
  `core/app/i18n/el.json`, keyed by English identifiers such as `import.preview.title`,
  with Greek values. UI templates and `core/app/` code never contain user-facing Greek
  or English text directly. Label text drawn on the tags ("ΠΡΟΣΦΟΡΑ", "έως", `€/τεμ.`)
  is not UI text: it stays in the renderer (`core/render/`, `core/domain/pricing.py`,
  decision 0012).
- The UI language is read from configuration and defaults to `el`, so adding English
  later means adding `en.json` with the same keys and changing that configuration; no
  code or template changes.
- Greek formats everywhere in the UI: money as `12,90 €` (the same formatter as the
  labels, `core.domain.pricing.format_price`), dates as `18/11/2026`, times as `14:05`
  in `Europe/Athens`. The database keeps UTC.
- Code, identifiers, logs, docs, decision notes and machine-readable error codes stay
  in English. A user-facing message is looked up from an error code, so logs show the
  code and the UI shows the Greek text.
- A test checks that every key used in templates and code exists in `el.json`, and
  that `el.json` has no unused keys. Keys built at runtime, such as messages looked up
  from error codes, are listed explicitly so the test can account for them.

# Vendored front-end files

Served from `/static/vendor/`; the shop box may be offline, so no CDN is used. Each
library sits in a directory named after its exact version, with its licence file.

| Library | Version | Files | Licence | Source |
| --- | --- | --- | --- | --- |
| htmx | 2.0.11 | `htmx-2.0.11/htmx.min.js` | Zero-Clause BSD (`htmx-2.0.11/LICENSE`) | npm package `htmx.org@2.0.11`, `dist/htmx.min.js` |
| ZXing for JS | 0.23.0 | `zxing-library-0.23.0/index.min.js` | Apache-2.0 (`zxing-library-0.23.0/LICENSE`) | npm package `@zxing/library@0.23.0`, `umd/index.min.js` |

`htmx.min.js` SHA-256: `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717`.

ZXing `index.min.js` SHA-256:
`3ede94153fb0c5b67a12d7adff6decd827c2b22714fdc6faecf27a8f20937ea6` (npm tarball
integrity `sha512-6fkkoFwP8CHxl6ugnPsj74PLJgX2iRv5zczGAyt5OBzQgxFhuhF0NCEc4t4OvSr8xAv2MRLlI0Iu9ZGDZQ2urA==`).
The UMD bundle is self-contained (global `ZXing`); it inlines the library's one
dependency, `ts-custom-error` 3.3.x (MIT, Adrien Gibrat), whose notice is
`zxing-library-0.23.0/LICENSE-ts-custom-error`. It is loaded only by `/static/app.js`, and only on
a phone whose browser has no `BarcodeDetector` (for example Safari and Firefox); the
typed field works without either.

To update: download the new version's npm tarball, copy `dist/htmx.min.js` and
`LICENSE` into a new `htmx-<version>/` directory, update `core/app/templates/base.html`
and this table, and delete the old directory. For ZXing, copy `umd/index.min.js` and
`LICENSE` (plus the `ts-custom-error` licence of the version it inlines) into `zxing-library-<version>/` and update `ZXING_SRC` in
`core/app/web.py`.

# Vendored front-end files

Served from `/static/vendor/`; the shop box may be offline, so no CDN is used. Each
library sits in a directory named after its exact version, with its licence file.

| Library | Version | Files | Licence | Source |
| --- | --- | --- | --- | --- |
| htmx | 2.0.11 | `htmx-2.0.11/htmx.min.js` | Zero-Clause BSD (`htmx-2.0.11/LICENSE`) | npm package `htmx.org@2.0.11`, `dist/htmx.min.js` |

`htmx.min.js` SHA-256: `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717`.

To update: download the new version's npm tarball, copy `dist/htmx.min.js` and
`LICENSE` into a new `htmx-<version>/` directory, update `core/app/templates/base.html`
and this table, and delete the old directory.

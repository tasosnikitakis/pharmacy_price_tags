# 0012 Renderer and pricing implementation choices

Workstream B's choices while implementing M2 (sections 6 and 7).

**Fonts.** DejaVu Sans Condensed 2.37, regular and bold, bundled under
`core/render/fonts/` with their licence (Bitstream Vera and Arev licences; DejaVu
changes in the public domain) and SHA-256 sums. The condensed cut lets long Greek names
and `999,99 €` fit in 296 pixels. The regular weight is not used at 13 or 14 px, where
its hinting drops or fuses the tonos on accented capitals; bold keeps it at all sizes.

**Golden images are compared by pixels, not bytes.** The comparison checks mode, size,
palette and every palette index. PNG bytes can differ between platforms because of
the zlib bundled with Pillow; pixels cannot. Goldens are tied to the Pillow and FreeType
versions in `uv.lock`; after a Pillow upgrade they are regenerated deliberately
(`uv run python -m tests.golden.regenerate`) and reviewed by eye. The render hash does
not include the Pillow version, so an upgrade that changes any golden's pixels must also
bump `TEMPLATE_VERSIONS` for the affected templates; otherwise stored renders, and the
tags showing them, keep the old pixels (the information on them stays correct).
This bump rule applies from M2's merge onwards. The two promo goldens changed during
M2's review without a bump; nothing had been stored then, so no render or tag could
keep the earlier pixels.

**Unit price size.** The layout requires both `unit_size * 10 >= price_size * 7` on the
font sizes and the same 70% on the ink height of the reference digits `0123456789`
measured at both sizes. Comparing a label's own digits is not enough: round digits
overshoot flat ones by about a pixel.

**Unit price suffixes.** `€/kg`, `€/l` and `€/τεμ.` (per piece).

**Event payloads.** `promo.prior_set` carries `promo_from` and `promo_to` as UTC
ISO-8601 strings, so payloads go straight into JSONB. `promo.refused.reason` is the
`PromoRefusal` value. When a promo is removed, the events come as `promo.refused`, then
`promo.prior_cleared`.

**Input checks beyond the contract.** `apply_price_change` raises `ValueError` for
negative prices and for naive datetimes; `unit_price` for a negative price.

**Promo label.** A "ΠΡΟΣΦΟΡΑ" badge (white on red, or red on yellow on BWRY), the prior
price struck through, "έως DD/MM", the promo price in red, and the unit price in black
below it when required. The brand is not shown on promo labels. `effective_template`
returns `promo` for any binding while a valid promo is active, as specified.

**Other tag sizes.** Templates are laid out for 296×128 and scale to other sizes; only
296×128 has golden images.

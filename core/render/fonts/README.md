# Bundled fonts

The renderer uses only these files, loaded by path relative to `core/render/`. It never
falls back to system fonts.

| File | Face |
| --- | --- |
| `DejaVuSansCondensed.ttf` | DejaVu Sans Condensed (regular) |
| `DejaVuSansCondensed-Bold.ttf` | DejaVu Sans Condensed Bold |

- **Source:** the official DejaVu release archive
  `https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip`
  (project site: https://dejavu-fonts.github.io/). The two files are copied unchanged
  from its `ttf/` folder.
- **Version:** 2.37.
- **Licence:** the Bitstream Vera Fonts licence plus the Arev Fonts licence, with
  DejaVu changes in the public domain. Full text in `LICENSE`, copied unchanged from the
  archive. Both licences allow redistribution, including commercially, provided the
  notices are kept; modified versions must be renamed.
- **Greek coverage:** 100% of the Greek set listed in the archive's `langcover.txt`,
  including accented and dialytika capitals.
- **SHA-256** (to check that the files were not modified):
  - archive `dejavu-fonts-ttf-2.37.zip`:
    `7576310b219e04159d35ff61dd4a4ec4cdba4f35c00e002a136f00e96a908b0a`
  - `DejaVuSansCondensed.ttf`:
    `8550cd5ca1acb65a8fc7877c46939cd0b4d909f8e7bc1e24716873d918c5e549`
  - `DejaVuSansCondensed-Bold.ttf`:
    `38098d0b8edd8430a0f53fb2831b5d36037563a998a620d3c87a4d7591766d20`

**Why the condensed cut:** the 296-pixel label must fit two lines of a long Greek
product name and prices up to `999,99 €`. DejaVu's TrueType hinting renders clean
1-bit glyphs without anti-aliasing. At 13 and 14 px the hinted regular weight drops or
fuses the tonos of some accented capitals, so the templates skip those sizes for
regular text (see `core/render/templates.py`).

Replacing a font changes every label: regenerate the golden images and bump every
template version (`tests/golden/README.md`).

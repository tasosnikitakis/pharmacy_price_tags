# Golden images

One PNG per case in `cases.py`, rendered by `core.render.render`. All fixtures are
synthetic; no real product or price data.

`tests/unit/test_render.py` renders each case and compares it with its PNG **pixel by
pixel** (mode, size, palette and every palette index), not byte by byte: the zlib
build inside Pillow can differ between platforms, which changes the compressed bytes
but not the image. Within one machine the bytes are deterministic, and a separate test
checks that.

## Regenerating

Only after a deliberate visual change:

```
uv run python -m tests.golden.regenerate             # every case
uv run python -m tests.golden.regenerate promo_bwr   # named cases
```

Then:

1. Look at every changed PNG (enlarge with nearest-neighbour scaling).
2. Bump the template's entry in `TEMPLATE_VERSIONS` (`core/render/label.py`), because
   the version is part of the render hash and tags must pick up the new image.
3. Commit the PNGs with the change.

A golden that changes without a code change usually means a different Pillow or
FreeType version: check `uv.lock` before regenerating.

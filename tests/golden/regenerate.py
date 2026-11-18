"""Rewrite the golden PNGs from the current renderer. Run deliberately, then review
every changed image before committing:

    uv run python -m tests.golden.regenerate          # all cases
    uv run python -m tests.golden.regenerate promo_bwr  # named cases only

A visual change also needs a bump of ``TEMPLATE_VERSIONS`` in ``core/render/label.py``.
"""

from __future__ import annotations

import sys

from core.render import render
from tests.golden.cases import CASES, golden_path


def main(names: list[str]) -> int:
    unknown = [name for name in names if name not in CASES]
    if unknown:
        sys.stderr.write(f"unknown cases: {', '.join(unknown)}\n")
        return 2
    for name in names or sorted(CASES):
        label, tag_type = CASES[name]
        png = render(label, tag_type).png
        path = golden_path(name)
        old = path.read_bytes() if path.exists() else None
        path.write_bytes(png)
        sys.stdout.write(f"{'unchanged' if old == png else 'written'}: {path.name}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

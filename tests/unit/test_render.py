"""Renderer (workstream B): golden images, palette, determinism, hashing, layout rules
and the render store.

Greek text never appears in an assertion's introspected values: the Windows CI console
may not encode it. Comparisons are done first and only booleans, numbers or ``ascii()``
reach the assertion.
"""

# Greek fixtures mix Greek and Latin letters that look alike, on purpose.
# ruff: noqa: RUF001

from __future__ import annotations

import io
import struct
import uuid
import zlib
from dataclasses import replace

import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.db.models import RenderRow, TagTypeRow
from core.render import TEMPLATE_VERSIONS, LabelData, TemplateKey, label_hash, render
from core.render._fonts import ink_bbox
from core.render._layout import ELLIPSIS, TextItem
from core.render.templates import layout
from core.services.render_store import ensure_render, load_png, save_render
from shared.tag_types import PALETTE_COLOURS, Palette, TagType
from tests.golden.cases import BW, BWR, BWRY, CASES, LONG_NAME, LONG_WORD, golden_path

TAG = TagType(
    key="gicisky-2.9-bwr", vendor="gicisky", model="2.9", width=296, height=128, palette=Palette.BWR
)
LABEL = LabelData(
    TemplateKey.STANDARD, name="ΑΣΠΙΡΊΝΗ Protect 100mg", brand="Bayer", price_text="4,50 €"
)

#: label_hash(LABEL, TAG), pinned: the hash must not change between machines, Python
#: versions or releases unless the template version or the serialisation changes.
LABEL_HASH = "76399b02be054629941ca7b59f2eff10b539342a896295410efad1b2e5742b8f"


def _open(png: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(png))
    image.load()
    return image


def _png_chunks(png: bytes) -> dict[bytes, bytes]:
    chunks: dict[bytes, bytes] = {}
    pos = 8
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        kind = png[pos + 4 : pos + 8]
        chunks.setdefault(kind, png[pos + 8 : pos + 8 + length])
        pos += 12 + length
    return chunks


# --- contract placeholders (now implemented) --------------------------------------


def test_same_label_same_hash_and_bytes() -> None:
    assert label_hash(LABEL, TAG) == label_hash(LABEL, TAG)
    a, b = render(LABEL, TAG), render(LABEL, TAG)
    assert (a.hash, a.png) == (b.hash, b.png)


def test_output_is_native_size_and_only_palette_colours() -> None:
    out = render(LABEL, TAG)
    image = Image.open(io.BytesIO(out.png)).convert("RGB")
    assert image.size == (296, 128)
    allowed = set(PALETTE_COLOURS[Palette.BWR])
    assert {rgb for _, rgb in image.getcolors(maxcolors=1 << 16) or []} <= allowed


# --- golden images ----------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_image(name: str) -> None:
    label, tag_type = CASES[name]
    path = golden_path(name)
    assert path.exists(), f"missing golden {path.name}; see tests/golden/README.md"
    expected = _open(path.read_bytes())
    actual = _open(render(label, tag_type).png)
    assert (actual.mode, actual.size) == (expected.mode, expected.size)
    assert actual.getpalette() == expected.getpalette()
    a, e = actual.tobytes(), expected.tobytes()
    differing = sum(1 for x, y in zip(a, e, strict=True) if x != y)
    assert differing == 0, f"{name}: {differing} pixels differ from the golden image"


def test_every_template_and_palette_has_a_golden() -> None:
    templates = {label.template for label, _ in CASES.values()}
    assert templates == set(TemplateKey)
    assert {tag_type.palette for _, tag_type in CASES.values()} == set(Palette)


# --- palette ----------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_pixel_is_a_palette_index(name: str) -> None:
    label, tag_type = CASES[name]
    png = render(label, tag_type).png
    image = _open(png)
    colours = PALETTE_COLOURS[tag_type.palette]
    assert image.mode == "P"
    assert image.size == (tag_type.width, tag_type.height)
    # The PNG palette is exactly the tag's colours, in order, with nothing padded.
    plte = _png_chunks(png)[b"PLTE"]
    assert plte == bytes(channel for rgb in colours for channel in rgb)
    used = {index for _, index in image.getcolors(maxcolors=256) or []}
    assert used <= set(range(len(colours)))


@pytest.mark.parametrize("name", sorted(CASES))
def test_red_and_yellow_only_on_promo_labels(name: str) -> None:
    label, tag_type = CASES[name]
    image = _open(render(label, tag_type).png)
    used = {index for _, index in image.getcolors(maxcolors=256) or []}
    if label.template is TemplateKey.PROMO:
        assert 2 in used  # red promo price
        assert (3 in used) == (tag_type.palette is Palette.BWRY)
    else:
        assert used <= {0, 1}


def test_png_has_no_metadata_chunks() -> None:
    chunks = _png_chunks(render(LABEL, TAG).png)
    assert set(chunks) == {b"IHDR", b"PLTE", b"IDAT", b"IEND"}
    width, height, depth, colour_type = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (width, height, depth, colour_type) == (296, 128, 2, 3)  # 2-bit indexed
    zlib.decompress(chunks[b"IDAT"])  # a single, complete IDAT stream


def test_bw_tag_uses_a_one_bit_png() -> None:
    label, tag_type = CASES["standard_bw"]
    ihdr = _png_chunks(render(label, tag_type).png)[b"IHDR"]
    assert ihdr[8:10] == bytes([1, 3])


# --- hashing and determinism ------------------------------------------------------


def test_hash_is_pinned_across_machines() -> None:
    assert label_hash(LABEL, TAG) == LABEL_HASH


def test_render_carries_hash_and_tag_type() -> None:
    out = render(LABEL, TAG)
    assert out.hash == label_hash(LABEL, TAG)
    assert (out.tag_type_key, out.width, out.height) == (TAG.key, 296, 128)


@pytest.mark.parametrize(
    "changed",
    [
        replace(LABEL, name="ΑΣΠΙΡΊΝΗ Protect 500mg"),
        replace(LABEL, brand=None),
        replace(LABEL, price_text="4,51 €"),
        replace(LABEL, template=TemplateKey.UNIT_PRICE, unit_price_text="45,00 €/kg"),
    ],
    ids=["name", "brand", "price", "template"],
)
def test_hash_changes_with_any_label_field(changed: LabelData) -> None:
    assert label_hash(changed, TAG) != label_hash(LABEL, TAG)


def test_hash_changes_with_tag_type_and_template_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert label_hash(LABEL, BWRY) != label_hash(LABEL, TAG)
    before = label_hash(LABEL, TAG)
    monkeypatch.setitem(TEMPLATE_VERSIONS, TemplateKey.STANDARD, 2)
    assert label_hash(LABEL, TAG) != before


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_case_renders_deterministically(name: str) -> None:
    label, tag_type = CASES[name]
    first, second = render(label, tag_type), render(label, tag_type)
    assert first.png == second.png
    assert first.hash == second.hash


# --- the 70% unit-price rule, measured on rendered pixels ---------------------------


def _digit_ink_height(image: Image.Image, item: TextItem) -> int:
    """Height in rendered pixels of the integer digits of a price ("12" of "12,90 €"),
    measured in the label image within the item's columns for those digits."""
    digits = item.text.split(",", 1)[0]
    left, _, right, _ = ink_bbox(digits, item.bold, item.size)
    pixels = image.load()
    assert pixels is not None
    rows = [
        y
        for y in range(item.ink.top, item.ink.bottom)
        if any(pixels[x, y] != 0 for x in range(item.x + left, item.x + right))
    ]
    return rows[-1] - rows[0] + 1


PRICE_PAIRS = [
    ("0,50 €", "2,50 €/kg"),
    ("4,50 €", "22,50 €/kg"),
    ("12,90 €", "64,50 €/l"),
    ("99,90 €", "0,10 €/τεμ."),
    ("999,99 €", "4999,95 €/l"),
    ("999,99 €", "19999,80 €/l"),
    ("1,00 €", "812,50 €/kg"),
]


def _unit_labels() -> list[tuple[str, LabelData, TagType]]:
    cases: list[tuple[str, LabelData, TagType]] = []
    for number, (price, unit) in enumerate(PRICE_PAIRS):
        cases.append((
            f"unit_price-{number}",
            LabelData(TemplateKey.UNIT_PRICE, name=LONG_NAME, brand="Κορρές", price_text=price,
                      unit_price_text=unit),
            BWR,
        ))  # fmt: skip
        for tag_type in (BWR, BWRY):
            cases.append((
                f"promo-{tag_type.palette.value}-{number}",
                LabelData(TemplateKey.PROMO, name=LONG_NAME, price_text=price,
                          prior_price_text="999,99 €", promo_until_text="31/12",
                          unit_price_text=unit),
                tag_type,
            ))  # fmt: skip
    return cases


@pytest.mark.parametrize(
    ("label", "tag_type"),
    [(label, tag_type) for _, label, tag_type in _unit_labels()],
    ids=[name for name, _, _ in _unit_labels()],
)
def test_unit_price_is_at_least_70_percent_of_price_and_both_bold(
    label: LabelData, tag_type: TagType
) -> None:
    items = {item.role: item for item in layout(label, tag_type) if isinstance(item, TextItem)}
    price, unit = items["price"], items["unit_price"]
    assert price.bold and unit.bold
    image = _open(render(label, tag_type).png)
    price_h = _digit_ink_height(image, price)
    unit_h = _digit_ink_height(image, unit)
    assert unit_h * 10 >= price_h * 7, (price_h, unit_h)
    assert unit_h <= price_h  # the selling price stays the most prominent


# --- names: two lines, shrink, ellipsis, never into the price area ------------------


NAMES = [
    "Α",
    "ΑΣΠΙΡΊΝΗ",
    LONG_NAME,
    LONG_WORD,
    LONG_NAME + " " + LONG_NAME,
    "ΪΫΆΈΉΊΌΎΏ ΐΰ άέήίόύώ " * 6,
    "x" * 400,
]


@pytest.mark.parametrize("name", range(len(NAMES)))
@pytest.mark.parametrize("template", [TemplateKey.STANDARD, TemplateKey.UNIT_PRICE,
                                      TemplateKey.PROMO])  # fmt: skip
def test_name_stays_in_its_area(name: int, template: TemplateKey) -> None:
    label = LabelData(
        template, name=NAMES[name], brand="Ήπιο Ώμέγα Ϊ", price_text="999,99 €",
        unit_price_text="19999,80 €/l", prior_price_text="999,99 €", promo_until_text="31/12",
    )  # fmt: skip
    items = layout(label, BWR)
    texts = [item for item in items if isinstance(item, TextItem)]
    name_lines = [item for item in texts if item.role == "name"]
    assert 1 <= len(name_lines) <= 2
    inside = [item.box.contains(item.ink) for item in texts]
    assert all(inside)
    inside_tag = [i.ink.left >= 0 and i.ink.right <= 296 and i.ink.top >= 0 and i.ink.bottom <= 128
                  for i in texts]  # fmt: skip
    assert all(inside_tag)
    name_box = name_lines[0].box
    others = {item.box for item in texts if item.role != "name"}
    overlapping = [box for box in others if box.overlaps(name_box)]
    assert overlapping == []


def test_long_name_is_truncated_with_an_ellipsis_on_two_lines() -> None:
    label = LabelData(TemplateKey.STANDARD, name=LONG_NAME + " " + LONG_NAME, price_text="1,00 €")
    lines = [i for i in layout(label, BWR) if isinstance(i, TextItem) and i.role == "name"]
    ends_with_ellipsis = lines[-1].text.endswith(ELLIPSIS)
    assert len(lines) == 2
    assert ends_with_ellipsis, ascii(lines[-1].text)


def test_name_shrinks_before_it_is_truncated() -> None:
    short = LabelData(TemplateKey.STANDARD, name="ΑΣΠΙΡΊΝΗ", price_text="1,00 €")
    longer = LabelData(TemplateKey.STANDARD, name=LONG_NAME, price_text="1,00 €")
    size = {
        key: [i.size for i in layout(label, BWR) if isinstance(i, TextItem) and i.role == "name"]
        for key, label in (("short", short), ("long", longer))
    }
    assert size["short"][0] > size["long"][0]
    untruncated = [i.text.endswith(ELLIPSIS) for i in layout(short, BWR) if isinstance(i, TextItem)]
    assert not any(untruncated)


def test_word_too_long_for_a_line_is_broken_not_overflowed() -> None:
    label = LabelData(TemplateKey.STANDARD, name=LONG_WORD, price_text="1,00 €")
    lines = [i for i in layout(label, BWR) if isinstance(i, TextItem) and i.role == "name"]
    joined_ok = "".join(line.text for line in lines).rstrip(ELLIPSIS) in LONG_WORD
    assert len(lines) == 2
    assert joined_ok


# --- required fields and colours ----------------------------------------------------


def test_promo_on_a_bw_tag_is_refused() -> None:
    label, _ = CASES["promo_bwr"]
    with pytest.raises(ValueError):
        render(label, BW)


@pytest.mark.parametrize(
    "label",
    [
        LabelData(TemplateKey.STANDARD, name="Α"),
        LabelData(TemplateKey.STANDARD, price_text="1,00 €"),
        LabelData(TemplateKey.UNIT_PRICE, name="Α", price_text="1,00 €"),
        LabelData(TemplateKey.PROMO, name="Α", price_text="1,00 €", promo_until_text="30/11"),
        LabelData(TemplateKey.PROMO, name="Α", price_text="1,00 €", prior_price_text="2,00 €"),
        LabelData(TemplateKey.UNASSIGNED, short_code="A1"),
        LabelData(TemplateKey.UNASSIGNED, tag_model="gicisky 2.9"),
    ],
    ids=["no-price", "no-name", "no-unit", "no-prior", "no-until", "no-model", "no-code"],
)
def test_missing_required_field_is_refused(label: LabelData) -> None:
    with pytest.raises(ValueError):
        render(label, BWR)


def test_other_tag_sizes_render_within_bounds() -> None:
    big = TagType(key="test-4.2", vendor="test", model="4.2", width=400, height=300,
                  palette=Palette.BWR)  # fmt: skip
    for name in ("standard_bwr_long_name_max_price", "promo_bwr_unit_long_name", "unassigned_bwr"):
        label, _ = CASES[name]
        assert _open(render(label, big).png).size == (400, 300)


def test_domain_label_data_renders() -> None:
    """``build_label_data`` output is accepted by every template it can produce."""
    from datetime import UTC, datetime, timedelta

    from core.domain.pricing import build_label_data, unassigned_label_data
    from core.domain.types import (
        PackUnit,
        PriceKind,
        PricePoint,
        PriceSource,
        PriceState,
        ProductFacts,
    )

    now = datetime(2026, 12, 1, 8, 0, tzinfo=UTC)
    product = ProductFacts(1, "5200000000001", "Κρέμα χεριών", "Korres", 200_000,
                           PackUnit.GRAM, True, False)  # fmt: skip
    price = PriceState(1000, 800, now, now + timedelta(days=7))
    history = [PricePoint(1, 1000, PriceKind.REGULAR, now - timedelta(days=60), PriceSource.IMPORT)]
    for template in (TemplateKey.STANDARD, TemplateKey.UNIT_PRICE, TemplateKey.PROMO):
        render(build_label_data(product, price, history, template, now), BWR)
    render(unassigned_label_data("A7K3", "gicisky 2.9"), BWR)


# --- render store (database) ------------------------------------------------------


@pytest.fixture
def tag_type_row(db: Session) -> TagType:
    key = f"test-{uuid.uuid4().hex[:12]}"
    db.add(TagTypeRow(key=key, vendor="test", model=key, width=296, height=128, palette="BWR"))
    db.flush()
    return TAG.model_copy(update={"key": key})


@pytest.mark.db
def test_render_store_reuses_rows_by_hash(db: Session, tag_type_row: TagType) -> None:
    digest = ensure_render(db, LABEL, tag_type_row)
    assert digest == label_hash(LABEL, tag_type_row)
    assert ensure_render(db, LABEL, tag_type_row) == digest
    count = db.scalar(select(func.count()).select_from(RenderRow).where(RenderRow.hash == digest))
    assert count == 1
    assert load_png(db, digest) == render(LABEL, tag_type_row).png


@pytest.mark.db
def test_render_store_ignores_a_duplicate_insert(db: Session, tag_type_row: TagType) -> None:
    out = render(LABEL, tag_type_row)
    row_id = db.scalar(select(TagTypeRow.id).where(TagTypeRow.key == tag_type_row.key))
    assert row_id is not None
    save_render(db, out, row_id)
    save_render(db, out, row_id)
    count = db.scalar(select(func.count()).select_from(RenderRow).where(RenderRow.hash == out.hash))
    assert count == 1


@pytest.mark.db
def test_render_store_needs_a_known_tag_type(db: Session) -> None:
    unknown = TAG.model_copy(update={"key": f"missing-{uuid.uuid4().hex[:12]}"})
    with pytest.raises(LookupError):
        ensure_render(db, LABEL, unknown)
    assert load_png(db, "0" * 64) is None

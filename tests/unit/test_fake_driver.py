"""The fake driver follows the ``TagDriver`` contract (PLAN.md section 8)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from gateway.drivers.base import PushError, TagDriver
from gateway.drivers.fake import FAKE_TAGS_ENV, FakeDriver, FakeTag, png_mismatch, safe_filename
from shared.tag_types import PALETTE_COLOURS, Palette, TagType

BWR = TagType(
    key="fake-bwr", vendor="fake", model="2.9-bwr", width=296, height=128, palette=Palette.BWR
)


def palette_png(
    tag_type: TagType = BWR,
    *,
    size: tuple[int, int] | None = None,
    colours: tuple[tuple[int, int, int], ...] | None = None,
    index: int = 1,
) -> bytes:
    """A palette PNG with a filled rectangle drawn in palette ``index``."""
    colours = colours or PALETTE_COLOURS[tag_type.palette]
    img = Image.new("P", size or (tag_type.width, tag_type.height), 0)
    img.putpalette([c for rgb in colours for c in rgb])
    for x in range(10, 60):
        for y in range(5, 40):
            img.putpixel((x, y), index)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _driver(tmp_path: Path, **kw: object) -> FakeDriver:
    tags = [FakeTag("AA:BB:CC:00:00:01", "2.9-bwr"), FakeTag("FAKE-2", "2.9-bwr", reachable=False)]
    return FakeDriver(tags, output_dir=tmp_path / "fake_tags", **kw)  # type: ignore[arg-type]


def test_is_a_tag_driver(tmp_path: Path) -> None:
    assert isinstance(_driver(tmp_path), TagDriver)


async def test_push_writes_png_named_after_vendor_id(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    png = palette_png()
    result = await driver.push_image("AA:BB:CC:00:00:01", BWR, png)
    assert result.ok and result.error is None and result.duration_s >= 0
    written = tmp_path / "fake_tags" / "AA_BB_CC_00_00_01.png"
    assert written.read_bytes() == png
    assert driver.image_path("AA:BB:CC:00:00:01") == written


@pytest.mark.parametrize(
    "png",
    [
        palette_png(size=(128, 296)),
        palette_png(colours=((255, 255, 255), (0, 0, 0), (0, 0, 255))),
        palette_png(colours=(*PALETTE_COLOURS[Palette.BWRY],), index=3),  # yellow on BWR
        b"not a png",
        palette_png()[:-20],
    ],
    ids=["size", "colours", "index", "garbage", "truncated"],
)
async def test_mismatching_png_is_rejected_without_contacting_tag(
    tmp_path: Path, png: bytes
) -> None:
    driver = _driver(tmp_path)
    result = await driver.push_image("AA:BB:CC:00:00:01", BWR, png)
    assert (result.ok, result.error) == (False, PushError.REJECTED)
    assert driver.pushes == []
    assert not (tmp_path / "fake_tags").exists()


def test_rgb_png_is_rejected() -> None:
    out = io.BytesIO()
    Image.new("RGB", (296, 128), (255, 255, 255)).save(out, format="PNG")
    assert png_mismatch(out.getvalue(), BWR) == "not a palette PNG"


@pytest.mark.parametrize("palette", list(Palette))
def test_matching_png_for_every_palette(palette: Palette) -> None:
    tag_type = BWR.model_copy(update={"palette": palette})
    assert (
        png_mismatch(palette_png(tag_type, index=len(PALETTE_COLOURS[palette]) - 1), tag_type)
        is None
    )


async def test_unknown_or_unreachable_tag_is_not_found(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    for vendor_id in ("nope", "FAKE-2"):
        result = await driver.push_image(vendor_id, BWR, palette_png())
        assert result.error is PushError.NOT_FOUND


async def test_discover_returns_reachable_tags(tmp_path: Path) -> None:
    seen = await _driver(tmp_path).discover(timeout_s=0.1)
    assert [(s.vendor_id, s.model) for s in seen] == [("AA:BB:CC:00:00:01", "2.9-bwr")]
    assert seen[0].seen_at.tzinfo is not None


async def test_status(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    assert (await driver.status("AA:BB:CC:00:00:01")).reachable  # type: ignore[union-attr]
    assert not (await driver.status("FAKE-2")).reachable  # type: ignore[union-attr]


async def test_injected_failures_are_deterministic_with_a_seed(tmp_path: Path) -> None:
    async def run(seed: int) -> list[bool]:
        driver = _driver(tmp_path, fail_rate=0.5, fail_with=PushError.PROTOCOL, seed=seed)
        out = []
        for _ in range(20):
            r = await driver.push_image("AA:BB:CC:00:00:01", BWR, palette_png())
            assert r.ok or r.error is PushError.PROTOCOL
            out.append(r.ok)
        return out

    first = await run(7)
    assert first == await run(7)
    assert 0 < sum(first) < 20


async def test_fail_rate_one_always_fails_with_chosen_code(tmp_path: Path) -> None:
    driver = _driver(tmp_path, fail_rate=1.0, fail_with=PushError.TIMEOUT)
    r = await driver.push_image("AA:BB:CC:00:00:01", BWR, palette_png())
    assert (r.ok, r.error, r.detail) == (False, PushError.TIMEOUT, "injected failure")
    assert driver.pushes == [("AA:BB:CC:00:00:01", False)]


async def test_delay(tmp_path: Path) -> None:
    r = await _driver(tmp_path, delay_s=0.05).push_image("AA:BB:CC:00:00:01", BWR, palette_png())
    assert r.ok and r.duration_s >= 0.04


def test_config_from_json_file_via_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "tags.json"
    config = {
        "fail_rate": 0.25,
        "fail_with": "not_found",
        "seed": 3,
        "tags": [{"vendor_id": "F1", "model": "m", "battery_mv": 2900, "firmware": "1.0"}],
    }
    path.write_bytes(json.dumps(config).encode())
    monkeypatch.setenv(FAKE_TAGS_ENV, str(path))
    driver = FakeDriver(output_dir=tmp_path)
    assert driver.tags["F1"].battery_mv == 2900
    assert (driver.fail_rate, driver.fail_with) == (0.25, PushError.NOT_FOUND)


def test_config_from_toml_file_and_overrides(tmp_path: Path) -> None:
    path = tmp_path / "tags.toml"
    path.write_bytes(b'delay_s = 2.0\n[[tags]]\nvendor_id = "F1"\nmodel = "m"\nrssi = -70\n')
    driver = FakeDriver(config_path=path, delay_s=0.0)
    assert driver.tags["F1"].rssi == -70
    assert driver.delay_s == 0.0
    assert driver.output_dir == Path("fake_tags")


def test_bad_config_is_refused(tmp_path: Path) -> None:
    from gateway.drivers.fake import FakeConfigError

    path = tmp_path / "tags.json"
    path.write_bytes(b'{"tags": [{"vendor_id": "x", "colour": "red"}]}')
    with pytest.raises(FakeConfigError):
        FakeDriver(config_path=path)
    with pytest.raises(FakeConfigError):
        FakeDriver([], fail_rate=1.5)


def test_safe_filename() -> None:
    assert safe_filename("AA:BB/..\\x") == "AA_BB_.._x"

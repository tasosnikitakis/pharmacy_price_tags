"""Fake tag driver (PLAN.md section 8): pretend tags that write each pushed PNG to disk.

Used by every end-to-end test and by development without hardware. It follows the
``TagDriver`` contract like a real driver would:

- a PNG that does not match ``tag_type`` (size, palette, pixel indices) is
  ``rejected`` without "contacting" the tag;
- a tag that is not configured, or configured as unreachable, is ``not_found``;
- it never raises for tag-side problems.

It can be told to fail a fraction of pushes with a chosen ``PushError`` and to add a
delay, with a seed for deterministic tests.

Configuration file (JSON, or TOML when the name ends in ``.toml``)::

    fail_rate = 0.2          # optional, 0.0 to 1.0
    fail_with = "timeout"    # optional, a PushError value
    delay_s = 0.5            # optional, per push
    seed = 1                 # optional

    [[tags]]
    vendor_id = "FAKE-0001"
    model = "2.9-bwr"        # matched by the core to tag_type.model (vendor "fake")
    rssi = -60               # optional
    battery_mv = 2900        # optional
    battery_pct = 80         # optional
    firmware = "fake-1"      # optional
    reachable = true         # optional; false: not discovered, pushes are not_found

Constructor arguments override the file. The file path comes from the
``GATEWAY_FAKE_TAGS`` environment variable when no path is given.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import struct
import time
import tomllib
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gateway.drivers.base import PushError, PushResult, SeenTag, TagStatus
from shared.tag_types import PALETTE_COLOURS, TagType

FAKE_TAGS_ENV = "GATEWAY_FAKE_TAGS"
DEFAULT_OUTPUT_DIR = Path("fake_tags")


@dataclass(frozen=True, slots=True)
class FakeTag:
    """One pretend tag."""

    vendor_id: str
    model: str | None
    rssi: int | None = -60
    battery_mv: int | None = None
    battery_pct: int | None = None
    firmware: str | None = None
    reachable: bool = True


class FakeConfigError(ValueError):
    """The fake tag configuration file is invalid."""


def load_fake_config(path: Path) -> dict[str, Any]:
    """Read a JSON or TOML fake-tag configuration file."""
    data = path.read_bytes()
    try:
        if path.suffix.lower() == ".toml":
            loaded: Any = tomllib.loads(data.decode("utf-8"))
        else:
            loaded = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise FakeConfigError(f"{path.name}: {exc}") from None
    if not isinstance(loaded, dict):
        raise FakeConfigError(f"{path.name}: top level must be an object")
    return loaded


def _tag_from(entry: Mapping[str, Any]) -> FakeTag:
    allowed = {f for f in FakeTag.__dataclass_fields__}
    unknown = set(entry) - allowed
    if unknown or "vendor_id" not in entry:
        raise FakeConfigError(f"bad tag entry (unknown keys: {sorted(unknown)})")
    return FakeTag(**dict(entry))


def safe_filename(vendor_id: str) -> str:
    """``vendor_id`` as a file name that works on Windows too (``AA:BB`` -> ``AA_BB``)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", vendor_id) or "_"


class FakeDriver:
    """A ``TagDriver`` whose tags are a configuration file and a folder of PNGs."""

    vendor = "fake"

    def __init__(
        self,
        tags: Sequence[FakeTag] | None = None,
        *,
        config_path: Path | None = None,
        output_dir: Path | None = None,
        fail_rate: float | None = None,
        fail_with: PushError | None = None,
        delay_s: float | None = None,
        seed: int | None = None,
    ) -> None:
        config: dict[str, Any] = {}
        if tags is None:
            if config_path is None and os.environ.get(FAKE_TAGS_ENV):
                config_path = Path(os.environ[FAKE_TAGS_ENV])
            if config_path is not None:
                config = load_fake_config(config_path)
            tags = [_tag_from(t) for t in config.get("tags", [])]
        self.tags: dict[str, FakeTag] = {t.vendor_id: t for t in tags}
        self.output_dir = output_dir if output_dir is not None else DEFAULT_OUTPUT_DIR
        self.fail_rate = float(fail_rate if fail_rate is not None else config.get("fail_rate", 0))
        if not 0.0 <= self.fail_rate <= 1.0:
            raise FakeConfigError("fail_rate must be between 0 and 1")
        self.fail_with = (
            fail_with
            if fail_with is not None
            else PushError(config.get("fail_with", PushError.TIMEOUT.value))
        )
        self.delay_s = float(delay_s if delay_s is not None else config.get("delay_s", 0))
        self._rng = random.Random(seed if seed is not None else config.get("seed"))
        #: Every push attempt that reached a tag, in order: (vendor_id, ok).
        self.pushes: list[tuple[str, bool]] = []

    async def discover(self, timeout_s: float) -> list[SeenTag]:
        now = datetime.now(UTC)
        return [
            SeenTag(
                vendor_id=t.vendor_id,
                model=t.model,
                rssi=t.rssi,
                battery_mv=t.battery_mv,
                battery_pct=t.battery_pct,
                seen_at=now,
                firmware=t.firmware,
            )
            for t in self.tags.values()
            if t.reachable
        ]

    async def push_image(self, vendor_id: str, tag_type: TagType, png: bytes) -> PushResult:
        started = time.monotonic()

        def result(error: PushError | None, detail: str | None = None) -> PushResult:
            return PushResult(
                ok=error is None,
                error=error,
                duration_s=max(0.0, time.monotonic() - started),
                detail=detail,
            )

        problem = png_mismatch(png, tag_type)
        if problem is not None:
            return result(PushError.REJECTED, problem)
        tag = self.tags.get(vendor_id)
        if tag is None or not tag.reachable:
            return result(PushError.NOT_FOUND, "tag not in range")
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        if self.fail_rate > 0 and self._rng.random() < self.fail_rate:
            self.pushes.append((vendor_id, False))
            return result(self.fail_with, "injected failure")
        try:
            self._write(vendor_id, png)
        except OSError as exc:
            self.pushes.append((vendor_id, False))
            return result(PushError.PROTOCOL, f"could not write image: {exc.strerror}")
        self.pushes.append((vendor_id, True))
        return result(None)

    async def status(self, vendor_id: str) -> TagStatus | None:
        tag = self.tags.get(vendor_id)
        if tag is None or not tag.reachable:
            return TagStatus(
                vendor_id, reachable=False, rssi=None, battery_mv=None, battery_pct=None
            )
        return TagStatus(vendor_id, True, tag.rssi, tag.battery_mv, tag.battery_pct)

    def image_path(self, vendor_id: str) -> Path:
        return self.output_dir / f"{safe_filename(vendor_id)}.png"

    def _write(self, vendor_id: str, png: bytes) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        target = self.image_path(vendor_id)
        tmp = target.with_suffix(".png.tmp")
        tmp.write_bytes(png)
        os.replace(tmp, target)


# A minimal PNG reader (standard library only, so the gateway does not need Pillow):
# enough to check that an image is a palette PNG of the tag's size and colours.

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_mismatch(png: bytes, tag_type: TagType) -> str | None:
    """Why ``png`` cannot be shown on ``tag_type``, or ``None`` if it can.

    Checks: a valid non-interlaced palette PNG, the tag's width and height, palette
    entries equal to the tag's colours in ``PALETTE_COLOURS`` order, and every pixel
    index within the tag's colours.
    """
    try:
        return _check_png(png, tag_type)
    except (ValueError, struct.error, zlib.error) as exc:
        return f"not a valid PNG: {exc}"


def _chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    if not png.startswith(_PNG_SIGNATURE):
        raise ValueError("bad signature")
    pos, chunks = len(_PNG_SIGNATURE), []
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        kind = png[pos + 4 : pos + 8]
        data = png[pos + 8 : pos + 8 + length]
        if len(data) != length:
            raise ValueError("truncated chunk")
        (crc,) = struct.unpack(">I", png[pos + 8 + length : pos + 12 + length])
        if zlib.crc32(kind + data) != crc:
            raise ValueError("bad chunk CRC")
        chunks.append((kind, data))
        pos += 12 + length
        if kind == b"IEND":
            return chunks
    raise ValueError("no IEND chunk")


def _check_png(png: bytes, tag_type: TagType) -> str | None:
    chunks = _chunks(png)
    if not chunks or chunks[0][0] != b"IHDR":
        raise ValueError("IHDR missing")
    width, height, depth, colour_type, _, _, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    if (width, height) != (tag_type.width, tag_type.height):
        return f"size {width}x{height}, tag needs {tag_type.width}x{tag_type.height}"
    if colour_type != 3:
        return "not a palette PNG"
    if depth not in (1, 2, 4, 8):
        raise ValueError(f"bit depth {depth}")
    if interlace:
        return "interlaced PNG"
    plte = b"".join(d for k, d in chunks if k == b"PLTE")
    entries = [tuple(plte[i : i + 3]) for i in range(0, len(plte) - len(plte) % 3, 3)]
    expected = PALETTE_COLOURS[tag_type.palette]
    usable = min(len(entries), len(expected))
    if usable == 0 or [tuple(e) for e in entries[:usable]] != list(expected[:usable]):
        return f"palette does not match {tag_type.palette.value}"
    raw = zlib.decompress(b"".join(d for k, d in chunks if k == b"IDAT"))
    highest = _max_index(raw, width, height, depth)
    if highest >= usable:
        return f"pixel index {highest} is outside the {tag_type.palette.value} palette"
    return None


def _max_index(raw: bytes, width: int, height: int, depth: int) -> int:
    """Highest palette index used, after undoing the PNG scanline filters."""
    stride = (width * depth + 7) // 8
    if len(raw) != height * (stride + 1):
        raise ValueError("image data has the wrong length")
    previous = bytearray(stride)
    highest = 0
    per_byte = 8 // depth
    mask = (1 << depth) - 1
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        line = bytearray(raw[start + 1 : start + 1 + stride])
        for i in range(stride):
            left = line[i - 1] if i else 0
            up = previous[i]
            corner = previous[i - 1] if i else 0
            if kind == 1:
                line[i] = (line[i] + left) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + up) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + ((left + up) >> 1)) & 0xFF
            elif kind == 4:
                line[i] = (line[i] + _paeth(left, up, corner)) & 0xFF
            elif kind != 0:
                raise ValueError(f"filter type {kind}")
        for x in range(width):
            byte = line[x // per_byte]
            shift = 8 - depth * (x % per_byte + 1)
            highest = max(highest, (byte >> shift) & mask)
        previous = line
    return highest


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c

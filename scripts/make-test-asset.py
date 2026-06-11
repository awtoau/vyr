#!/usr/bin/env python3
"""Generate the committed F6 test asset: vyr-core/tests/assets/checker-24.png
— plus its RAW twin vyr-size/assets/checker-24.rgba (same pixel() spec,
flattened straight RGBA, no PNG container) for the F9 size vehicle's
`include_bytes!` (no_std core cannot decode PNG; baking decoded pixels into
flash is exactly the F15 model the size number should reflect).

A 24x24 straight-alpha RGBA PNG, fully deterministic pixel spec (stdlib only
— zlib + struct, no PIL):

  - 1px near-black border ring          (20, 20, 20, 255)
  - TL quadrant: opaque red             (230, 40, 40, 255)
  - TR quadrant: opaque green           (40, 180, 90, 255)
  - BL quadrant: opaque blue            (40, 90, 230, 255)
  - BR quadrant: SEMI-TRANSPARENT yellow (255, 220, 0, 128)  <- the alpha-
        blend evidence: over a coloured backdrop the golden must show the mix
  - centre 4x4 hole: fully transparent  (0, 0, 0, 0)         <- the a==0 skip

The committed PNG is the source of truth (goldens pin its pixels via the IR
golden hash); this script exists so the asset is reproducible, not because
regeneration is routine. Output is logged to ./tmp/make-test-asset.log.
"""
from __future__ import annotations

import struct
import sys
import time
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "vyr-core" / "tests" / "assets" / "checker-24.png"
OUT_RAW = REPO / "vyr-size" / "assets" / "checker-24.rgba"
LOG = REPO / "tmp" / "make-test-asset.log"
SIZE = 24


def log(msg: str) -> None:
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1e6):06d} UTC"
    line = f"{stamp}  INFO  [make-test-asset] {msg}"
    print(line, file=sys.stderr)
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def pixel(x: int, y: int) -> tuple[int, int, int, int]:
    if x in (0, SIZE - 1) or y in (0, SIZE - 1):
        return (20, 20, 20, 255)                  # border ring
    if 10 <= x < 14 and 10 <= y < 14:
        return (0, 0, 0, 0)                       # transparent centre hole
    half = SIZE // 2
    if y < half:
        return (230, 40, 40, 255) if x < half else (40, 180, 90, 255)
    return (40, 90, 230, 255) if x < half else (255, 220, 0, 128)


def chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def main() -> int:
    raw = bytearray()
    raw_rgba = bytearray()
    for y in range(SIZE):
        raw.append(0)  # filter type 0 (None) per row — simplest, deterministic
        for x in range(SIZE):
            raw.extend(pixel(x, y))
            raw_rgba.extend(pixel(x, y))
    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)  # 8-bit RGBA
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(png)
    log(f"wrote {OUT} ({len(png)} bytes, {SIZE}x{SIZE} RGBA)")
    OUT_RAW.parent.mkdir(parents=True, exist_ok=True)
    OUT_RAW.write_bytes(raw_rgba)
    log(f"wrote {OUT_RAW} ({len(raw_rgba)} bytes raw straight-RGBA)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Build the engineering-gallery evidence images (docs/milestones/eng-*).

1. eng-band-clip-{full,banded,diff}.png — the ORIGINAL F1 band-equivalence
   failure pair (tmp/band-fail-*-30.png, kept from the discovery run) plus an
   amplified-diff visualisation: differing pixels dilated + painted red so the
   4 culprit pixels are visible at gallery scale.
2. eng-ring-zoom.png — nearest-neighbour 6x crop of the F1 golden's ring:
   what fixed-step polygon flattening + 1/64px quantization actually looks
   like at pixel level.
3. eng-text-zoom.png — 4x crop of the F5 golden's button: glyph-cache AA from
   integer-position A8 mask blits.

Output: docs/milestones/, log: tmp/make-eng-renders.log (timestamped).
"""
from __future__ import annotations

import time
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
OUT = REPO / "docs" / "milestones"
LOG = TMP / "make-eng-renders.log"
_lines: list[str] = []


def log(msg: str) -> None:
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1e6):06d} UTC"
    line = f"{stamp}  INFO  [eng-renders] {msg}"
    print(line, flush=True)
    _lines.append(line)


def amplified_diff(full: Image.Image, banded: Image.Image) -> tuple[Image.Image, int]:
    """Red-dilated visualisation of differing pixels over a faded base."""
    fa, ba = full.convert("RGB"), banded.convert("RGB")
    w, h = fa.size
    fpx, bpx = fa.load(), ba.load()
    diffs = [(x, y) for y in range(h) for x in range(w) if fpx[x, y] != bpx[x, y]]
    # Faded grayscale base so the red marks dominate.
    base = fa.convert("L").point(lambda v: 160 + v // 3).convert("RGB")
    out = base.load()
    R = 3  # dilation radius: 4 isolated pixels are invisible undilated
    for (x, y) in diffs:
        for dy in range(-R, R + 1):
            for dx in range(-R, R + 1):
                if dx * dx + dy * dy <= R * R and 0 <= x + dx < w and 0 <= y + dy < h:
                    out[x + dx, y + dy] = (220, 30, 30)
    return base, len(diffs)


def zoom(src: Path, box: tuple[int, int, int, int], factor: int, dst: Path) -> None:
    with Image.open(src) as im:
        crop = im.convert("RGB").crop(box)
        big = crop.resize((crop.width * factor, crop.height * factor), Image.NEAREST)
        big.save(dst)
    log(f"{dst.name}: {src.name} crop {box} x{factor} -> {big.size[0]}x{big.size[1]}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    full_p = TMP / "band-fail-full-30.png"
    band_p = TMP / "band-fail-banded-30.png"
    if full_p.is_file() and band_p.is_file():
        with Image.open(full_p) as f, Image.open(band_p) as b:
            f.convert("RGB").save(OUT / "eng-band-clip-full.png")
            b.convert("RGB").save(OUT / "eng-band-clip-banded.png")
            diff_img, n = amplified_diff(f, b)
            diff_img.save(OUT / "eng-band-clip-diff.png")
        log(f"band-clip pair archived; {n} differing pixels marked (dilated red)")
    else:
        log("WARN: band-fail PNGs missing from tmp/ — skip (regenerate by "
            "reverting the painter to transform-based draws, run the band test)")

    zoom(TMP / "golden-f1.png", (70, 18, 106, 54), 6, OUT / "eng-ring-zoom.png")
    zoom(TMP / "text-golden-f5.png", (10, 44, 110, 76), 4, OUT / "eng-text-zoom.png")

    LOG.write_text("\n".join(_lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

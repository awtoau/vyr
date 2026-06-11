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


def diagram_gutter(dst: Path) -> None:
    """Band + gutter explainer: the delivered band, the overscan margin, and
    where the renderer's clip edge actually sits."""
    from PIL import ImageDraw

    W, H, S = 520, 360, 2  # canvas + screen scale (1 IR px = 2 diagram px)
    sx, sy = 40, 50        # screen origin on the canvas
    img = Image.new("RGB", (W, H), (250, 250, 250))
    d = ImageDraw.Draw(img)

    def srect(x0, y0, x1, y1):  # screen coords -> canvas box
        return (sx + x0 * S, sy + y0 * S, sx + x1 * S, sy + y1 * S)

    # The 120x120 screen.
    d.rectangle(srect(0, 0, 120, 120), outline=(120, 120, 120), width=2)
    d.text((sx, sy - 28), "screen 120x120", fill=(60, 60, 60))
    # The gutter region (band grown by 8px on every side) — orange, BEHIND the band.
    d.rectangle(srect(-8, 52, 128, 98), fill=(255, 224, 178),
                outline=(230, 130, 0), width=2)
    # The delivered band (rows 60..90, full width) — blue, on top.
    d.rectangle(srect(0, 60, 120, 90), fill=(220, 230, 245),
                outline=(30, 90, 168), width=2)
    d.text((sx + 6, sy + 70 * S - 6), "band 120x30 - the pixels we DELIVER",
           fill=(30, 90, 168))
    d.text((sx + 132 * S, sy + 50 * S),
           "+8 px gutter on every side:\nrasterized, then thrown away.\n"
           "The renderer's CLIP EDGE lives\non the orange line - never next\n"
           "to a delivered pixel.",
           fill=(150, 80, 0))
    d.text((sx, sy + 124 * S),
           "cost: (120+16) x (30+16) rasterized for a 120x30 band - the overscan\n"
           "the F2 scaling table prices, and the thing a Fast/Draft tier can drop.",
           fill=(90, 90, 90))
    img.save(dst)
    log(f"{dst.name}: gutter diagram")


def diagram_dirty(dst: Path) -> None:
    """Dirty-rectangle explainer: a widget moves; what repaints."""
    from PIL import ImageDraw

    W, H, S = 520, 400, 2
    sx, sy = 40, 50
    img = Image.new("RGB", (W, H), (250, 250, 250))
    d = ImageDraw.Draw(img)

    def srect(x0, y0, x1, y1):
        return (sx + x0 * S, sy + y0 * S, sx + x1 * S, sy + y1 * S)

    d.rectangle(srect(0, 0, 120, 120), outline=(120, 120, 120), width=2)
    d.text((sx, sy - 28), "screen 120x120 - a widget moved this frame",
           fill=(60, 60, 60))
    # Dirty regions: WAS (must repaint the background) + NOW (paint the widget).
    d.rectangle(srect(14, 20, 58, 48), fill=(252, 228, 228),
                outline=(200, 60, 60), width=2)
    d.text((sx + 16 * S, sy + 22 * S), "WAS\n(repaint what's\nunderneath)",
           fill=(160, 40, 40))
    d.rectangle(srect(62, 64, 106, 92), fill=(220, 230, 245),
                outline=(30, 90, 168), width=2)
    d.text((sx + 64 * S, sy + 66 * S), "NOW\n(paint the widget\nhere)",
           fill=(30, 90, 168))
    # Everything else untouched.
    d.text((sx + 6 * S, sy + 104 * S),
           "everything else: NOT touched this frame", fill=(120, 120, 120))
    d.text((sx, sy + 128 * S),
           "dirty = WAS + NOW. Next frame the renderer walks the widget tree,\n"
           "SKIPS every op whose bbox misses the dirty rects, and repaints only\n"
           "inside them - banded through the small working buffer if they are\n"
           "tall. Tree order = z-order, so overlapping widgets repaint correctly.",
           fill=(90, 90, 90))
    img.save(dst)
    log(f"{dst.name}: dirty-rect diagram")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    diagram_gutter(OUT / "eng-diagram-gutter.png")
    diagram_dirty(OUT / "eng-diagram-dirty.png")

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

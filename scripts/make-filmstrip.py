#!/usr/bin/env python3
"""Filmstrip: pick N evenly-spaced frames from a PNG sequence dir and tile
them horizontally (1px separators) — the inline-chat-friendly view of an
animation. Usage: make-filmstrip.py <frames-dir> <out.png> [count=6]
Log: tmp/make-filmstrip.log (timestamped)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent


def log(msg: str) -> None:
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1e6):06d} UTC"
    line = f"{stamp}  INFO  [filmstrip] {msg}"
    print(line, flush=True)
    logp = REPO / "tmp" / "make-filmstrip.log"
    logp.parent.mkdir(exist_ok=True)
    with open(logp, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    frames_dir = Path(sys.argv[1])
    out = Path(sys.argv[2])
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    frames = sorted(frames_dir.glob("*.png"))
    if len(frames) < count:
        log(f"ERROR: only {len(frames)} frames in {frames_dir} (< {count})")
        return 1
    picks = [frames[round(i * (len(frames) - 1) / (count - 1))] for i in range(count)]
    images = [Image.open(p).convert("RGB") for p in picks]
    w, h = images[0].size
    sep = 1
    strip = Image.new("RGB", (count * w + (count - 1) * sep, h), (136, 136, 136))
    for i, im in enumerate(images):
        strip.paste(im, (i * (w + sep), 0))
    out.parent.mkdir(parents=True, exist_ok=True)
    strip.save(out)
    log(f"{out}: {count} frames from {frames_dir.name} "
        f"({picks[0].name} .. {picks[-1].name}), {strip.size[0]}x{strip.size[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

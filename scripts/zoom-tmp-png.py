#!/usr/bin/env python3
"""Zoom a ./tmp PNG with nearest-neighbour for pixel eyeballing.

Usage: zoom-tmp-png.py <in.png> <out.png> [factor]
Output + log to ./tmp (awto logging rules).
"""

import sys
import time
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"


def log(msg: str) -> None:
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1e6):06d} UTC"
    line = f"{stamp}  INFO  [zoom-tmp-png] {msg}"
    print(line, file=sys.stderr)
    TMP.mkdir(exist_ok=True)
    with open(TMP / "zoom-tmp-png.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    factor = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    im = Image.open(src)
    out = im.resize((im.width * factor, im.height * factor), Image.NEAREST)
    out.save(dst)
    log(f"{src} -> {dst} at {factor}x ({out.width}x{out.height})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

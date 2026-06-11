#!/usr/bin/env python3
"""make-subset-font.py — regenerate vyr-size/assets/roboto-ascii.ttf.

The LVGL-style cut-down font for the F9 RUNNABLE M4 vehicle (#9):
printable-ASCII-only subset of fonts/roboto.ttf, hinting dropped. LVGL's
font converter ships exactly this shape by default (ASCII range, no hinting
bytecode — LVGL rasterizes unhinted); vyr draws unhinted too (skrifa
DrawSettings), so the dropped tables are dead weight on MCU.

Exact subsetter flags (documented here AND in assets/roboto-ascii.md):
  --unicodes=U+0020-007E   printable ASCII only
  --no-hinting             strip TrueType hinting bytecode (fpgm/prep/cvt
                           + per-glyph instructions) — vyr renders unhinted
  --layout-features=       drop ALL OpenType layout (GSUB/GPOS incl. kern):
                           vyr's text path places glyphs by plain advances,
                           no shaping, so layout tables are unreachable
  --name-IDs=1,2,3,6       keep only the names needed to identify the face
  --notdef-outline         keep the visible .notdef box (honest failure —
                           though vyr hard-errors on unmapped codepoints
                           before any tofu could render)

NEVER touches fonts/roboto.ttf — the goldens pin its exact rasterization.

Usage: python3 scripts/make-subset-font.py [--pyftsubset /path/to/pyftsubset]
Output + log: ./tmp/make-subset-font.log
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "fonts" / "roboto.ttf"
OUT = REPO / "vyr-size" / "assets" / "roboto-ascii.ttf"
LOG = REPO / "tmp" / "make-subset-font.log"
_lines: list[str] = []


def log(msg: str) -> None:
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1e6):06d} UTC"
    line = f"{stamp}  INFO  [make-subset-font] {msg}"
    print(line, flush=True)
    _lines.append(line)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--pyftsubset",
        default=shutil.which("pyftsubset"),
        help="path to fonttools' pyftsubset (default: from PATH)",
    )
    args = ap.parse_args()
    if not args.pyftsubset:
        log("ERROR: pyftsubset not found — pip install fonttools, or pass --pyftsubset")
        return 1
    cmd = [
        args.pyftsubset,
        str(SRC),
        "--unicodes=U+0020-007E",
        "--no-hinting",
        "--layout-features=",
        "--name-IDs=1,2,3,6",
        "--notdef-outline",
        f"--output-file={OUT}",
    ]
    log("exact command: " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        log(f"ERROR: pyftsubset rc={r.returncode}: {r.stderr.strip()[:400]}")
        return r.returncode
    full, sub = SRC.stat().st_size, OUT.stat().st_size
    log(f"full {SRC.name}: {full:,} B → subset {OUT.name}: {sub:,} B "
        f"({sub / full:.1%} of full, −{full - sub:,} B)")
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("\n".join(_lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""gen-sintab.py — emit the 256-entry Q12 sine table vyr-scene embeds.

The long scene's orbits need circular motion, and the scene generator must
cost (almost) nothing on the M4: `libm::sinf` promotes to f64 and is ~1,145
instructions per call on an M4F (docs/measurements/perf-history.md, #32), so
a scene that called it would price its own animation driver into every
insns/frame number. A committed integer table costs a load.

Q12 (4096 = 1.0) keeps r*sin(θ) exact in i32 for any radius that fits a
screen, and the table is written once into vyr-scene/src/lib.rs — this
script exists so the constants are reproducible, not so they are generated
at build time.

Output: tmp/gen-sintab.log (the table itself goes to stdout).
"""
from __future__ import annotations

import datetime
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
N = 256
SCALE = 4096


def main() -> int:
    vals = [math.floor(SCALE * math.sin(2 * math.pi * i / N) + 0.5) for i in range(N)]
    rows = []
    for i in range(0, N, 8):
        rows.append("    " + ", ".join(f"{v:5d}" for v in vals[i:i + 8]) + ",")
    table = "\n".join(rows)
    print(table)
    TMP.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    (TMP / "gen-sintab.log").write_text(
        f"[{stamp}] gen-sintab: N={N} scale={SCALE} "
        f"min={min(vals)} max={max(vals)}\n{table}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""present-compare-runs.py — diff two scripts/board-present.py JSON runs.

Reproducibility is the claim that makes a single DWT number evidence rather
than an anecdote: the firmware is deterministic, single-threaded, interrupt-free
and has no clock in the render path, so two freshly-flashed runs should agree to
within bus-contention drift. This prints the per-cell delta in ppm and the
worst case, so "reproducible" is a measured statement and not an assertion.

Usage: python3 scripts/present-compare-runs.py A.json B.json
Log: tmp/present-compare-runs.log
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOG = REPO / "tmp" / "present-compare-runs.log"

SCALARS = (
    "pixels_identical", "swap_correct", "swap_cycles", "ccm_w_mbps",
    "sdram_w_rgb565scan_mbps", "sdram_w_rgb888scan_mbps",
    "sdram_w_layeroff_mbps", "sdram_r_rgb565scan_mbps",
    "phase_blank_mean", "phase_active_mean", "phase_active_penalty_pct",
    "dirty_n", "dirty_px", "frame_hash", "cycles_per_frame", "heap_peak",
)


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    a = json.loads(Path(sys.argv[1]).read_text())["run"]
    b = json.loads(Path(sys.argv[2]).read_text())["run"]
    out = []

    def say(s):
        print(s)
        out.append(s)

    ka = {(r["tier"], r["rect"], r["mode"]): r for r in a["rows"]}
    kb = {(r["tier"], r["rect"], r["mode"]): r for r in b["rows"]}
    say(f"{'cell':<30}{'run1 render':>14}{'run2 render':>14}"
        f"{'ppm':>8}{'run1 blit':>12}{'run2 blit':>12}{'ppm':>8}")
    worst = 0
    hashes_ok = True
    for k in sorted(ka):
        if k not in kb:
            say(f"{'/'.join(k):<30} MISSING in run 2")
            continue
        r1, r2 = ka[k], kb[k]
        dr = round(1e6 * (r2["render"] - r1["render"]) / r1["render"])
        db = (round(1e6 * (r2["blit"] - r1["blit"]) / r1["blit"])
              if r1["blit"] else 0)
        worst = max(worst, abs(dr), abs(db))
        hashes_ok &= r1["hash"] == r2["hash"]
        say(f"{'/'.join(k):<30}{r1['render']:>14,}{r2['render']:>14,}"
            f"{dr:>8}{r1['blit']:>12,}{r2['blit']:>12,}{db:>8}")
    say(f"worst |delta| across all cells: {worst} ppm")
    say(f"every cell's frame hash identical across runs: {hashes_ok}")
    for k in SCALARS:
        flag = "" if a.get(k) == b.get(k) else "   <-- differs"
        say(f"  {k:<28} {a.get(k)!r:>22} | {b.get(k)!r:<22}{flag}")
    LOG.parent.mkdir(exist_ok=True)
    LOG.write_text("\n".join(out) + "\n")
    print(f"\nwrote {LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""perf-replay-summary.py — read tmp/perf-replay.jsonl and print the history as
a table: one row per specimen, render-only / total / fold per tier, plus the
frame hash so a scene change is visible rather than inferred.

Usage: python3 scripts/perf-replay-summary.py [--file tmp/perf-replay.jsonl]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TIERS = ("exact", "fast", "draft")


def cell(rec, plat, tier, opt="z"):
    for c in rec.get("cells", []):
        if c["platform"] == plat and c["tier"] == tier and c["opt_level"] == opt \
                and c["status"] == "measured":
            return c
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(REPO / "tmp" / "perf-replay.jsonl"))
    a = ap.parse_args()
    recs = [json.loads(ln) for ln in Path(a.file).read_text().splitlines() if ln.strip()]
    hdr = f"{'commit':9} {'date':11} " + "".join(
        f"{t + ' render-only':>20}" for t in TIERS) + "  exact hash"
    print(hdr)
    print("-" * len(hdr))
    for r in recs:
        sp = r["specimen"]
        if "cells" not in r:
            print(f"{sp['commit']:9} {sp.get('date', '')[:10]:11} SKIPPED: {r.get('reason')}")
            continue
        line = f"{sp['commit']:9} {sp.get('date', '')[:10]:11}"
        for t in TIERS:
            c = cell(r, "qemu-m4", t)
            v = c["metrics"]["insns_per_frame_render_only"] if c else None
            line += f"{v:>20,}" if v else f"{'—':>20}"
        ex = cell(r, "qemu-m4", "exact")
        line += "  " + (ex["metrics"]["frame_hash"] or "—" if ex else "—")
        print(line)

    print()
    print("fold (insns/frame) and its share of the measured total, last specimen:")
    last = [r for r in recs if "cells" in r][-1]
    for t in TIERS:
        c = cell(last, "qemu-m4", t)
        if not c:
            continue
        m = c["metrics"]
        print(f"  {t:6} total {m['insns_per_frame_total']:>12,}  fold {m['harness_fold_insns']:>10,}"
              f"  = {100 * m['harness_fold_insns'] / m['insns_per_frame_total']:5.1f}%"
              f"  render-only {m['insns_per_frame_render_only']:>12,}"
              f"  heap {m['heap_peak_b']:>8,} B  flash {m['flash_text_data_b']:>8,} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

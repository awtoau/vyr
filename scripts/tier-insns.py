#!/usr/bin/env python3
"""tier-insns.py — EXACT M4 instruction counts for every vyr quality tier.

#27: the middle `Quality::Fast` tier has to be priced against BOTH its
neighbours on the SAME vehicle, with the same tool, in one run — quoting a
Fast number taken today against an Exact number taken last month is exactly
the provenance failure docs/performance.md §5 exists to prevent.

For each tier it:
  1. builds vyr-size for thumbv7em-none-eabihf with that tier's feature
     (CARGO_INCREMENTAL=0 — incremental codegen-unit boundaries move the
     instruction count by ~50% between otherwise identical builds),
  2. runs scripts/qemu-insn.py on the ELF (plugin QEMU + libinsn: an exact
     architectural instruction count, not SYS_CLOCK wall time),
  3. records insns/frame, insns/px, the frame hash and the ELF SHA-256.

Output: tmp/tier-insns.json   Log: tmp/tier-insns.log
Usage:  python3 scripts/tier-insns.py [--repeat 2] [--tiers exact,fast,draft]
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
LOG = TMP / "tier-insns.log"
# release-mcu, NOT release: the MCU profile is opt-level="z" + fat LTO +
# panic=abort, which is what dev.py qemu-m4 builds and what every committed
# M4 anchor was measured on. Measuring opt-level=3 here and comparing against
# those anchors — or against the LVGL harness, which compiles -Os — would be
# an apples-to-oranges speed advantage, not a real one.
ELF = REPO / "target" / "thumbv7em-none-eabihf" / "release-mcu" / "vyr-size"
PIXELS = 480 * 270

FEATURES = {
    "exact": "run-qemu",
    "fast": "run-qemu,fast",
    "draft": "run-qemu,draft",
}

_lines: list[str] = []


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    _lines.append(line)


def flush() -> None:
    TMP.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(_lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--tiers", default="exact,fast,draft")
    opts = ap.parse_args()

    out: dict[str, dict] = {}
    for tier in opts.tiers.split(","):
        tier = tier.strip()
        if tier not in FEATURES:
            log(f"unknown tier {tier}")
            return 2
        log(f"=== {tier}: build ===")
        rc = subprocess.run(
            [
                "cargo", "build", "--profile", "release-mcu", "-p", "vyr-size",
                "--target", "thumbv7em-none-eabihf",
                "--no-default-features", "--features", FEATURES[tier],
            ],
            cwd=REPO,
            env={**__import__("os").environ, "CARGO_INCREMENTAL": "0"},
            capture_output=True,
            text=True,
        )
        if rc.returncode != 0:
            log(rc.stdout + rc.stderr)
            return 1
        log(f"=== {tier}: count ===")
        r = subprocess.run(
            [sys.executable, "scripts/qemu-insn.py", str(ELF),
             "--name", f"tier-{tier}", "--repeat", str(opts.repeat)],
            cwd=REPO, capture_output=True, text=True,
        )
        sys.stdout.write(r.stdout[-2000:])
        if r.returncode != 0:
            log(r.stdout[-4000:] + r.stderr[-4000:])
            return 1
        data = json.loads((TMP / f"qemu-insn-tier-{tier}.json").read_text())
        out[tier] = data
        per = data.get("insns_per_frame")
        log(f"{tier}: {per:,} insns/frame = {per / PIXELS:.1f} insn/px" if per else f"{tier}: ?")

    if "exact" in out and "draft" in out and "fast" in out:
        e = out["exact"]["insns_per_frame"]
        f = out["fast"]["insns_per_frame"]
        d = out["draft"]["insns_per_frame"]
        log(f"#27 VERDICT  Exact {e:,} / Fast {f:,} / Draft {d:,} insns/frame")
        log(f"#27          Fast = {f / d:.2f}x Draft, {e / f:.2f}x cheaper than Exact, "
            f"= {100.0 * (f - d) / (e - d):.0f}% of the Exact-over-Draft bill")
    (TMP / "tier-insns.json").write_text(json.dumps(out, indent=2) + "\n")
    log(f"wrote {TMP / 'tier-insns.json'}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        flush()

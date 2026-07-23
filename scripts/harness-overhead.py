#!/usr/bin/env python3
"""harness-overhead.py — how much of each measured "frame" is the BENCHMARK,
not the renderer?

Both M4 vehicles fold every output byte of every band into an FNV-1a hash
(vyr: `workload::render_frame_banded`; LVGL: `flush_cb`) so the frame is
verifiably materialised. That fold is 388,800 bytes per frame on both sides and
it is NOT rendering. The LVGL side is already measured exactly — `flush_cb` is
its own symbol and `scripts/m4-attribute.py` prices it — but vyr's fold is
inlined into `render_frame_banded`, so the only way to price it is to build the
same firmware with the fold folding nothing and diff the instruction counts.

This script does that TEMPORARILY: it copies the source aside, patches the fold
to run over an empty slice, rebuilds, measures with the plugin QEMU, and
RESTORES the original file in a finally block. Nothing is left modified.

Output: tmp/harness-overhead.json / tmp/harness-overhead.log
Usage:  python3 scripts/harness-overhead.py [--tiers exact,draft]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
LOG = TMP / "harness-overhead.log"
SRC = REPO / "vyr-size" / "src" / "workload.rs"
BACKUP = TMP / "harness-overhead-workload.rs.orig"
ELF = REPO / "target" / "thumbv7em-none-eabihf" / "release-mcu" / "vyr-size"
FEATURES = {"exact": "run-qemu", "fast": "run-qemu,fast", "draft": "run-qemu,draft"}
PIXELS = 480 * 270
BYTES_PER_FRAME = 480 * 270 * 3

_lines: list[str] = []


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line, flush=True)
    _lines.append(line)
    LOG.write_text("\n".join(_lines) + "\n")


def measure(tier: str, tag: str, opt: str | None = None) -> int | None:
    cfg = ["--config", f"profile.release-mcu.opt-level={opt}"] if opt else []
    r = subprocess.run(
        ["cargo", "build", "--profile", "release-mcu", "-p", "vyr-size",
         "--target", "thumbv7em-none-eabihf", "--no-default-features",
         "--features", FEATURES[tier]] + cfg,
        cwd=REPO, capture_output=True, text=True,
        env={**os.environ, "CARGO_INCREMENTAL": "0"})
    if r.returncode != 0:
        log("BUILD FAILED\n" + r.stdout[-2000:] + r.stderr[-2000:])
        return None
    q = subprocess.run([sys.executable, "scripts/qemu-insn.py", str(ELF),
                        "--name", tag, "--repeat", "1"],
                       cwd=REPO, capture_output=True, text=True)
    if q.returncode != 0:
        log("QEMU FAILED\n" + q.stdout[-2000:] + q.stderr[-2000:])
        return None
    data = json.loads((TMP / f"qemu-insn-{tag}.json").read_text())
    return data.get("insns_per_frame")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="exact,draft")
    ap.add_argument("--opt", default=None,
                    help='opt-level override, e.g. 3 or \'"s"\' '
                         '(the profile default is "z")')
    a = ap.parse_args()
    tiers = [t.strip() for t in a.tiers.split(",")]
    out: dict = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "bytes_hashed_per_frame": BYTES_PER_FRAME, "tiers": {}}
    original = SRC.read_text()
    BACKUP.write_text(original)
    needle = "fnv1a_fold(hash, core::hint::black_box(&band_buf[..len]))"
    if needle not in original:
        log(f"FATAL: fold call not found verbatim in {SRC}")
        return 2
    try:
        for tier in tiers:
            SRC.write_text(original)
            base = measure(tier, f"ho-{tier}-with-hash", a.opt)
            SRC.write_text(original.replace(
                needle, "fnv1a_fold(hash, core::hint::black_box(&band_buf[..0]))"))
            nohash = measure(tier, f"ho-{tier}-no-hash", a.opt)
            if base is None or nohash is None:
                return 1
            delta = base - nohash
            out["tiers"][f"{tier}@{a.opt or 'z'}"] = {
                "insns_per_frame_with_hash": base,
                "insns_per_frame_without_hash": nohash,
                "harness_hash_insns_per_frame": delta,
                "insns_per_hashed_byte": delta / BYTES_PER_FRAME,
                "render_only_insn_per_px": nohash / PIXELS,
            }
            log(f"{tier}@{a.opt or 'z'}: with hash {base:,} / without {nohash:,} "
                f"=> harness fold {delta:,} insns/frame "
                f"({100.0 * delta / base:.1f}% of the published number, "
                f"{delta / BYTES_PER_FRAME:.2f} insns per hashed byte); "
                f"render-only {nohash / PIXELS:.1f} insn/px")
    finally:
        SRC.write_text(original)
        log(f"restored {SRC} from memory (backup kept at {BACKUP})")
    (TMP / "harness-overhead.json").write_text(json.dumps(out, indent=2) + "\n")
    log(f"wrote {TMP / 'harness-overhead.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

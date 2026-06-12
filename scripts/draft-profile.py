#!/usr/bin/env python3
"""draft-profile.py — per-FUNCTION callgrind profile of a vyr Draft render.

STEP 1 of #16's "meet/beat LVGL on the M4" work: find exactly where the Draft
tier spends its instructions before optimizing. Runs the vyr-callgrind driver
(must be built already: cargo build --release -p vyr-callgrind in the standalone
vyr checkout) under callgrind on the M4 FIXTURE_IR scene at Quality::Draft, then
callgrind_annotate for the per-function Ir breakdown.

The driver renders the SAME 480x270 scene N times in one process (setup once,
outside the loop), so the per-function Ir totals are dominated by the render
path; the top consumers ARE the Draft hot paths.

All output -> tmp/draft-profile/ + tmp/draft-profile.log (timestamped).

Usage: python3 scripts/draft-profile.py [--n 20] [--quality draft]
"""
from __future__ import annotations
import argparse, subprocess, sys, time
from pathlib import Path

VYR = Path("/home/dan/git/vyr")
DRIVER = VYR / "target" / "release" / "vyr-callgrind"
FIXTURE = Path("/home/dan/git/awto-vyvanse/scripts/callgrind-render-cost/fixture-m4.json")
FONTS = VYR / "fonts"
ASSETS = VYR / "vyr-core" / "tests" / "assets"
WORK = VYR / "tmp" / "draft-profile"
LOG = VYR / "tmp" / "draft-profile.log"

_lines: list[str] = []


def log(msg: str) -> None:
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1e6):06d} UTC"
    line = f"{stamp}  INFO  [draft-profile] {msg}"
    print(line, flush=True)
    _lines.append(line)


def flush() -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write("\n".join(_lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--quality", default="draft", choices=["draft", "exact"])
    opts = ap.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    if not DRIVER.is_file():
        log(f"FATAL: driver missing: {DRIVER} (build vyr-callgrind first)")
        flush()
        return 2
    out = WORK / f"cg.{opts.quality}.{opts.n}.out"
    out.unlink(missing_ok=True)
    cmd = [
        "valgrind", "--tool=callgrind", "--cache-sim=no", "--branch-sim=no",
        f"--callgrind-out-file={out}", "--",
        str(DRIVER), str(FIXTURE), str(FONTS), str(ASSETS), opts.quality, str(opts.n),
    ]
    log(f"callgrind {opts.quality} N={opts.n}: {' '.join(cmd[-6:])}")
    runlog = WORK / f"run.{opts.quality}.{opts.n}.log"
    with runlog.open("w") as lf:
        r = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT)
    if r.returncode != 0 or not out.is_file():
        log(f"FATAL: callgrind rc={r.returncode}; see {runlog}")
        flush()
        return 1
    # Per-function annotate (threshold 99% so the long tail still shows).
    ann = WORK / f"annotate.{opts.quality}.{opts.n}.txt"
    a = subprocess.run(
        ["callgrind_annotate", "--threshold=99.5", "--inclusive=no", str(out)],
        capture_output=True, text=True,
    )
    ann.write_text(a.stdout)
    log(f"annotate -> {ann}")
    # Echo the function table head into the log.
    started = False
    shown = 0
    for line in a.stdout.splitlines():
        if "Ir" in line and "file:function" in line:
            started = True
        if started:
            log(line)
            shown += 1
            if shown > 60:
                break
    flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

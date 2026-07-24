#!/usr/bin/env python3
"""orientcard-gate.py — everything that can be checked WITHOUT the board, in
one run, for the corner-and-orientation card.

The card is a bring-up fixture whose whole value is that the picture on the
glass is provably the picture the renderer meant to draw. That claim has four
machine-checkable halves and this script runs all of them plus the repo gates:

  1. every feature combination the card ships in still COMPILES for the MCU
     target — including the three board legs, which are what actually get
     flashed;
  2. `--features board` is byte-identical to HEAD (the card must be additive:
     scripts/board-elf-identical.py);
  3. the repo gates: fmt-check, check-mcu, clippy, test — goldens untouched;
  4. (delegated to scripts/testcard-host.py --card orient) the reference PNG,
     its re-folded hash, and the cross-ISA hash under emulation.

Usage:  python3 scripts/orientcard-gate.py [--skip-elf-identical]
Output: tmp/orientcard-gate.json, log tmp/orientcard-gate.log
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
LOG = TMP / "orientcard-gate.log"
OUT = TMP / "orientcard-gate.json"

# Every combination the card is built in. The board legs are the ones that get
# flashed; the run-qemu ones are the host/emulated hash vehicles. All are
# CHECKED for the MCU target where they target it, because a feature that only
# compiles on x86-64 is not a feature this project has.
COMBOS = [
    ("mcu", "board,lcd,orientcard"),
    ("mcu", "board,ltdc,orientcard"),
    ("mcu", "board,ltdc,present,orientcard"),
    ("mcu", "run-qemu,orientcard"),
    ("host", "run-qemu,orientcard"),
    # The colour card must keep working unchanged — the orientation card shares
    # its module, and a shared module is exactly where a regression hides.
    ("mcu", "board,ltdc,present,testcard"),
    ("host", "run-qemu,testcard"),
]

TARGET = "thumbv7em-none-eabihf"
_lines: list[str] = []


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    _lines.append(line)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    log("$ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                          check=False, **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-elf-identical", action="store_true")
    args = ap.parse_args()
    TMP.mkdir(exist_ok=True)
    out: dict = {"when": datetime.datetime.now().astimezone().isoformat(
        timespec="seconds"), "combos": {}, "gates": {}}
    rc = 0

    # --- 1. every feature combination compiles -------------------------------
    for where, feats in COMBOS:
        cmd = ["cargo", "check", "-p", "vyr-size", "--no-default-features",
               "--features", feats]
        if where == "mcu":
            cmd += ["--target", TARGET, "--profile", "release-mcu"]
        p = run(cmd)
        ok = p.returncode == 0
        out["combos"][f"{where}:{feats}"] = ok
        log(f"  {'ok' if ok else '*** FAILED ***'}  {where} {feats}")
        if not ok:
            log(p.stderr[-4000:])
            rc = rc or 2

    # --- 2. --features board is byte-identical to HEAD -----------------------
    if not args.skip_elf_identical:
        p = run([sys.executable, "scripts/board-elf-identical.py"])
        out["gates"]["board_elf_identical"] = (p.returncode == 0)
        log(f"  board-elf-identical rc={p.returncode}")
        for line in (p.stdout + p.stderr).splitlines()[-12:]:
            log("    " + line)
        if p.returncode != 0:
            rc = rc or 3

    # --- 3. the repo gates ---------------------------------------------------
    for gate in ("fmt-check", "check-mcu", "clippy", "test"):
        p = run([sys.executable, "dev.py", gate])
        out["gates"][gate] = (p.returncode == 0)
        log(f"  dev.py {gate} rc={p.returncode}")
        if p.returncode != 0:
            log((p.stdout + p.stderr)[-6000:])
            rc = rc or 4

    OUT.write_text(json.dumps(out, indent=2) + "\n")
    log(f"wrote {OUT}; overall rc={rc}")
    return rc


if __name__ == "__main__":
    code = main()
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a") as f:
        f.write("\n".join(_lines) + "\n")
    sys.exit(code)

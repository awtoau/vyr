#!/usr/bin/env python3
"""gate-combos.py — clippy every board/display feature combination for
thumbv7em (release-mcu) and verify the host testcard body hashes.

One script, one log (tmp/gate-combos.log), one summary — so the whole
compile-matrix gate is a single tool call rather than twenty.
"""
import datetime
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
TMP.mkdir(exist_ok=True)
LOG = TMP / "gate-combos.log"
TARGET = "thumbv7em-none-eabihf"

# Every combination a board/display build can be flashed in. DMA is the default
# (no feature); spipio selects the busy-wait baseline.
MCU_COMBOS = [
    "board",                              # measurement build (no display)
    "board,lcd",                          # SPI scene, DMA flush (default)
    "board,lcd,spipio",                   # SPI scene, busy-wait flush
    "board,lcd,anim",                     # animated SPI, DMA flush
    "board,lcd,anim,spipio",              # animated SPI, busy-wait flush
    "board,lcd,testcard",                 # colour card over SPI
    "board,lcd,orientcard",               # orient card over SPI
    "board,ltdc",                         # LTDC/SDRAM scan
    "board,ltdc,present",                 # direct RGB888 vs banded
    "board,ltdc,testcard",                # card blitted
    "board,ltdc,present,testcard",        # DIRECT colour card (the experiment)
    "board,ltdc,present,orientcard",      # DIRECT orient card
    "run-qemu",                           # emulated-M4 workload (perf: no fold)
    "verify",                             # #45 verify build (fold + hash proof)
    "verify,fast",                        # verify composes with a tier
    "verify,rig",                         # verify + animated chain hash
    "run-qemu,testcard",                  # cross-ISA colour card hash
    "run-qemu,orientcard",                # cross-ISA orient card hash
]

# Committed card body hashes (must be invariant under MADCTL/RB_SWAP/rate/DMA).
WANT = {
    "testcard": "0x65b88925c9a2ba19",
    "orientcard": "0x50bf6c0f146d4d4b",
    "frame": "0x24dcaff531c6eb01",  # measurement frame (run-qemu workload)
}


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


logf = open(LOG, "a")


def log(m):
    line = f"[{now()}] {m}"
    print(line)
    logf.write(line + "\n")
    logf.flush()


def clippy(feats):
    # No `-D warnings`: the project does not deny warnings globally (there is a
    # pre-existing clippy lint in vyr-core, which this pass must not modify). The
    # bar is (a) clippy returns rc=0 and (b) it emits NO warning originating in
    # vyr-size — i.e. this integration introduced none in the display code.
    cmd = [
        "cargo", "clippy", "-p", "vyr-size", "--target", TARGET,
        "--profile", "release-mcu", "--no-default-features",
        "--features", feats,
    ]
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    size_warns = [ln for ln in r.stderr.splitlines()
                  if "vyr-size/src" in ln and "warning" not in ln.lower()
                  and "-->" in ln]
    ok = r.returncode == 0 and not size_warns
    if not ok:
        logf.write(f"\n=== FAIL clippy {feats} rc={r.returncode} "
                   f"size_warns={size_warns} ===\n{r.stderr[-6000:]}\n")
    return ok


def host_run(feats):
    """Build+run the host leg, return its stdout (has the card/frame hashes)."""
    b = subprocess.run(
        ["cargo", "build", "--release", "-p", "vyr-size",
         "--no-default-features", "--features", feats],
        cwd=REPO, capture_output=True, text=True,
        env={**__import__("os").environ, "CARGO_INCREMENTAL": "0"},
    )
    if b.returncode:
        logf.write(f"\n=== FAIL host build {feats} ===\n{b.stderr[-4000:]}\n")
        return None
    r = subprocess.run([str(REPO / "target/release/vyr-size")],
                       cwd=REPO, capture_output=True, text=True)
    logf.write(f"\n=== host {feats} stdout ===\n{r.stdout}\n{r.stderr[-2000:]}\n")
    return r.stdout


def main():
    log("=" * 78)
    log("gate-combos: clippy matrix (thumbv7em release-mcu) + host card hashes")

    results = {}
    for feats in MCU_COMBOS:
        ok = clippy(feats)
        results[feats] = ok
        log(f"clippy {'PASS' if ok else 'FAIL'}  {feats}")

    # Host card-body hashes: same committed IR must fold to the same 64 bits.
    hashes = {}
    for feats, key in (("run-qemu,testcard", "testcard"),
                       ("run-qemu,orientcard", "orientcard")):
        out = host_run(feats)
        if out is None:
            hashes[key] = "BUILD-FAIL"
            continue
        m = re.search(r"testcard [^\n]*fnv1a=(0x[0-9a-f]{16})", out)
        if not m:
            m = re.search(r"card[^\n]*(0x[0-9a-f]{16})", out)
        hashes[key] = m.group(1) if m else "NO-HASH"

    # Measurement frame hash from the VERIFY host leg (#45: the perf build
    # compiles no fold and emits no hash — the hash is the verify build's job).
    out = host_run("verify")
    m = re.search(r"frame fnv1a=(0x[0-9a-f]{16})", out or "")
    hashes["frame"] = m.group(1) if m else "NO-HASH"

    log("-" * 78)
    allok = all(results.values())
    for feats, ok in results.items():
        if not ok:
            log(f"  COMBO FAIL: {feats}")
    log(f"clippy matrix: {sum(results.values())}/{len(results)} pass")
    for key in ("testcard", "orientcard", "frame"):
        got = hashes.get(key, "?")
        want = WANT[key]
        match = got == want
        allok = allok and match
        log(f"  hash {key}: got={got} want={want} {'OK' if match else 'MISMATCH'}")
    log(f"GATE-COMBOS: {'ALL PASS' if allok else 'FAILURES ABOVE'}")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())

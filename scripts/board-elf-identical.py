#!/usr/bin/env python3
"""board-elf-identical.py — prove that adding a display feature left the
MEASUREMENT build byte-for-byte unchanged (#28, #30).

The `lcd` and `ltdc` features are documented as strictly ADDITIVE to `board`:
`--no-default-features --features board` is supposed to compile none of their
code and remain exactly the configuration the committed cycle/hash numbers
were taken on. Running the board and getting the same frame hash and the same
cycle count is strong evidence, but it is not the claim -- the claim is that
the ELF is the same bytes.

So: build `--features board` from the working tree, swap in HEAD's version of
every file the board build actually reads and this change touched, rebuild,
and compare sha256. The originals are restored in a `finally`, and the
working-tree build is redone afterwards so the tree is left as it was found.

Usage: python3 scripts/board-elf-identical.py [--against <git-ref>]
Log: tmp/board-elf-identical.log
"""
import argparse
import datetime
import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
ELF = REPO / "target/thumbv7em-none-eabihf/release-mcu/vyr-size"

# The only files the `board` build reads that a display feature would touch.
# lcd.rs/ltdc.rs are NOT here on purpose: `board` does not compile them, which
# is precisely what this script exists to verify.
FILES = ["vyr-size/src/main.rs", "vyr-size/Cargo.toml", "vyr-size/src/m4.rs"]

BUILD = ["cargo", "build", "-p", "vyr-size", "--target",
         "thumbv7em-none-eabihf", "--profile", "release-mcu",
         "--no-default-features", "--features", "board"]


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--against", default="HEAD",
                    help="git ref to compare the working tree against")
    args = ap.parse_args()

    TMP.mkdir(exist_ok=True)
    logf = open(TMP / "board-elf-identical.log", "a")

    def log(m):
        line = f"[{now()}] {m}"
        print(line)
        logf.write(line + "\n")
        logf.flush()

    def build(tag):
        r = subprocess.run(BUILD, cwd=REPO, capture_output=True, text=True)
        if r.returncode:
            log(f"{tag}: BUILD FAILED")
            logf.write(r.stderr[-4000:] + "\n")
            return None
        h = hashlib.sha256(ELF.read_bytes()).hexdigest()
        log(f"{tag}: sha256 {h}")
        return h

    log("=" * 70)
    log(f"board-elf-identical: working tree vs {args.against}")
    backups = {}
    rc = 1
    try:
        mine = build("working tree")
        for f in FILES:
            p = REPO / f
            backups[f] = p.read_bytes()
            r = subprocess.run(["git", "show", f"{args.against}:{f}"],
                               cwd=REPO, capture_output=True)
            if r.returncode:
                log(f"cannot read {args.against}:{f} — aborting")
                return 2
            p.write_bytes(r.stdout)
        base = build(f"{args.against}")
        same = mine is not None and mine == base
        log(f"BYTE-IDENTICAL: {same}")
        rc = 0 if same else 1
    finally:
        for f, b in backups.items():
            (REPO / f).write_bytes(b)
        log(f"restored {len(backups)} file(s) from memory")
        build("working tree (restored)")
        logf.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())

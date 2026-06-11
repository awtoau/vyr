#!/usr/bin/env python3
"""Rank flash contributors in a linked vyr-size ELF (F9 static analysis, #9).

Runs `arm-none-eabi-nm --print-size --size-sort --demangle` on the ELF,
aggregates text/rodata symbol sizes per top-level crate/namespace, and prints
(a) the per-crate totals and (b) the N largest individual symbols. The ELF
must be UNSTRIPPED — release-mcu strips by default, so build the analysis
ELF with the documented one-off override:

  cargo build --config profile.release-mcu.strip=false -p vyr-size \
      --target thumbv7em-none-eabihf --profile release-mcu [feature flags]

Usage: scripts/size-rank.py <elf> [top-N, default 25]
Output mirrored to ./tmp/size-rank.log (awto logging convention).
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOG = REPO / "tmp" / "size-rank.log"


def log(msg: str) -> None:
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1e6):06d} UTC"
    line = f"{stamp}  INFO  [size-rank] {msg}"
    print(line)
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# Demangled Rust paths start with the crate; v0 mangling keeps a hash suffix
# we strip for readability. Anonymous .Lanon rodata blobs group as one bucket
# (string/table literals lld kept — mostly panic locations + error format
# pieces; nm cannot attribute them to a crate).
CRATE_RE = re.compile(r"^<?([A-Za-z_][A-Za-z0-9_]*)")


def crate_of(name: str) -> str:
    if name.startswith(".Lanon"):
        return "(anon rodata: strings/tables)"
    if name.startswith("OUTLINED_FUNCTION"):
        return "(llvm outlined)"
    m = CRATE_RE.match(name.removeprefix("_RNv").removeprefix("_ZN"))
    if not m:
        return "(other)"
    head = m.group(1)
    # `<vyr_core::painter::TinySkiaCanvas as ...>` style: the crate is the
    # first path segment inside the angle bracket.
    return head


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    elf = sys.argv[1]
    top_n = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    out = subprocess.run(
        ["arm-none-eabi-nm", "--print-size", "--size-sort", "--demangle", elf],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        log(f"ERROR: nm rc={out.returncode}: {out.stderr.strip()}")
        return out.returncode
    per_crate: dict[str, int] = defaultdict(int)
    syms: list[tuple[int, str, str]] = []
    total = 0
    for line in out.stdout.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) < 4:
            continue
        _addr, size_hex, kind, name = parts
        # t/T = .text, r/R = .rodata — the flash-resident buckets; d/b are
        # RAM and tiny here (the size table covers them).
        if kind.lower() not in ("t", "r"):
            continue
        size = int(size_hex, 16)
        total += size
        syms.append((size, kind.lower(), name))
        per_crate[crate_of(name)] += size
    log(f"ELF: {elf}")
    log(f"attributed flash symbols: {total} B across {len(syms)} symbols")
    log("— per-crate totals (text+rodata, descending) —")
    for crate, size in sorted(per_crate.items(), key=lambda kv: -kv[1]):
        log(f"{size:>8} B  {size / total:>6.1%}  {crate}")
    log(f"— top {top_n} symbols —")
    for size, kind, name in sorted(syms, key=lambda s: -s[0])[:top_n]:
        log(f"{size:>8} B  [{kind}]  {name[:140]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

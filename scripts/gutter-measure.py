#!/usr/bin/env python3
"""gutter-measure.py — M4 heap peak + exact insns/frame for every tier (#38).

One run = the whole before/after picture for a GUTTER change:

  1. `./dev.py qemu-m4` / `--fast` / `--draft`  → allocator heap peak,
     cross-ISA frame hash (M4 vs x86-64), frame hash vs the committed baseline.
  2. `scripts/tier-insns.py`                    → exact plugin insns/frame
     (release-mcu build, unchanged).

Output: tmp/gutter-measure-<tag>.json   Log: tmp/gutter-measure-<tag>.log
Usage:  python3 scripts/gutter-measure.py --tag before [--no-insns]
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"

_lines: list[str] = []
_log_path: Path | None = None


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    _lines.append(line)
    if _log_path is not None:
        _log_path.write_text("\n".join(_lines) + "\n", encoding="utf-8")


def qemu_tier(flag: list[str], tier: str) -> dict:
    """One ./dev.py qemu-m4 run; returns the parsed facts (never gates)."""
    r = subprocess.run(
        [sys.executable, "dev.py", "qemu-m4", *flag],
        cwd=REPO, capture_output=True, text=True,
    )
    out = r.stdout + r.stderr
    (TMP / f"gutter-qemu-{tier.lower()}.log").write_text(out, encoding="utf-8")

    def g(pat: str):
        m = re.search(pat, out)
        return m.group(1) if m else None

    facts = {
        "rc": r.returncode,
        "heap_peak_b": g(r"heap peak\s+M4 (\d+) B"),
        "heap_live_end_b": g(r"heap peak\s+M4 \d+ B \(live-end (\d+) B\)"),
        "host_heap_peak_b": g(r"vs x86-64 (\d+) B"),
        "frame_hash_m4": g(r"frame hash\s+M4 (0x[0-9a-f]+)"),
        "cross_isa": "IDENTICAL" if "IDENTICAL" in out else ("MISMATCH" if "MISMATCH" in out else "?"),
        "insns_per_frame_indicative": g(r"~([\d,]+) insns/frame vs baseline"),
        "gate_fail": "GATE FAIL" in out,
        "gate_fail_lines": [ln.strip() for ln in out.splitlines() if "GATE FAIL" in ln],
    }
    log(f"{tier}: heap peak {facts['heap_peak_b']} B, hash {facts['frame_hash_m4']}, "
        f"cross-ISA {facts['cross_isa']}, rc={r.returncode}"
        + (" GATE FAIL: " + "; ".join(facts["gate_fail_lines"]) if facts["gate_fail"] else ""))
    return facts


def main() -> int:
    global _log_path
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--no-insns", action="store_true")
    ap.add_argument("--tiers", default="exact,fast,draft")
    ap.add_argument("--gutter", type=int, default=None,
                    help="temporarily patch GUTTER to this value for the run "
                         "(painter.rs is restored afterwards)")
    opts = ap.parse_args()
    TMP.mkdir(parents=True, exist_ok=True)
    _log_path = TMP / f"gutter-measure-{opts.tag}.log"

    painter = REPO / "vyr-core/src/painter.rs"
    saved = painter.read_text(encoding="utf-8")
    if opts.gutter is not None:
        m = re.search(r"(?m)^(const GUTTER: u32 = )(\d+)(;)", saved)
        painter.write_text(
            saved[: m.start()] + m.group(1) + str(opts.gutter) + m.group(3) + saved[m.end():],
            encoding="utf-8")
    try:
        return measure(opts)
    finally:
        if opts.gutter is not None:
            painter.write_text(saved, encoding="utf-8")
            log("restored vyr-core/src/painter.rs")


def measure(opts) -> int:

    gutter = re.search(r"^const GUTTER: u32 = (\d+);",
                       (REPO / "vyr-core/src/painter.rs").read_text(), re.M)
    fast_gutter = re.search(r"^const FAST_GUTTER: u32 = (\d+);",
                            (REPO / "vyr-core/src/painter.rs").read_text(), re.M)
    result: dict = {
        "tag": opts.tag,
        "when": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "gutter": int(gutter.group(1)) if gutter else None,
        "fast_gutter": int(fast_gutter.group(1)) if fast_gutter else None,
        "qemu": {},
        "insns": {},
    }
    log(f"=== {opts.tag}: GUTTER={result['gutter']} FAST_GUTTER={result['fast_gutter']} ===")

    tiers = [t.strip() for t in opts.tiers.split(",") if t.strip()]
    flags = {"exact": [], "fast": ["--fast"], "draft": ["--draft"]}
    for tier in tiers:
        log(f"=== qemu-m4 {tier} ===")
        result["qemu"][tier] = qemu_tier(flags[tier], tier)
        (TMP / f"gutter-measure-{opts.tag}.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8")

    if not opts.no_insns:
        log("=== tier-insns.py (exact plugin counts, release-mcu) ===")
        r = subprocess.run(
            [sys.executable, "scripts/tier-insns.py", "--tiers", ",".join(tiers)],
            cwd=REPO, capture_output=True, text=True,
        )
        (TMP / f"gutter-insns-{opts.tag}.log").write_text(r.stdout + r.stderr, encoding="utf-8")
        if r.returncode == 0:
            data = json.loads((TMP / "tier-insns.json").read_text())
            for tier, d in data.items():
                result["insns"][tier] = d.get("insns_per_frame")
                log(f"{tier}: {d.get('insns_per_frame'):,} insns/frame")
        else:
            log(f"tier-insns.py FAILED rc={r.returncode} (see tmp/gutter-insns-{opts.tag}.log)")

    (TMP / f"gutter-measure-{opts.tag}.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8")
    log(f"wrote tmp/gutter-measure-{opts.tag}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

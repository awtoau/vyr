#!/usr/bin/env python3
"""gutter-sweep.py — MEASURE the overscan gutter each quality tier needs (#38).

The gutter is a compile-time constant, so the only way to measure it is to
patch the constant, run the band-equivalence sweep
(`vyr-core/tests/fast_golden.rs::band_equivalence_sweep`: every band height
1..=H on DEMO_IR + demo_scene + CLIP_IR, byte-exact vs the full frame), and
read off the smallest value that passes.

Two verdicts per value, BOTH required:
  * the tier's stress sweep passes  — the gutter is sufficient across seams;
  * every committed golden hash is unchanged — a sufficient gutter is
    DISCARDED, so it cannot legitimately move a pixel. A moved hash means the
    value is too small, NOT a re-bless candidate (#38).

The source file is restored on every exit path, including Ctrl-C.

Output: tmp/gutter-sweep.json    Log: tmp/gutter-sweep.log
Usage:  python3 scripts/gutter-sweep.py [--tiers exact,fast,draft]
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
PAINTER = REPO / "vyr-core/src/painter.rs"
LOG = TMP / "gutter-sweep.log"

# tier -> (regex over painter.rs with the value as group 1, stress test name,
#          the overscan values to try, in order)
TIERS = {
    "exact": (r"(?m)^(const GUTTER: u32 = )(\d+)(;)", "exact_band_equivalence_stress",
              [0, 1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 64]),
    "fast": (r"(?m)^(const FAST_GUTTER: u32 = )(\d+)(;)", "fast_band_equivalence_stress",
             [0, 1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 64]),
    # Draft's overscan is a literal in the match arm, not a named constant.
    "draft": (r"(Quality::Draft => )(\d+)(,)", "draft_band_equivalence_stress", [0, 1, 2, 3, 4, 6, 8, 16]),
}

_lines: list[str] = []


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    _lines.append(line)
    TMP.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(_lines) + "\n", encoding="utf-8")


def patch(tier: str, value: int, original: str) -> None:
    """Rewrite the tier's overscan literal in painter.rs (prefix + value + suffix,
    reconstructed explicitly so no other digit in the line can be hit)."""
    m = re.search(TIERS[tier][0], original)
    assert m, f"no overscan literal found for tier {tier}"
    new = original[: m.start()] + m.group(1) + str(value) + m.group(3) + original[m.end():]
    PAINTER.write_text(new, encoding="utf-8")


def run_tests(test_filter: str | None) -> tuple[bool, str]:
    args = ["cargo", "test", "-p", "vyr-core", "--no-fail-fast"]
    if test_filter:
        args += ["--test", "fast_golden", test_filter, "--", "--exact"]
    r = subprocess.run(args, cwd=REPO, capture_output=True, text=True)
    return r.returncode == 0, r.stdout + r.stderr


def failures(out: str) -> list[str]:
    """Names of failing tests, plus the first assertion line of each panic."""
    names = re.findall(r"^\s{4}(\w+::\w+|\w+)$", out, re.M)
    seen, ordered = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


def detail(out: str) -> str:
    m = re.search(r"panicked at [^\n]*\n(.+)", out)
    return m.group(1).strip() if m else ""


def spectrum(out: str) -> dict:
    """Failing (scene, band_h) splits from the sweep panic, per scene."""
    per: dict[str, list[int]] = {}
    for scene, band_h in re.findall(r"^\s+(\w+) band_h=(\d+):", out, re.M):
        per.setdefault(scene, []).append(int(band_h))
    return {k: sorted(v) for k, v in per.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="exact,fast,draft")
    opts = ap.parse_args()
    original = PAINTER.read_text(encoding="utf-8")
    results: dict[str, list[dict]] = {}
    try:
        for tier in [t.strip() for t in opts.tiers.split(",") if t.strip()]:
            pat, test_name, values = TIERS[tier]
            results[tier] = []
            log(f"===== tier {tier}: sweeping overscan {values} =====")
            for v in values:
                patch(tier, v, original)
                ok_stress, out_stress = run_tests(test_name)
                row = {
                    "overscan": v,
                    "stress_pass": ok_stress,
                    "stress_detail": "" if ok_stress else detail(out_stress),
                    "failing_splits": {} if ok_stress else spectrum(out_stress),
                    "goldens_pass": None,
                    "golden_failures": [],
                }
                if ok_stress:
                    ok_all, out_all = run_tests(None)
                    row["goldens_pass"] = ok_all
                    if not ok_all:
                        row["golden_failures"] = failures(out_all)
                        row["golden_detail"] = detail(out_all)
                    (TMP / f"gutter-sweep-{tier}-{v}-full.log").write_text(
                        out_all, encoding="utf-8")
                else:
                    (TMP / f"gutter-sweep-{tier}-{v}-stress.log").write_text(
                        out_stress, encoding="utf-8")
                results[tier].append(row)
                counts = ", ".join(f"{k}×{len(vs)}" for k, vs in row["failing_splits"].items())
                log(f"{tier} overscan={v}: stress {'PASS' if ok_stress else 'FAIL'}"
                    + ("" if ok_stress else f" — {counts or row['stress_detail'][:160]}")
                    + ("" if row["goldens_pass"] is None else
                       f" | goldens {'unchanged' if row['goldens_pass'] else 'MOVED: ' + ','.join(row['golden_failures'])}"))
                (TMP / "gutter-sweep.json").write_text(
                    json.dumps(results, indent=2) + "\n", encoding="utf-8")
    finally:
        PAINTER.write_text(original, encoding="utf-8")
        log(f"restored {PAINTER.relative_to(REPO)}")

    for tier, rows in results.items():
        good = [r["overscan"] for r in rows if r["stress_pass"] and r["goldens_pass"]]
        log(f"VERDICT {tier}: minimum sufficient overscan = "
            f"{min(good) if good else 'NONE of the swept values'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

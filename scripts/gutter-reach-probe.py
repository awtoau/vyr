#!/usr/bin/env python3
"""gutter-reach-probe.py — is the overscan a BOUND or a coincidence? (#38)

The #38 sweep found `Quality::Exact` band-exact on the committed fixtures only
at overscan >= 16, with every failure on CHART_IR's diagonal polyline. That
raises the question the shipped constant depends on: does the reach of a
band-seam divergence scale with the HEIGHT of the AA polygon being cut (in
which case no fixed gutter is a correctness mechanism, only a fixture-sized
one), or does it saturate?

The probe renders ONE diagonal `line` op of a controlled vertical extent, at
every band height 1..=120, and asks the smallest overscan that makes the
stitched frame byte-identical to the full-frame render. Run for several line
heights, the answer is a curve, not a constant.

The probe test file is created under vyr-core/tests/ for the run and DELETED
afterwards, on every exit path; painter.rs is restored the same way.

Output: tmp/gutter-reach-probe.json   Log: tmp/gutter-reach-probe.log
Usage:  python3 scripts/gutter-reach-probe.py [--gutters 4,8,16,32,64,120]
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
PROBE = REPO / "vyr-core/tests/zz_gutter_reach_probe.rs"
GUTTER_RE = r"(?m)^(const GUTTER: u32 = )(\d+)(;)"

# (label, y0, y1) — the diagonal's vertical extent inside a 120x120 frame.
LINES = [("span19", 28, 47), ("span40", 20, 60), ("span80", 20, 100), ("span109", 5, 114)]

PROBE_SRC = """//! TEMPORARY #38 probe — created and deleted by scripts/gutter-reach-probe.py.
//! Do not commit.

use vyr_core::{Canvas, Quality, Rect, Rgb, TinySkiaCanvas};

const W: u32 = 120;
const H: u32 = 120;

fn banded(band_h: u32, y0: i32, y1: i32) -> Vec<u8> {
    let stride = (W * 3) as usize;
    let mut out = vec![0u8; stride * H as usize];
    let mut y = 0;
    while y < H {
        let h = band_h.min(H - y);
        let area = Rect { x: 0, y: y as i32, w: W, h };
        let mut c = TinySkiaCanvas::new_with_quality(area, Quality::Exact).expect("pixmap");
        c.fill_rrect(Rect { x: 0, y: 0, w: W, h: H }, 0, Rgb { r: 0xF0, g: 0xEE, b: 0xE8 }, 0xFF);
        c.line(10, y0, 110, y1, 2, Rgb { r: 0x1E, g: 0x5A, b: 0xA8 }, 0xFF);
        let band = &mut out[y as usize * stride..(y + h) as usize * stride];
        c.finish_into_rgb888(band, stride);
        y += h;
    }
    out
}

fn probe(label: &str, y0: i32, y1: i32) {
    let full = banded(H, y0, y1);
    let mut fails = 0usize;
    for band_h in 1..=H {
        if banded(band_h, y0, y1) != full {
            fails += 1;
        }
    }
    println!("PROBE {label} extent={} fails={fails}", (y1 - y0).abs());
}

#[test]
fn gutter_reach_probe() {
__CASES__
}
"""


_lines: list[str] = []


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    _lines.append(line)
    TMP.mkdir(parents=True, exist_ok=True)
    (TMP / "gutter-reach-probe.log").write_text("\n".join(_lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gutters", default="4,8,16,32,64,120")
    opts = ap.parse_args()
    gutters = [int(g) for g in opts.gutters.split(",")]
    original = PAINTER.read_text(encoding="utf-8")
    cases = "\n".join(f'    probe("{n}", {a}, {b});' for n, a, b in LINES)
    PROBE.write_text(PROBE_SRC.replace("__CASES__", cases), encoding="utf-8")
    results: dict[int, dict[str, int]] = {}
    try:
        for g in gutters:
            m = re.search(GUTTER_RE, original)
            PAINTER.write_text(
                original[: m.start()] + m.group(1) + str(g) + m.group(3) + original[m.end():],
                encoding="utf-8",
            )
            r = subprocess.run(
                ["cargo", "test", "-p", "vyr-core", "--test", "zz_gutter_reach_probe",
                 "--", "--nocapture"],
                cwd=REPO, capture_output=True, text=True,
            )
            out = r.stdout + r.stderr
            (TMP / f"gutter-reach-probe-{g}.log").write_text(out, encoding="utf-8")
            row = {label: int(n) for label, n in
                   re.findall(r"PROBE (\w+) extent=\d+ fails=(\d+)", out)}
            results[g] = row
            log(f"GUTTER={g}: " + (", ".join(f"{k} {v} failing splits" for k, v in row.items())
                                   or f"probe did not run (rc={r.returncode})"))
            (TMP / "gutter-reach-probe.json").write_text(
                json.dumps(results, indent=2) + "\n", encoding="utf-8")
    finally:
        PAINTER.write_text(original, encoding="utf-8")
        PROBE.unlink(missing_ok=True)
        log("restored painter.rs, deleted the probe test")
    return 0


if __name__ == "__main__":
    sys.exit(main())

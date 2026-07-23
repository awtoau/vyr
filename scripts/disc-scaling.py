#!/usr/bin/env python3
"""disc-scaling.py — does vyr's AA cost scale with AREA or with PERIMETER?

The falsifiable test behind the "LVGL anti-aliases only the EDGES" hypothesis.
A renderer that fills a disc's interior with solid spans and does coverage
arithmetic only on the boundary pays O(perimeter) for the AA part; one that
runs a general coverage pipeline over the whole shape pays O(area).

Method: render ONE disc of radius r on a FIXED canvas at each quality tier,
under callgrind (exact Ir instruction counts, deterministic), and subtract the
empty-canvas run so only the disc's marginal cost remains. Doubling r then
multiplies the marginal cost by ~2 (perimeter) or ~4 (area); the fitted
log-log slope says which, with no theorising.

Everything -> tmp/disc-scaling/ ; summary + table -> tmp/disc-scaling.log
Usage: python3 scripts/disc-scaling.py [--radii 8,16,32,64] [--canvas 320]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
WORK = TMP / "disc-scaling"
LOG = TMP / "disc-scaling.log"
CLI = REPO / "target" / "release" / "vyr-cli"

_lines: list[str] = []


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line, flush=True)
    _lines.append(line)


def flush() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(_lines) + "\n")


def scene(canvas: int, radius: int | None, shape: str = "disc") -> str:
    kids = []
    # An opaque backdrop so every run pays the identical backdrop cost and the
    # disc blends onto a defined dst (the same shape the real scenes have).
    kids.append(
        '{"name": "vy_frame", "attrs": {"x": "0", "y": "0", "width": "%d", '
        '"height": "%d", "background": "#101820"}}' % (canvas, canvas)
    )
    if radius:
        d = 2 * radius
        x = (canvas - d) // 2
        if shape == "disc":
            kids.append(
                '{"name": "vy_circle", "attrs": {"x": "%d", "y": "%d", "width": "%d", '
                '"height": "%d", "background": "#1E5AA8"}}' % (x, x, d, d)
            )
        elif shape == "gauge":
            kids.append(
                '{"name": "vy_gauge", "attrs": {"x": "%d", "y": "%d", "width": "%d", '
                '"height": "%d", "value": "65", "min": "0", "max": "100"}}' % (x, x, d, d)
            )
        elif shape == "rect":
            # A FLAT opaque rect of side 2r — the shape the real scene is
            # mostly made of. Cost here must scale with AREA in every tier;
            # what matters is the per-pixel constant each tier pays.
            kids.append(
                '{"name": "vy_frame", "attrs": {"x": "%d", "y": "%d", "width": "%d", '
                '"height": "%d", "background": "#1E5AA8"}}' % (x, x, d, d)
            )
    return (
        '{"w": %d, "h": %d, "root": {"name": "view", "children": [%s]}}'
        % (canvas, canvas, ",".join(kids))
    )


FN_RE = re.compile(r"^fn=\(\d+\)(?: (.*))?$")


def callgrind(scene_path: Path, quality: str, tag: str) -> dict:
    """One callgrind run. Returns totals + per-function Ir (name -> Ir)."""
    out = WORK / f"cg.{tag}.out"
    out.unlink(missing_ok=True)
    png = WORK / f"{tag}.png"
    cmd = [
        "valgrind", "--tool=callgrind", "--cache-sim=no", "--branch-sim=no",
        f"--callgrind-out-file={out}", "--",
        str(CLI), "render", str(scene_path), str(png),
    ]
    if quality != "exact":
        cmd.append(f"--{quality}")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)
    if r.returncode != 0 or not out.is_file():
        log(f"FATAL callgrind rc={r.returncode} tag={tag}\n{r.stderr[-2000:]}")
        raise SystemExit(1)
    # Parse the callgrind format: fn=(id) name  then cost lines (first column
    # is the line number, second is Ir).  Self cost per function.
    per_fn: dict[str, int] = {}
    names: dict[str, str] = {}
    cur = None
    total = 0
    file_total = None
    skip_next_cost = False  # the line after `calls=` is the CALLEE's inclusive
                            # cost, not this function's self cost — counting it
                            # double-counts the whole program.
    for line in out.read_text(errors="replace").splitlines():
        if line.startswith(("summary:", "totals:")):
            try:
                file_total = int(line.split(":", 1)[1].split()[0])
            except (ValueError, IndexError):
                pass
            continue
        if line.startswith("fn="):
            m = re.match(r"^fn=\((\d+)\)(?: (.*))?$", line)
            if m:
                fid, nm = m.group(1), m.group(2)
                if nm:
                    names[fid] = nm
                cur = names.get(fid, f"?{fid}")
            skip_next_cost = False
            continue
        if line.startswith("calls="):
            skip_next_cost = True
            continue
        if line.startswith(("cfn=", "cfl=", "cfi=", "fl=", "fi=", "fe=", "ob=", "cob=")):
            continue
        if line and (line[0].isdigit() or line[0] in "+-*"):
            if skip_next_cost:
                skip_next_cost = False
                continue
            parts = line.split()
            if len(parts) >= 2:
                try:
                    ir = int(parts[1])
                except ValueError:
                    continue
                total += ir
                if cur:
                    per_fn[cur] = per_fn.get(cur, 0) + ir
    if file_total is not None and abs(file_total - total) > max(1000, total // 200):
        log(f"  WARN {tag}: self-cost sum {total:,} != callgrind summary {file_total:,}")
    return {"total": file_total if file_total is not None else total, "per_fn": per_fn}


def fit_slope(xs: list[float], ys: list[float]) -> float:
    """log-log least-squares slope: 1 = perimeter, 2 = area."""
    lx = [math.log(x) for x in xs]
    ly = [math.log(y) for y in ys]
    n = len(lx)
    mx = sum(lx) / n
    my = sum(ly) / n
    num = sum((a - mx) * (b - my) for a, b in zip(lx, ly))
    den = sum((a - mx) ** 2 for a in lx)
    return num / den if den else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--radii", default="8,16,32,64")
    ap.add_argument("--canvas", type=int, default=320)
    ap.add_argument("--tiers", default="exact,fast,draft")
    ap.add_argument("--shapes", default="disc,gauge")
    a = ap.parse_args()
    radii = [int(x) for x in a.radii.split(",")]
    WORK.mkdir(parents=True, exist_ok=True)

    log("build vyr-cli --release")
    b = subprocess.run(["cargo", "build", "--release", "-p", "vyr-cli"],
                       cwd=REPO, capture_output=True, text=True)
    if b.returncode != 0:
        log(b.stdout[-3000:] + b.stderr[-3000:])
        return 1

    results: dict = {"canvas": a.canvas, "radii": radii, "runs": {}}
    # Baseline: backdrop only.
    base_path = WORK / "empty.json"
    base_path.write_text(scene(a.canvas, None))
    for tier in a.tiers.split(","):
        base = callgrind(base_path, tier, f"empty-{tier}")
        results["runs"][f"empty/{tier}"] = {"total": base["total"]}
        log(f"baseline {tier}: {base['total']:,} Ir (empty {a.canvas}x{a.canvas} canvas)")
        for shape in a.shapes.split(","):
            marg = []
            for r in radii:
                p = WORK / f"{shape}-{r}.json"
                p.write_text(scene(a.canvas, r, shape))
                res = callgrind(p, tier, f"{shape}{r}-{tier}")
                m = res["total"] - base["total"]
                marg.append(m)
                top = sorted(res["per_fn"].items(), key=lambda kv: -kv[1])[:12]
                results["runs"][f"{shape}{r}/{tier}"] = {
                    "total": res["total"], "marginal": m,
                    "top": [{"fn": k, "ir": v} for k, v in top],
                }
                area = math.pi * r * r
                log(f"  {shape} r={r:3d} {tier:5s}: total {res['total']:>12,}  "
                    f"marginal {m:>12,} Ir  = {m / area:8.1f} Ir/disc-px  "
                    f"= {m / (2 * math.pi * r):9.1f} Ir/perimeter-px")
            pos = [(r, m) for r, m in zip(radii, marg) if m > 0]
            if len(pos) >= 2:
                slope = fit_slope([r for r, _ in pos], [m for _, m in pos])
                ratios = [pos[i + 1][1] / pos[i][1] for i in range(len(pos) - 1)]
                verdict = ("AREA-scaled (whole-shape coverage)" if slope > 1.6
                           else "PERIMETER-scaled (edge-only)" if slope < 1.35
                           else "MIXED")
                log(f"  ==> {shape}/{tier}: log-log slope {slope:.2f} "
                    f"(1=perimeter, 2=area); doubling ratios "
                    f"{', '.join(f'{x:.2f}x' for x in ratios)} -> {verdict}")
                results["runs"][f"{shape}/{tier}/slope"] = slope

    (WORK / "disc-scaling.json").write_text(json.dumps(results, indent=2) + "\n")
    log(f"wrote {WORK / 'disc-scaling.json'}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        flush()

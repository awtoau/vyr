#!/usr/bin/env python3
"""metrics-history — the long-term metrics ledger + grapher.

The companion to perf-history.py (the F18 rig ledger), but for the WHOLE
valuable-number set, snapshotted per git commit (+ a dirty flag) so the
numbers that get "thrown around" become a tracked, graphed history instead:

  · size      flash KiB on the M4 (release-mcu), code-only / +font / +image
  · memory    M4 workload heap peak, Draft + Exact tiers
  · perf      host ns/px per primitive + scene (vyr-bench), the 480×270 +
              4K ladder rungs, and the INDICATIVE M4 insns/frame per tier
  · fidelity  Draft fast-path coverage %

Appends one flat record to the append-only ``docs/metrics/history.jsonl``,
then regenerates ``docs/metrics/index.html`` + hand-rolled SVG trend charts
(stdlib only — no matplotlib). Each chart's x-axis is history order; hover a
point for the commit + value.

Sources (collected fresh unless --regen-only):
  · ``./dev.py size-mcu``         parsed for the release-mcu text+data
  · ``./dev.py qemu-m4 [--draft]``parsed for insns/frame, heap, coverage
  · ``vyr-bench/baseline.json``   the committed host ns/px (blessed)
  · ``tmp/rig-ladder.json``       the latest ladder run (./dev.py ladder)

Usage: scripts/metrics-history.py [--regen-only]
Canonical entry: ``./dev.py track``. Output timestamped → tmp/metrics-history.log.

NOTE: the M4 insns/frame is INDICATIVE only — SYS_CLOCK is wall-influenced on
a plugin-less qemu (see dev.py qemu-m4); it is tracked for the trend, but the
deterministic perf signals are the host ns/px (vyr-bench/ladder) and the M4
heap peak. The chart labels say so.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
MET = REPO / "docs" / "metrics"
HISTORY = MET / "history.jsonl"


def log(msg: str) -> None:
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1e6):06d} UTC"
    line = f"{stamp}  INFO  [metrics-history] {msg}"
    print(line, file=sys.stderr)
    TMP.mkdir(exist_ok=True)
    with open(TMP / "metrics-history.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _git(*args: str) -> str:
    out = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=REPO, check=False
    )
    return out.stdout.strip()


def _git_commit() -> str:
    return _git("rev-parse", "--short", "HEAD") or "unknown"


def _git_dirty() -> bool:
    return bool(_git("status", "--porcelain"))


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _dev(*args: str) -> str:
    """Run ./dev.py <args>, return combined stdout+stderr (numbers are logged
    to stderr)."""
    p = subprocess.run(
        [sys.executable, str(REPO / "dev.py"), *args],
        capture_output=True, text=True, cwd=REPO, check=False,
    )
    return p.stdout + p.stderr


# --- collection --------------------------------------------------------------

def collect() -> dict:
    m: dict = {}

    # size: release-mcu text+data per config → flash KiB.
    size_out = _dev("size-mcu")
    for cfg, key in (("code-only", "flash_code_kib"),
                     ("font", "flash_font_kib"),
                     ("font,image", "flash_fontimg_kib")):
        mm = re.search(
            rf"sized {re.escape(cfg)} / release-mcu: text=(\d+) data=(\d+)", size_out
        )
        if mm:
            m[key] = round((int(mm.group(1)) + int(mm.group(2))) / 1024, 1)
    if "flash_code_kib" not in m:
        log("note: size-mcu produced no release-mcu lines — size metrics skipped")

    # M4 per tier: insns/frame (indicative), heap peak, Draft coverage.
    for tier, args, pfx in (("draft", ["--draft"], "m4_draft"), ("exact", [], "m4_exact")):
        out = _dev("qemu-m4", *args)
        mi = re.search(r"\(([\d,]+)/frame", out)
        mh = re.search(r"heap peak\s+M4 (\d+) B", out)
        if mi:
            m[f"{pfx}_insns"] = int(mi.group(1).replace(",", ""))
        if mh:
            m[f"{pfx}_heap_kib"] = round(int(mh.group(1)) / 1024, 1)
        if tier == "draft":
            mc = re.search(r"fast-path coverage ([\d.]+)%", out)
            if mc:
                m["draft_coverage_pct"] = float(mc.group(1))

    # host ns/px from the committed bench baseline (blessed medians).
    bench = REPO / "vyr-bench" / "baseline.json"
    if bench.exists():
        b = json.loads(bench.read_text())
        for k, key in (("prim/fill_rrect_64", "ns_fill_rrect"),
                       ("prim/disc_r24", "ns_disc"),
                       ("prim/gradient_64", "ns_gradient"),
                       ("prim/line_diag_w3", "ns_line"),
                       ("scene/ir_full_exact", "ns_scene_exact"),
                       ("scene/ir_full_draft", "ns_scene_draft")):
            if k in b:
                m[key] = round(b[k], 4)
    else:
        log("note: vyr-bench/baseline.json absent — host ns/px skipped")

    # the 480×270 reference ladder rung (fresh run, if ci/ladder left it).
    ladder = TMP / "rig-ladder.json"
    if ladder.exists():
        for r in json.loads(ladder.read_text()).get("rungs", []):
            if (r.get("w"), r.get("h")) == (480, 270):
                m["ladder480_full_ns_px"] = round(r["full_ns_px"], 4)
                m["ladder480_incr_ns_px"] = round(r["incr_ns_dirty_px"], 4)
            if (r.get("w"), r.get("h")) == (3840, 2160):
                m["ladder4k_full_ns_px"] = round(r["full_ns_px"], 4)
    else:
        log("note: tmp/rig-ladder.json absent — ladder metrics skipped "
            "(run ./dev.py ladder)")
    return m


def append_record() -> dict:
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "commit": _git_commit(),
        "dirty": _git_dirty(),
        "host": platform.node(),
        "arch": platform.machine(),
        "cpu": _cpu_model(),
        "metrics": collect(),
    }
    MET.mkdir(parents=True, exist_ok=True)
    with open(HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    log(f"appended metrics record (commit {rec['commit']}"
        f"{'+dirty' if rec['dirty'] else ''}, {len(rec['metrics'])} metrics) "
        f"→ {HISTORY.relative_to(REPO)}")
    return rec


def load_history() -> list[dict]:
    if not HISTORY.exists():
        return []
    return [json.loads(line) for line in HISTORY.read_text().splitlines() if line.strip()]


# --- hand-rolled SVG (same approach as perf-history.py) ----------------------

PALETTE = ["#5b8def", "#44b07b", "#e0a03c", "#d9534f", "#9a6fd0", "#3fb6c4"]


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def svg_chart(title: str, ylabel: str,
              series: list[tuple[str, list[tuple[int, float]]]],
              labels: list[str], out: Path, y_fmt: str = "{:.3g}") -> None:
    """x = history index; one polyline+markers per series. `labels` = the
    per-run commit strings for hover/x-ticks."""
    W, Hh = 760, 320
    ml, mr, mt, mb = 70, 168, 34, 40
    pw, ph = W - ml - mr, Hh - mt - mb
    ys = [v for _, pts in series for _, v in pts]
    if not ys:
        out.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="60">'
                       f'<text x="10" y="30" font-size="13">{_esc(title)}: no data</text></svg>\n')
        return
    n = len(labels)
    y_max = max(ys) * 1.15 or 1.0
    xs_max = max(1, n - 1)

    def px(i): return ml + (pw * (i / xs_max) if n > 1 else pw / 2)
    def py(v): return mt + ph * (1 - v / y_max)

    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{Hh}" '
         f'font-family="system-ui,sans-serif" font-size="11">',
         f'<rect width="{W}" height="{Hh}" fill="#ffffff"/>',
         f'<text x="{ml}" y="20" font-size="13" font-weight="600" fill="#222">{_esc(title)}</text>']
    for k in range(6):
        v = y_max * k / 5
        y = py(v)
        p.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{ml + pw}" y2="{y:.1f}" stroke="#dddddd"/>')
        p.append(f'<text x="{ml - 6}" y="{y + 4:.1f}" text-anchor="end" fill="#555">{y_fmt.format(v)}</text>')
    step = max(1, n // 8)
    for i in range(0, n, step):
        p.append(f'<text x="{px(i):.1f}" y="{mt + ph + 16}" text-anchor="middle" fill="#555" '
                 f'transform="rotate(35 {px(i):.1f} {mt + ph + 16})">{_esc(labels[i])}</text>')
    p.append(f'<text x="14" y="{mt + ph / 2:.1f}" text-anchor="middle" fill="#555" '
             f'transform="rotate(-90 14 {mt + ph / 2:.1f})">{_esc(ylabel)}</text>')
    for si, (label, pts) in enumerate(series):
        if not pts:
            continue
        color = PALETTE[si % len(PALETTE)]
        coords = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in pts)
        if len(pts) > 1:
            p.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="1.8"/>')
        for i, v in pts:
            p.append(f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="3" fill="{color}">'
                     f'<title>{_esc(label)} @ {_esc(labels[i])}: {y_fmt.format(v)}</title></circle>')
        ly = mt + 8 + si * 16
        p.append(f'<rect x="{ml + pw + 14}" y="{ly - 8}" width="10" height="10" fill="{color}"/>')
        p.append(f'<text x="{ml + pw + 30}" y="{ly + 1}" fill="#333">{_esc(label)}</text>')
    p.append("</svg>")
    out.write_text("\n".join(p) + "\n")
    log(f"chart → {out.relative_to(REPO)}")


# group → (title, ylabel, [(legend-label, metric-key), ...], svg filename, y_fmt)
GROUPS = [
    ("Flash on the M4 (release-mcu, text+data)", "KiB",
     [("code-only", "flash_code_kib"), ("+font", "flash_font_kib"),
      ("+font+image", "flash_fontimg_kib")], "size-flash.svg", "{:.0f}"),
    ("M4 workload heap peak", "KiB",
     [("Draft", "m4_draft_heap_kib"), ("Exact", "m4_exact_heap_kib")],
     "mem-heap.svg", "{:.0f}"),
    ("M4 insns/frame — INDICATIVE (wall-influenced, not gated)", "insns/frame",
     [("Draft", "m4_draft_insns"), ("Exact", "m4_exact_insns")],
     "perf-m4-insns.svg", "{:.3g}"),
    ("Primitive cost (host, vyr-bench median)", "ns / px",
     [("fill_rrect", "ns_fill_rrect"), ("disc", "ns_disc"),
      ("gradient", "ns_gradient"), ("line", "ns_line")],
     "perf-prim.svg", "{:.2g}"),
    ("Scene cost (host, vyr-bench median)", "ns / px",
     [("ir Exact", "ns_scene_exact"), ("ir Draft", "ns_scene_draft")],
     "perf-scene.svg", "{:.2g}"),
    ("Ladder host cost", "ns / px",
     [("480 full", "ladder480_full_ns_px"), ("480 incr/dirty", "ladder480_incr_ns_px"),
      ("4K full", "ladder4k_full_ns_px")], "perf-ladder.svg", "{:.2g}"),
    ("Draft fast-path coverage", "%",
     [("coverage", "draft_coverage_pct")], "fidelity-coverage.svg", "{:.0f}"),
]


def regen_pages(history: list[dict]) -> None:
    MET.mkdir(parents=True, exist_ok=True)
    labels = [f"{r['commit']}{'*' if r.get('dirty') else ''}" for r in history]

    def series_for(members):
        out = []
        for legend, key in members:
            pts = [(i, r["metrics"][key]) for i, r in enumerate(history)
                   if key in r.get("metrics", {})]
            out.append((legend, pts))
        return out

    imgs = ""
    for title, ylabel, members, fname, yfmt in GROUPS:
        svg_chart(title, ylabel, series_for(members), labels, MET / fname, yfmt)
        imgs += f'<p><img src="{fname}" alt="{_esc(title)}"></p>\n'

    latest = history[-1] if history else None
    snap = ""
    if latest:
        for k, v in sorted(latest["metrics"].items()):
            snap += f"<tr><td>{_esc(k)}</td><td>{v}</td></tr>\n"
    runs = "".join(
        f"<li><code>{r['ts']}</code> · <code>{r['commit']}"
        f"{'+dirty' if r.get('dirty') else ''}</code> · {_esc(r['host'])} "
        f"({_esc(r['arch'])}) · {len(r.get('metrics', {}))} metrics</li>\n"
        for r in history
    )
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>vyr metrics history — size · memory · performance</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 880px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.1rem; margin-top: 2rem; }}
  p.lede {{ opacity: .8; }}
  table {{ border-collapse: collapse; font-size: .85rem; }}
  th, td {{ border: 1px solid #8884; padding: .25rem .5rem; text-align: right; }}
  th:first-child, td:first-child {{ text-align: left; }}
  img {{ max-width: 100%; border: 1px solid #8884; background: #fff; }}
  code {{ font-size: .9em; }}
</style></head><body>
<h1>vyr metrics history — size · memory · performance</h1>
<p class="lede">Every <code>./dev.py track</code> snapshots the valuable numbers
against the current git commit (a <code>*</code> = a dirty tree) and appends to
the append-only <a href="history.jsonl"><code>history.jsonl</code></a>, then
regenerates this page. The point: the numbers we throw around get a tracked,
graphed home. <b>The M4 insns/frame is INDICATIVE</b> — SYS_CLOCK is
wall-influenced on a plugin-less qemu; the deterministic signals are the host
ns/px (vyr-bench/ladder) + the M4 heap peak. Companion to the
<a href="../perf/index.html">F18 rig perf ledger</a>.</p>

<h2>Trends{f" — latest <code>{latest['commit']}{'*' if latest.get('dirty') else ''}</code> · {latest['ts']}" if latest else ""}</h2>
{imgs}
<h2>Latest snapshot</h2>
<table><tr><th>metric</th><th>value</th></tr>
{snap}</table>

<h2>Runs</h2>
<ol start="0">
{runs}</ol>
</body></html>
"""
    (MET / "index.html").write_text(html)
    log(f"page → {(MET / 'index.html').relative_to(REPO)} ({len(history)} run(s))")


def main(argv: list[str]) -> int:
    regen_only = "--regen-only" in argv
    rest = [a for a in argv if a != "--regen-only"]
    if rest:
        log(f"ERROR: unknown args: {rest}")
        return 2
    if not regen_only:
        append_record()
    history = load_history()
    if not history:
        log("ERROR: no history to chart (docs/metrics/history.jsonl is empty)")
        return 1
    regen_pages(history)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

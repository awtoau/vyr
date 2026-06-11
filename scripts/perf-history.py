#!/usr/bin/env python3
"""perf-history — the F18 perf ledger + grapher (issue #18).

Appends one run record (from the rig outputs in ./tmp) to the committed,
append-only ``docs/perf/history.jsonl``, then regenerates the published
perf page: ``docs/perf/index.html`` + hand-rolled SVG charts (stdlib only —
keeping deps honest; no matplotlib).

Inputs (./tmp):
  rig-ladder.json       required to append (``./dev.py ladder`` writes it)
  rig-anim-stats.json   optional (``./dev.py anim``)
  rig-arm.json          optional (``./dev.py anim --arm`` — cross-ISA verdict)

Usage: scripts/perf-history.py [--regen-only]

Canonical entry point: ``./dev.py perf-history``. All output timestamped,
mirrored to ./tmp/perf-history.log.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
PERF = REPO / "docs" / "perf"
HISTORY = PERF / "history.jsonl"

# One stable colour per ladder rung (charts stay readable across history).
RUNG_COLORS = {
    "120x68": "#5b8def",
    "240x135": "#44b07b",
    "480x270": "#e0a03c",
    "960x540": "#d9534f",
    "1920x1080": "#9a6fd0",
    "3840x2160": "#3fb6c4",
}
FALLBACK_COLOR = "#888888"


def log(msg: str) -> None:
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1e6):06d} UTC"
    line = f"{stamp}  INFO  [perf-history] {msg}"
    print(line, file=sys.stderr)
    TMP.mkdir(exist_ok=True)
    with open(TMP / "perf-history.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _git_commit() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, cwd=REPO, check=False,
    )
    return out.stdout.strip() or "unknown"


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def append_record() -> dict | None:
    ladder_path = TMP / "rig-ladder.json"
    if not ladder_path.exists():
        log("ERROR: tmp/rig-ladder.json missing — run ./dev.py ladder first "
            "(or use --regen-only)")
        return None
    ladder = json.loads(ladder_path.read_text())
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "commit": _git_commit(),
        "host": platform.node(),
        "cpu": _cpu_model(),
        "arch": platform.machine(),
        "rungs": ladder["rungs"],
    }
    for key, name in (("anim", "rig-anim-stats.json"), ("arm", "rig-arm.json")):
        p = TMP / name
        if p.exists():
            rec[key] = json.loads(p.read_text())
        else:
            log(f"note: tmp/{name} absent — record carries no {key!r} section")
    PERF.mkdir(parents=True, exist_ok=True)
    with open(HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    log(f"appended run record (commit {rec['commit']}, {len(rec['rungs'])} rungs) "
        f"→ {HISTORY.relative_to(REPO)}")
    return rec


def load_history() -> list[dict]:
    if not HISTORY.exists():
        return []
    return [json.loads(line) for line in HISTORY.read_text().splitlines() if line.strip()]


# --- hand-rolled SVG ---------------------------------------------------------

def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def svg_chart(
    title: str,
    ylabel: str,
    series: list[tuple[str, str, list[tuple[int, float]]]],
    n_runs: int,
    out: Path,
    y_fmt: str = "{:.2f}",
) -> None:
    """One line chart: x = run index (history order), one polyline+markers per
    series. ``series`` = [(label, colour, [(run_idx, value), ...]), ...]."""
    W, H = 760, 320
    ml, mr, mt, mb = 64, 168, 34, 36  # margins: right one holds the legend
    pw, ph = W - ml - mr, H - mt - mb
    ys = [v for _, _, pts in series for _, v in pts]
    if not ys:
        out.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="60">'
                       f'<text x="10" y="30" font-size="13">{_esc(title)}: no data yet</text></svg>\n')
        return
    y_max = max(ys) * 1.15 or 1.0
    xs_max = max(1, n_runs - 1)

    def px(i: int) -> float:
        return ml + (pw * (i / xs_max) if n_runs > 1 else pw / 2)

    def py(v: float) -> float:
        return mt + ph * (1 - v / y_max)

    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
         f'font-family="system-ui,sans-serif" font-size="11">',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         f'<text x="{ml}" y="20" font-size="13" font-weight="600" fill="#222">{_esc(title)}</text>']
    # y grid + ticks (5 divisions)
    for k in range(6):
        v = y_max * k / 5
        y = py(v)
        p.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{ml + pw}" y2="{y:.1f}" '
                 f'stroke="#dddddd" stroke-width="1"/>')
        p.append(f'<text x="{ml - 6}" y="{y + 4:.1f}" text-anchor="end" fill="#555">'
                 f'{y_fmt.format(v)}</text>')
    # x ticks: run indices (sparse when many)
    step = max(1, n_runs // 10)
    for i in range(0, n_runs, step):
        p.append(f'<text x="{px(i):.1f}" y="{mt + ph + 16}" text-anchor="middle" '
                 f'fill="#555">{i}</text>')
    p.append(f'<text x="{ml + pw / 2:.1f}" y="{H - 6}" text-anchor="middle" fill="#555">'
             f'run # (history order)</text>')
    p.append(f'<text x="14" y="{mt + ph / 2:.1f}" text-anchor="middle" fill="#555" '
             f'transform="rotate(-90 14 {mt + ph / 2:.1f})">{_esc(ylabel)}</text>')
    # series
    for si, (label, color, pts) in enumerate(series):
        if not pts:
            continue
        coords = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in pts)
        if len(pts) > 1:
            p.append(f'<polyline points="{coords}" fill="none" stroke="{color}" '
                     f'stroke-width="1.8"/>')
        for i, v in pts:
            p.append(f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="3" fill="{color}">'
                     f'<title>{_esc(label)} run {i}: {y_fmt.format(v)}</title></circle>')
        ly = mt + 8 + si * 16
        p.append(f'<rect x="{ml + pw + 14}" y="{ly - 8}" width="10" height="10" fill="{color}"/>')
        p.append(f'<text x="{ml + pw + 30}" y="{ly + 1}" fill="#333">{_esc(label)}</text>')
    p.append("</svg>")
    out.write_text("\n".join(p) + "\n")
    log(f"chart → {out.relative_to(REPO)}")


def _rung_key(r: dict) -> str:
    return f"{r['w']}x{r['h']}"


def regen_pages(history: list[dict]) -> None:
    PERF.mkdir(parents=True, exist_ok=True)
    # All rung keys ever seen, ladder order (by pixel count).
    keys: list[str] = []
    for rec in history:
        for r in rec["rungs"]:
            k = _rung_key(r)
            if k not in keys:
                keys.append(k)
    keys.sort(key=lambda k: int(k.split("x")[0]))

    def series_for(metric) -> list[tuple[str, str, list[tuple[int, float]]]]:
        out = []
        for k in keys:
            pts = []
            for i, rec in enumerate(history):
                for r in rec["rungs"]:
                    if _rung_key(r) == k:
                        v = metric(r)
                        if v is not None:
                            pts.append((i, v))
            out.append((k, RUNG_COLORS.get(k, FALLBACK_COLOR), pts))
        return out

    n = len(history)
    svg_chart(
        "Full-frame render cost per rung (lower is better)",
        "ns / px",
        series_for(lambda r: r["full_ns_px"]),
        n, PERF / "ladder-full-ns-px.svg",
    )
    svg_chart(
        "Incremental repaint cost per rung (per dirty px)",
        "ns / dirty px",
        series_for(lambda r: r["incr_ns_dirty_px"]),
        n, PERF / "ladder-incr-ns-px.svg",
    )
    svg_chart(
        "Incremental speedup vs full frame (higher is better)",
        "full ms / incremental ms",
        series_for(lambda r: (r["full_ns"] / r["incr_ns"]) if r.get("incr_ns") else None),
        n, PERF / "ladder-incr-speedup.svg", y_fmt="{:.1f}",
    )
    insn_series = []
    pts = [
        (i, rec["arm"]["insn"])
        for i, rec in enumerate(history)
        if rec.get("arm") and rec["arm"].get("insn")
    ]
    have_insn = bool(pts)
    if have_insn:
        insn_series = [("anim run (ARM emulated)", "#d9534f", pts)]
        svg_chart(
            "Exact ARM instruction count, anim acceptance run (TCG plugin)",
            "instructions",
            insn_series, n, PERF / "arm-insn.svg", y_fmt="{:.3g}",
        )

    latest = history[-1] if history else None
    rows = ""
    if latest:
        for r in latest["rungs"]:
            rows += (
                f"<tr><td>{_rung_key(r)}</td>"
                f"<td>{r['full_ns'] / 1e6:.3f}</td><td>{r['full_ns_px']:.2f}</td>"
                f"<td>{r['headroom_full_x']:.2f}×</td>"
                f"<td>{r['incr_ns'] / 1e6:.3f}</td><td>{r['incr_ns_dirty_px']:.2f}</td>"
                f"<td>{r['headroom_incr_x']:.2f}×</td>"
                f"<td>{r['dirty_pct']:.1f}%</td>"
                f"<td>{r['full_ns'] / r['incr_ns']:.1f}×</td></tr>\n"
            )
    anim_line = arm_line = ""
    if latest and latest.get("anim"):
        a = latest["anim"]
        anim_line = (
            f"<p>Latest anim acceptance: <b>{a['frames']} frames @ "
            f"{a['w']}×{a['h']}</b>, run hash <code>{a['run_hash']}</code>, "
            f"dirty {a['dirty_pct']:.1f}%/step, {a['ms_frame']:.2f} ms/frame "
            f"(full + incremental + byte-verify{' + PNG dump' if a.get('dumped_pngs') else ''}, "
            f"host wall clock).</p>"
        )
    if latest and latest.get("arm"):
        m = latest["arm"]
        verdict = m.get("cross_isa", "?")
        badge = "✅ byte-identical" if verdict == "identical" else f"❌ {verdict}"
        insn_txt = (
            f"; exact insn count {m['insn']:,}" if m.get("insn")
            else "; TCG plugins unavailable on this host (no insn counts — "
                 "emulated wall time is NON-target-indicative)"
        )
        arm_line = (
            f"<p>Cross-ISA rung (qemu-arm-static, ARMv7 musl static build): "
            f"<b>{badge}</b> across {m['frames']} frames{insn_txt}.</p>"
        )
    runs_list = "".join(
        f"<li><code>{rec['ts']}</code> · <code>{rec['commit']}</code> · "
        f"{_esc(rec['host'])} ({_esc(rec['arch'])})</li>\n"
        for rec in history
    )
    insn_img = '<p><img src="arm-insn.svg" alt="ARM insn counts"></p>' if have_insn else ""
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>vyr perf history — the F18 rig ledger</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 880px;
         margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.1rem; margin-top: 2rem; }}
  p.lede {{ opacity: .8; }}
  table {{ border-collapse: collapse; font-size: .85rem; }}
  th, td {{ border: 1px solid #8884; padding: .25rem .5rem; text-align: right; }}
  th:first-child, td:first-child {{ text-align: left; }}
  img {{ max-width: 100%; border: 1px solid #8884; background: #fff; }}
  code {{ font-size: .9em; }}
</style>
</head>
<body>
<h1>vyr perf history — the rig ledger</h1>
<p class="lede">The F18 rig (<a href="https://github.com/awtoau/vyr/issues/18">#18</a>)
drives a frame-indexed parametric scene — slider sweep, toggle blink, a disc
crossing clip boundaries, a digits-only frame counter, the checker asset
translating — rendering every frame BOTH ways (full, and incrementally via
dirty rects) with per-frame byte-equality asserted, at six resolutions from
120×68 to 4K (3840×2160). Numbers are medians (vyr-bench methodology);
the committed regression goldens are the FNV-1a frame-hash chain
(<code>vyr-rig/hashchain.json</code>) and the ns/px baselines
(<code>vyr-rig/baseline.json</code>). This page regenerates from the
append-only <a href="history.jsonl"><code>history.jsonl</code></a> via
<code>./dev.py perf-history</code>.</p>

<h2>Latest run{f" — <code>{latest['commit']}</code> · {latest['ts']}" if latest else ""}</h2>
<table>
<tr><th>rung</th><th>full ms</th><th>ns/px</th><th>×60fps</th>
<th>incr ms</th><th>ns/dirty-px</th><th>×60fps</th><th>dirty</th><th>speedup</th></tr>
{rows}</table>
<p class="lede">×60fps = headroom vs the 16.67 ms budget (&gt;1 fits).
The 4K story the ladder exists to tell: the incremental path is where
60 fps @ 4K lives.</p>
{anim_line}{arm_line}

<h2>Trends over history</h2>
<p><img src="ladder-full-ns-px.svg" alt="full-frame ns/px per rung over history"></p>
<p><img src="ladder-incr-ns-px.svg" alt="incremental ns/dirty-px per rung over history"></p>
<p><img src="ladder-incr-speedup.svg" alt="incremental speedup per rung over history"></p>
{insn_img}

<h2>Runs</h2>
<ol start="0">
{runs_list}</ol>
<p class="lede">Host wall-clock numbers are desktop-host numbers; the
embedded story is measured separately (F9). Anything emulated is labelled
NON-target-indicative — QEMU measures the host, never the F427.</p>
</body>
</html>
"""
    (PERF / "index.html").write_text(html)
    log(f"page → {(PERF / 'index.html').relative_to(REPO)} ({n} run(s) charted)")


def main(argv: list[str]) -> int:
    regen_only = "--regen-only" in argv
    rest = [a for a in argv if a != "--regen-only"]
    if rest:
        log(f"ERROR: unknown args: {rest}")
        return 2
    if not regen_only and append_record() is None:
        return 1
    history = load_history()
    if not history:
        log("ERROR: no history to chart (docs/perf/history.jsonl is empty)")
        return 1
    regen_pages(history)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

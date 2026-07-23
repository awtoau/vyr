#!/usr/bin/env python3
"""Render ONE chart from the merged ledger (tmp/merged-ledger.jsonl).

The merged ledger mixes units — ns/px for the ladder rungs, ms/frame for the
anim block, KiB for flash, instructions for the M4 counts. Putting those on one
linear axis would be a dual-axis chart, which is never correct. The only
legitimate way to get them onto a SINGLE axis is to index every series to its
own first observation = 100, so the axis becomes dimensionless "% of baseline"
and the shapes are directly comparable.

Only series with >= 2 observations can be a trend; the rest are reported as
single points and deliberately left off the chart rather than drawn as a
one-point "line".

Writes:
  tmp/merged-ledger.html   the chart (self-contained, light + dark)
  tmp/chart-merged-ledger.log
"""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
SRC = TMP / "merged-ledger.jsonl"
OUT = TMP / "merged-ledger.html"
LOG = TMP / "chart-merged-ledger.log"

# Categorical slots 1-4, validated on the adjacent pairlist in both modes.
SERIES = [
    ("ladder480_full_ns_px", "480x270 full", "#2a78d6", "#3987e5", "ns/px"),
    ("ladder480_incr_ns_px", "480x270 incremental", "#eb6834", "#d95926", "ns/dirty-px"),
    ("ladder4k_full_ns_px", "4K full", "#1baf7a", "#199e70", "ns/px"),
    ("anim_ms_frame", "anim frame time", "#eda100", "#c98500", "ms/frame"),
]

W, H = 940, 460
PAD_L, PAD_R, PAD_T, PAD_B = 66, 168, 56, 74

_lines: list[str] = []


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")
    line = f"{stamp} UTC  INFO  [chart-ledger] {msg}"
    print(line)
    _lines.append(line)


def load() -> list[dict]:
    rows = [json.loads(ln) for ln in SRC.read_text().splitlines() if ln.strip()]
    rows.sort(key=lambda d: d["ts"])
    log(f"read {len(rows)} merged row(s)")
    return rows


def value_for(row: dict, key: str):
    if key == "anim_ms_frame":
        return (row.get("anim") or {}).get("ms_frame")
    if key in (row.get("bridge") or {}):
        return row["bridge"][key]
    return (row.get("metrics") or {}).get(key)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build() -> str:
    rows = load()
    n = len(rows)

    xs = [PAD_L + (W - PAD_L - PAD_R) * (i / max(n - 1, 1)) for i in range(n)]

    plotted, skipped = [], []
    for key, label, light, dark, unit in SERIES:
        raw = [value_for(r, key) for r in rows]
        present = [v for v in raw if v is not None]
        if len(present) < 2:
            skipped.append((label, len(present)))
            continue
        base = present[0]
        idx = [None if v is None else v / base * 100.0 for v in raw]
        plotted.append(
            {"key": key, "label": label, "light": light, "dark": dark,
             "unit": unit, "raw": raw, "idx": idx, "base": base}
        )
        log(f"plot {label}: {len(present)}/{n} points, base={base:g} {unit}")
    for label, cnt in skipped:
        log(f"skip {label}: {cnt} point(s) — cannot form a trend")

    vals = [v for s in plotted for v in s["idx"] if v is not None]
    lo, hi = min(vals), max(vals)
    span = max(hi - lo, 1.0)
    lo, hi = lo - span * 0.18, hi + span * 0.18

    def y(v: float) -> float:
        return PAD_T + (H - PAD_T - PAD_B) * (1 - (v - lo) / (hi - lo))

    parts: list[str] = []

    # ---- recessive grid + y axis -------------------------------------------
    step = 5 if (hi - lo) <= 40 else 10
    tick = int(lo // step) * step
    while tick <= hi:
        if tick >= lo:
            yy = y(tick)
            parts.append(
                f'<line class="grid" x1="{PAD_L}" y1="{yy:.1f}" x2="{W-PAD_R}" y2="{yy:.1f}"/>'
            )
            parts.append(
                f'<text class="tick" x="{PAD_L-12}" y="{yy+4:.1f}" text-anchor="end">{tick}</text>'
            )
        tick += step

    # the 100 baseline gets emphasis — it is what every series is indexed to
    parts.append(
        f'<line class="baseline" x1="{PAD_L}" y1="{y(100):.1f}" x2="{W-PAD_R}" y2="{y(100):.1f}"/>'
    )

    # ---- x labels: commit + run time ---------------------------------------
    for i, r in enumerate(rows):
        t = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
        parts.append(
            f'<text class="xlab" x="{xs[i]:.1f}" y="{H-PAD_B+26}" text-anchor="middle">{esc(r["commit"])}</text>'
        )
        parts.append(
            f'<text class="xsub" x="{xs[i]:.1f}" y="{H-PAD_B+44}" text-anchor="middle">'
            f'{t.strftime("%m-%d %H:%M")}</text>'
        )
        parts.append(
            f'<text class="xsrc" x="{xs[i]:.1f}" y="{H-PAD_B+60}" text-anchor="middle">{esc(r["source"])}</text>'
        )

    # ---- series ------------------------------------------------------------
    for si, s in enumerate(plotted):
        # split into contiguous runs so a gap is a real gap, never interpolated
        run: list[tuple[float, float]] = []
        runs: list[list[tuple[float, float]]] = []
        for i, v in enumerate(s["idx"]):
            if v is None:
                if len(run) > 1:
                    runs.append(run)
                elif len(run) == 1:
                    runs.append(run)
                run = []
            else:
                run.append((xs[i], y(v)))
        if run:
            runs.append(run)

        for r in runs:
            if len(r) < 2:
                continue
            d = " ".join(f"{'M' if j == 0 else 'L'}{x:.1f},{yy:.1f}" for j, (x, yy) in enumerate(r))
            parts.append(f'<path class="ln s{si}" d="{d}"/>')

        for i, v in enumerate(s["idx"]):
            if v is None:
                continue
            parts.append(
                f'<circle class="pt s{si}" cx="{xs[i]:.1f}" cy="{y(v):.1f}" r="5.5"/>'
            )

        # direct label at the last present point (relief for the contrast WARN)
        last = max(i for i, v in enumerate(s["idx"]) if v is not None)
        parts.append(
            f'<text class="dlab s{si}t" x="{xs[last]+14:.1f}" y="{y(s["idx"][last])+4:.1f}">'
            f'{esc(s["label"])}</text>'
        )

    # ---- hover layer -------------------------------------------------------
    for i, r in enumerate(rows):
        tip = [f'{r["commit"]} · {r["source"]}']
        for s in plotted:
            v, iv = s["raw"][i], s["idx"][i]
            tip.append(
                f'{s["label"]}: ' + ("no data" if v is None else f'{v:.4g} {s["unit"]} ({iv:.1f})')
            )
        parts.append(
            f'<g class="hit" data-tip="{esc(" | ".join(tip))}">'
            f'<rect x="{xs[i]-34:.1f}" y="{PAD_T}" width="68" height="{H-PAD_T-PAD_B}"/>'
            f'<line class="cross" x1="{xs[i]:.1f}" y1="{PAD_T}" x2="{xs[i]:.1f}" y2="{H-PAD_B}"/>'
            f"</g>"
        )

    svg = f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Merged vyr ledger, indexed to first observation">{"".join(parts)}</svg>'

    # ---- table view (relief + the accessible fallback) ----------------------
    head = "".join(f"<th>{esc(r['commit'])}<span>{esc(r['source'])}</span></th>" for r in rows)
    body = ""
    for si, s in enumerate(plotted):
        cells = ""
        for i in range(n):
            v, iv = s["raw"][i], s["idx"][i]
            cells += "<td>—</td>" if v is None else f"<td>{v:.4g}<span>{iv:.1f}</span></td>"
        body += f'<tr><th scope="row"><i class="sw s{si}b"></i>{esc(s["label"])}<span>{esc(s["unit"])}</span></th>{cells}</tr>'

    skip_rows = "".join(
        f'<li>{esc(l)} — {c} observation{"s" if c != 1 else ""}</li>' for l, c in skipped
    )
    single = [
        k for r in rows for k in (r.get("metrics") or {})
    ]
    single_list = "".join(f"<li>{esc(k)}</li>" for k in sorted(set(single)))

    css_series = "".join(
        f".s{i}{{stroke:{s['light']}}}.s{i}t{{fill:{s['light']}}}.s{i}b{{background:{s['light']}}}"
        for i, s in enumerate(plotted)
    )
    css_series_dark = "".join(
        f".s{i}{{stroke:{s['dark']}}}.s{i}t{{fill:{s['dark']}}}.s{i}b{{background:{s['dark']}}}"
        for i, s in enumerate(plotted)
    )

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>vyr merged ledger — one chart</title>
<style>
:root{{color-scheme:light;--surface-1:#fcfcfb;--surface-2:#f3f2ef;--text-primary:#0b0b0b;
--text-secondary:#52514e;--text-muted:#7a7873;--rule:#e2e0db;--grid:#ebe9e4}}
{css_series}
@media (prefers-color-scheme:dark){{:root:where(:not([data-theme=light])){{color-scheme:dark;
--surface-1:#1a1a19;--surface-2:#232322;--text-primary:#fff;--text-secondary:#c3c2b7;
--text-muted:#8f8e86;--rule:#343431;--grid:#2b2b29}}
:root:where(:not([data-theme=light])){{{css_series_dark}}}}}
:root[data-theme=dark]{{color-scheme:dark;--surface-1:#1a1a19;--surface-2:#232322;
--text-primary:#fff;--text-secondary:#c3c2b7;--text-muted:#8f8e86;--rule:#343431;--grid:#2b2b29}}
:root[data-theme=dark]{{{css_series_dark}}}
*{{box-sizing:border-box}}
body{{margin:0;padding:32px 24px 56px;background:var(--surface-1);color:var(--text-primary);
font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:1000px;margin:0 auto}}
h1{{font-size:21px;margin:0 0 6px;letter-spacing:-.01em}}
.sub{{color:var(--text-secondary);margin:0 0 4px;font-size:14px}}
.note{{color:var(--text-muted);font-size:13px;margin:0 0 22px}}
.fig{{background:var(--surface-2);border:1px solid var(--rule);border-radius:10px;
padding:8px 4px 4px;overflow-x:auto;position:relative}}
svg{{display:block;width:100%;height:auto;min-width:720px}}
.grid{{stroke:var(--grid);stroke-width:1}}
.baseline{{stroke:var(--text-muted);stroke-width:1;stroke-dasharray:3 3;opacity:.75}}
.tick,.xlab,.xsub,.xsrc{{fill:var(--text-muted);font-size:11px}}
.xlab{{fill:var(--text-secondary);font-size:12px;font-family:ui-monospace,monospace}}
.xsrc{{font-size:10px;letter-spacing:.05em;text-transform:uppercase}}
.ln{{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}}
.pt{{stroke:var(--surface-2);stroke-width:2}}
.s0.pt{{fill:#2a78d6}}.s1.pt{{fill:#eb6834}}.s2.pt{{fill:#1baf7a}}.s3.pt{{fill:#eda100}}
.dlab{{font-size:12px;font-weight:600}}
.hit rect{{fill:transparent}}
.hit .cross{{stroke:var(--text-muted);stroke-width:1;stroke-dasharray:2 3;opacity:0}}
.hit:hover .cross{{opacity:.8}}
#tip{{position:absolute;pointer-events:none;opacity:0;transform:translate(-50%,-100%);
background:var(--surface-1);border:1px solid var(--rule);border-radius:7px;padding:8px 10px;
font-size:12px;line-height:1.5;color:var(--text-primary);box-shadow:0 6px 22px rgb(0 0 0/.16);
max-width:330px;white-space:pre-line;z-index:5}}
table{{border-collapse:collapse;width:100%;margin-top:26px;font-size:13px}}
caption{{text-align:left;color:var(--text-secondary);padding-bottom:9px;font-size:13px}}
th,td{{border-bottom:1px solid var(--rule);padding:8px 10px;text-align:right}}
thead th{{color:var(--text-secondary);font-weight:600}}
tbody th{{text-align:left;font-weight:500;color:var(--text-primary)}}
th span,td span{{display:block;font-size:11px;color:var(--text-muted);font-weight:400}}
td{{font-family:ui-monospace,monospace}}
.sw{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:8px}}
.gap{{margin-top:26px;padding:14px 16px;background:var(--surface-2);border:1px solid var(--rule);
border-radius:10px}}
.gap h2{{font-size:13px;margin:0 0 8px;color:var(--text-secondary);text-transform:uppercase;
letter-spacing:.06em}}
.gap ul{{margin:0;padding-left:20px;color:var(--text-secondary);font-size:13px;columns:2}}
.gap p{{margin:0 0 8px;color:var(--text-muted);font-size:13px}}
</style></head><body><div class="wrap">
<h1>vyr merged ledger — one chart</h1>
<p class="sub">docs/perf + docs/metrics, unioned. Every series indexed to its own first
observation = 100, so mixed units (ns/px, ns/dirty-px, ms/frame) share one axis.</p>
<p class="note">Lower is better for all four series. A gap means that harness did not run at
that commit — the line is broken rather than interpolated.</p>
<div class="fig">{svg}<div id="tip"></div></div>
<table>
<caption>Same data, absolute values with the indexed figure beneath each.</caption>
<thead><tr><th scope="col">Series</th>{head}</tr></thead>
<tbody>{body}</tbody></table>
<div class="gap"><h2>Not plottable — single observation only</h2>
<p>These exist at exactly one commit, so they have no trend to draw. They are the
<code>docs/metrics</code> payload, recorded once at <code>f2f0724</code>.</p>
<ul>{skip_rows}{single_list}</ul></div>
</div>
<script>
const tip=document.getElementById('tip'),fig=document.querySelector('.fig');
document.querySelectorAll('.hit').forEach(g=>{{
  g.addEventListener('mousemove',e=>{{
    const r=fig.getBoundingClientRect();
    tip.textContent=g.dataset.tip.split(' | ').join('\\n');
    tip.style.left=(e.clientX-r.left)+'px';
    tip.style.top=(e.clientY-r.top-14)+'px';
    tip.style.opacity=1;
  }});
  g.addEventListener('mouseleave',()=>tip.style.opacity=0);
}});
</script></body></html>"""
    return html


def main() -> None:
    TMP.mkdir(exist_ok=True)
    OUT.write_text(build())
    log(f"wrote {OUT.relative_to(REPO)}")
    LOG.write_text("\n".join(_lines) + "\n")


if __name__ == "__main__":
    main()

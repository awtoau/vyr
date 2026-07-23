#!/usr/bin/env python3
"""Chart vyr's M4 instructions/frame across commits, against the LVGL anchor.

Unlike the merged-ledger chart, every value here is the SAME unit
(instructions per frame), so no indexing is needed — one natural axis.

The axis is LOG10. Exact sits near 75 M while Draft ends near 8.5 M: on a
linear axis the whole Draft-vs-LVGL crossover — the thing the chart exists to
show — would be compressed into the bottom 10 % of the plot. A log axis is the
standard, non-misleading choice for perf data spanning an order of magnitude,
and the axis is explicitly labelled as such.

The LVGL anchor is drawn as a horizontal reference line, NOT a categorical
series: it is a fixed threshold measured once on the same emulated silicon,
not a quantity that varies per vyr commit.

Reads tmp/m4-history.jsonl (from m4-history-sweep.py).
Writes tmp/m4-history.html + tmp/chart-m4-history.log
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
SRC = TMP / "m4-history.jsonl"
OUT = TMP / "m4-history.html"
LOG = TMP / "chart-m4-history.log"

# Measured fresh on this host, same qemu machine + methodology, 40 frames /
# 39 cs. See awto-vyvanse/scripts/lvgl-m4-bench (LVGL is MIT and stays out of
# the vyr repo — vyr is clean-room).
LVGL_INSNS = 9_750_000
LVGL_NOTE = "LVGL v9.6 — 9.75 M (measured, 39 cs / 40 frames)"

C_EXACT_L, C_EXACT_D = "#2a78d6", "#3987e5"   # slot 1
C_DRAFT_L, C_DRAFT_D = "#eb6834", "#d95926"   # slot 2

W, H = 1000, 480
PAD_L, PAD_R, PAD_T, PAD_B = 74, 210, 44, 116

_lines: list[str] = []


def log(m: str) -> None:
    s = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"{s} UTC  [chart-m4] {m}"
    print(line)
    _lines.append(line)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build() -> str:
    rows = [json.loads(l) for l in SRC.read_text().splitlines() if l.strip()]
    log(f"read {len(rows)} commit row(s)")

    n = len(rows)
    xs = [PAD_L + (W - PAD_L - PAD_R) * (i / max(n - 1, 1)) for i in range(n)]

    ex = [(r["exact"] or {}).get("insns_per_frame") for r in rows]
    dr = [(r["draft"] or {}).get("insns_per_frame") for r in rows]
    vals = [v for v in ex + dr + [LVGL_INSNS] if v]
    lo, hi = min(vals) * 0.72, max(vals) * 1.20

    def y(v: float) -> float:
        t = (math.log10(v) - math.log10(lo)) / (math.log10(hi) - math.log10(lo))
        return PAD_T + (H - PAD_T - PAD_B) * (1 - t)

    p: list[str] = []

    # log gridlines at 1-2-5 decades
    tick = 10 ** math.floor(math.log10(lo))
    while tick <= hi:
        for mult in (1, 2, 5):
            v = tick * mult
            if lo <= v <= hi:
                yy = y(v)
                p.append(f'<line class="grid" x1="{PAD_L}" y1="{yy:.1f}" x2="{W-PAD_R}" y2="{yy:.1f}"/>')
                lab = f"{v/1e6:g} M" if v >= 1e6 else f"{v/1e3:g} k"
                p.append(f'<text class="tick" x="{PAD_L-11}" y="{yy+4:.1f}" text-anchor="end">{lab}</text>')
        tick *= 10

    # ---- the LVGL reference line -------------------------------------------
    ly = y(LVGL_INSNS)
    p.append(f'<line class="anchor" x1="{PAD_L}" y1="{ly:.1f}" x2="{W-PAD_R}" y2="{ly:.1f}"/>')
    p.append(f'<text class="alab" x="{W-PAD_R+12}" y="{ly-6:.1f}">LVGL anchor</text>')
    p.append(f'<text class="asub" x="{W-PAD_R+12}" y="{ly+11:.1f}">9.75 M — measured</text>')

    # ---- series ------------------------------------------------------------
    for key, series, cls, label in (
        ("exact", ex, "ex", "vyr Exact"),
        ("draft", dr, "dr", "vyr Draft"),
    ):
        pts = [(xs[i], y(v)) for i, v in enumerate(series) if v]
        if len(pts) > 1:
            d = " ".join(f"{'M' if j==0 else 'L'}{x:.1f},{yy:.1f}" for j, (x, yy) in enumerate(pts))
            p.append(f'<path class="ln {cls}" d="{d}"/>')
        for x, yy in pts:
            p.append(f'<circle class="pt {cls}" cx="{x:.1f}" cy="{yy:.1f}" r="4.5"/>')
        if pts:
            lx, ly2 = pts[-1]
            p.append(f'<text class="dlab {cls}t" x="{lx+13:.1f}" y="{ly2+4:.1f}">{label}</text>')

    # ---- x ticks: every commit, label a readable subset --------------------
    step = max(1, n // 11)
    for i, r in enumerate(rows):
        p.append(f'<line class="xtick" x1="{xs[i]:.1f}" y1="{H-PAD_B}" x2="{xs[i]:.1f}" y2="{H-PAD_B+5}"/>')
        if i % step == 0 or i == n - 1:
            p.append(
                f'<text class="xlab" x="{xs[i]:.1f}" y="{H-PAD_B+20}" text-anchor="end" '
                f'transform="rotate(-42 {xs[i]:.1f} {H-PAD_B+20})">{esc(r["commit"])}</text>'
            )

    # ---- hover -------------------------------------------------------------
    for i, r in enumerate(rows):
        e, d = ex[i], dr[i]
        t = [f'{r["commit"]} — {r["subject"][:64]}']
        t.append(f'Exact: {e:,} insns/frame' if e else "Exact: no data")
        if d:
            t.append(f'Draft: {d:,} insns/frame ({d/LVGL_INSNS:.2f}x LVGL)')
        else:
            t.append("Draft: tier does not exist yet")
        cs = (r["draft"] or r["exact"] or {}).get("cs")
        if cs:
            t.append(f'raw: {cs} cs / {(r["draft"] or r["exact"]).get("frames")} frames')
        p.append(
            f'<g class="hit" data-tip="{esc(" | ".join(t))}">'
            f'<rect x="{xs[i]-16:.1f}" y="{PAD_T}" width="32" height="{H-PAD_T-PAD_B}"/>'
            f'<line class="cross" x1="{xs[i]:.1f}" y1="{PAD_T}" x2="{xs[i]:.1f}" y2="{H-PAD_B}"/></g>'
        )

    svg = f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="vyr M4 instructions per frame across commits versus the LVGL anchor">{"".join(p)}</svg>'

    # ---- table -------------------------------------------------------------
    body = ""
    for i, r in enumerate(rows):
        e, d = ex[i], dr[i]
        ratio = f"{d/LVGL_INSNS:.2f}x" if d else "—"
        win = ' class="win"' if d and d < LVGL_INSNS else ""
        body += (
            f'<tr><td class="c">{esc(r["commit"])}</td>'
            f'<td class="s">{esc(r["subject"][:70])}</td>'
            f'<td>{e/1e6:.1f} M</td>' if e else
            f'<tr><td class="c">{esc(r["commit"])}</td><td class="s">{esc(r["subject"][:70])}</td><td>—</td>'
        )
        body += f'<td>{d/1e6:.2f} M</td>' if d else "<td>—</td>"
        body += f'<td{win}>{ratio}</td></tr>'

    first_win = next((r["commit"] for i, r in enumerate(rows) if dr[i] and dr[i] < LVGL_INSNS), None)
    log(f"first commit under the LVGL anchor: {first_win}")

    dark = f".ex{{stroke:{C_EXACT_D}}}.ext{{fill:{C_EXACT_D}}}.ex.pt{{fill:{C_EXACT_D}}}.dr{{stroke:{C_DRAFT_D}}}.drt{{fill:{C_DRAFT_D}}}.dr.pt{{fill:{C_DRAFT_D}}}"

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>vyr on the M4 — instructions/frame vs LVGL</title><style>
:root{{color-scheme:light;--surface-1:#fcfcfb;--surface-2:#f3f2ef;--text-primary:#0b0b0b;
--text-secondary:#52514e;--text-muted:#7a7873;--rule:#e2e0db;--grid:#ebe9e4;--good:#008300}}
.ex{{stroke:{C_EXACT_L}}}.ext{{fill:{C_EXACT_L}}}.ex.pt{{fill:{C_EXACT_L}}}
.dr{{stroke:{C_DRAFT_L}}}.drt{{fill:{C_DRAFT_L}}}.dr.pt{{fill:{C_DRAFT_L}}}
@media (prefers-color-scheme:dark){{:root:where(:not([data-theme=light])){{color-scheme:dark;
--surface-1:#1a1a19;--surface-2:#232322;--text-primary:#fff;--text-secondary:#c3c2b7;
--text-muted:#8f8e86;--rule:#343431;--grid:#2b2b29;--good:#3fa64a}}
:root:where(:not([data-theme=light])){{{dark}}}}}
:root[data-theme=dark]{{color-scheme:dark;--surface-1:#1a1a19;--surface-2:#232322;
--text-primary:#fff;--text-secondary:#c3c2b7;--text-muted:#8f8e86;--rule:#343431;
--grid:#2b2b29;--good:#3fa64a}}:root[data-theme=dark]{{{dark}}}
*{{box-sizing:border-box}}body{{margin:0;padding:30px 22px 50px;background:var(--surface-1);
color:var(--text-primary);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,sans-serif}}
.wrap{{max-width:1060px;margin:0 auto}}h1{{font-size:21px;margin:0 0 6px}}
.sub{{color:var(--text-secondary);margin:0 0 3px;font-size:14px}}
.note{{color:var(--text-muted);font-size:13px;margin:0 0 20px}}
.fig{{background:var(--surface-2);border:1px solid var(--rule);border-radius:10px;
padding:8px 4px 4px;position:relative;overflow-x:auto}}
svg{{display:block;width:100%;height:auto;min-width:760px}}
.grid{{stroke:var(--grid);stroke-width:1}}
.anchor{{stroke:var(--good);stroke-width:2;stroke-dasharray:6 4}}
.alab{{fill:var(--good);font-size:12px;font-weight:600}}
.asub{{fill:var(--text-muted);font-size:11px}}
.tick,.xlab{{fill:var(--text-muted);font-size:11px}}
.xlab{{font-family:ui-monospace,monospace}}
.xtick{{stroke:var(--text-muted);stroke-width:1;opacity:.4}}
.ln{{fill:none;stroke-width:2;stroke-linejoin:round}}
.pt{{stroke:var(--surface-2);stroke-width:1.5}}
.dlab{{font-size:12.5px;font-weight:600}}
.hit rect{{fill:transparent}}.hit .cross{{stroke:var(--text-muted);stroke-width:1;
stroke-dasharray:2 3;opacity:0}}.hit:hover .cross{{opacity:.85}}
#tip{{position:absolute;pointer-events:none;opacity:0;transform:translate(-50%,-100%);
background:var(--surface-1);border:1px solid var(--rule);border-radius:7px;padding:8px 10px;
font-size:12px;line-height:1.5;box-shadow:0 6px 22px rgb(0 0 0/.16);max-width:360px;
white-space:pre-line;z-index:5}}
table{{border-collapse:collapse;width:100%;margin-top:24px;font-size:13px}}
caption{{text-align:left;color:var(--text-secondary);padding-bottom:9px}}
th,td{{border-bottom:1px solid var(--rule);padding:7px 9px;text-align:right}}
th{{color:var(--text-secondary);font-weight:600}}
td.c{{text-align:left;font-family:ui-monospace,monospace}}
td.s{{text-align:left;color:var(--text-secondary);font-size:12px}}
td.win{{color:var(--good);font-weight:700}}
</style></head><body><div class="wrap">
<h1>vyr on the emulated Cortex-M4 — instructions/frame vs LVGL</h1>
<p class="sub">Every commit that can run the M4 vehicle, measured with <em>its own</em>
<code>dev.py qemu-m4</code>. Same 480x270 scene, same <code>netduinoplus2</code> machine,
same icount methodology as the LVGL bench. Lower is better.</p>
<p class="note">Log axis — Exact sits near 75 M while Draft ends near 8.5 M, so a linear
axis would flatten the crossover this chart exists to show. The vehicle reports whole
centiseconds (1 cs = 10^7 insns), so a 20-frame run quantises to +/-0.5 M/frame; hover any
point for the raw cs count. {esc(LVGL_NOTE)}.</p>
<div class="fig">{svg}<div id="tip"></div></div>
<table><caption>Per-commit measurements. The ratio column is Draft vs the measured LVGL anchor;
values under 1.00x (green) beat LVGL.</caption>
<thead><tr><th style="text-align:left">commit</th><th style="text-align:left">subject</th>
<th>Exact</th><th>Draft</th><th>Draft / LVGL</th></tr></thead><tbody>{body}</tbody></table>
</div><script>
const tip=document.getElementById('tip'),fig=document.querySelector('.fig');
document.querySelectorAll('.hit').forEach(g=>{{
 g.addEventListener('mousemove',e=>{{const r=fig.getBoundingClientRect();
  tip.textContent=g.dataset.tip.split(' | ').join('\\n');
  tip.style.left=(e.clientX-r.left)+'px';tip.style.top=(e.clientY-r.top-14)+'px';
  tip.style.opacity=1;}});
 g.addEventListener('mouseleave',()=>tip.style.opacity=0);}});
</script></body></html>"""


def main() -> None:
    OUT.write_text(build())
    log(f"wrote {OUT.relative_to(REPO)}")
    LOG.write_text("\n".join(_lines) + "\n")


if __name__ == "__main__":
    main()

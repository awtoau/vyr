#!/usr/bin/env python3
"""microbench-html.py — render the micro-benchmark SQLite DB to a viewer page.

Produces `docs/perf/microbench.html`: a sortable GRID (Tabulator) of every
point plus GRAPHS (uPlot) of the cost scaling and the tier ladder, styled and
vendored exactly like `docs/perf/index.html` — same theme CSS vars, same two
libraries from `docs/perf/vendor/`, self-contained, no CDN. Open with
`code-insiders docs/perf/microbench.html` (or a browser served from docs/perf).

Reads the latest run in the DB by default. `./dev.py microbench` regenerates
it after every measurement; run standalone to re-render without measuring:

    python3 scripts/microbench-html.py [--db tmp/microbench.db] [--run N]

Output: docs/perf/microbench.html + tmp/microbench-html.log
"""
from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
OUT = REPO / "docs" / "perf" / "microbench.html"
CLI = REPO / "target" / "release" / "vyr-cli"
SIZE_HOST = REPO / "target" / "debug" / "vyr-size"
SCENES = TMP / "probe-scenes"           # the IR JSON per point
SCENE_PNG = TMP / "microbench-scenes"   # rendered PNGs per (point, tier)

# The exact theme index.html ships (light default + dark override), so the two
# pages read as one site. Kept in sync by hand; if index.html reskins, copy.
THEME = """
:root{--bg:#f9f9f7;--surface:#fcfcfb;--sunken:#eeede9;--ink:#0b0b0b;
--ink-soft:#52514e;--ink-mut:#77756f;--line:#e1e0d9;--line-strong:#c3c2b7;
--accent:#2a78d6;--shadow:0 1px 2px rgba(11,11,11,.05),0 8px 24px rgba(11,11,11,.045);}
@media(prefers-color-scheme:dark){:root{--bg:#0d0d0d;--surface:#1a1a19;
--sunken:#111110;--ink:#fff;--ink-soft:#c3c2b7;--ink-mut:#898781;--line:#2c2c2a;
--line-strong:#383835;--accent:#3987e5;--shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35);}}
:root[data-theme=dark]{--bg:#0d0d0d;--surface:#1a1a19;--sunken:#111110;--ink:#fff;
--ink-soft:#c3c2b7;--ink-mut:#898781;--line:#2c2c2a;--line-strong:#383835;--accent:#3987e5;}
:root[data-theme=light]{--bg:#f9f9f7;--surface:#fcfcfb;--sunken:#eeede9;--ink:#0b0b0b;
--ink-soft:#52514e;--ink-mut:#77756f;--line:#e1e0d9;--line-strong:#c3c2b7;--accent:#2a78d6;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:24px}
h1{font-size:20px;margin:0 0 2px} h2{font-size:15px;margin:28px 0 10px;color:var(--ink-soft)}
.sub{color:var(--ink-mut);font-size:12px;margin-bottom:20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin:14px 0 8px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;
padding:14px 16px;box-shadow:var(--shadow)}
.card .k{color:var(--ink-mut);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:22px;font-weight:600;margin-top:3px}
.card .n{color:var(--ink-mut);font-size:11px;margin-top:2px}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:16px}
.chart{background:var(--surface);border:1px solid var(--line);border-radius:10px;
padding:12px 14px 4px;box-shadow:var(--shadow)}
.chart h3{font-size:13px;margin:0 0 2px} .chart .cap{color:var(--ink-mut);font-size:11px;margin:0 0 8px}
.split{display:flex;height:22px;border-radius:5px;overflow:hidden;border:1px solid var(--line);margin:4px 0}
.split>span{display:block} .legend{font-size:11px;color:var(--ink-mut);margin-top:6px}
.legend b{display:inline-block;width:9px;height:9px;border-radius:2px;margin:0 3px 0 10px;vertical-align:middle}
#grid{background:var(--surface);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow)}
.uplot{font-family:inherit}
a{color:var(--accent)}
"""

# f64 = determinism tax (red-ish), hw-f32 (amber), mem (blue = the real driver),
# rest (muted). One palette, used by both the SVG bars and the uPlot legend.
CLS_COLORS = {"f64": "#d9534f", "hwf32": "#e0a13c", "mem": "#3987e5", "rest": "#9a988f"}


def load(db_path: Path, run: int | None) -> tuple[dict, list[dict]]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    if run is None:
        run = con.execute("SELECT max(run_id) FROM run").fetchone()[0]
    meta = dict(con.execute("SELECT * FROM run WHERE run_id=?", (run,)).fetchone())
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM points WHERE run_id=? AND name!='null' ORDER BY tier,name", (run,))]
    con.close()
    return meta, rows


def scaling_series(rows: list[dict], tier: str, alpha: int, kinds: list[str]) -> dict:
    """uPlot data for insns/px vs width, one series per kind (opaque)."""
    widths = sorted({r["w"] for r in rows if r["tier"] == tier and r["alpha"] == alpha})
    data = [widths]
    for kind in kinds:
        by_w = {r["w"]: r["insns_per_px"] for r in rows
                if r["tier"] == tier and r["alpha"] == alpha and r["kind"] == kind
                and r["radius"] in (0, 8)}  # rrect: the r=8 row
        data.append([by_w.get(w) for w in widths])
    return {"x": widths, "series": data}


def ladder_series(rows: list[dict], kind: str, alpha: int, tiers: list[str]) -> dict:
    widths = sorted({r["w"] for r in rows if r["kind"] == kind and r["alpha"] == alpha})
    data = [widths]
    for tier in tiers:
        by_w = {r["w"]: r["insns_per_px"] for r in rows
                if r["tier"] == tier and r["kind"] == kind and r["alpha"] == alpha
                and r["radius"] == 0}
        data.append([by_w.get(w) for w in widths])
    return {"x": widths, "series": data}


def class_bars(rows: list[dict], tier: str) -> list[dict]:
    """Average class split per kind (deep points only) → stacked-bar spec."""
    out = []
    for kind in ("rect", "rrect", "disc"):
        sel = [r for r in rows if r["tier"] == tier and r["kind"] == kind
               and r["f64_share"] is not None]
        if not sel:
            continue
        f64 = 100 * sum(r["f64_share"] for r in sel) / len(sel)
        hw = 100 * sum(r["f32_hw_share"] for r in sel) / len(sel)
        mem = 100 * sum(r["mem_share"] for r in sel) / len(sel)
        out.append({"kind": kind, "f64": f64, "hwf32": hw, "mem": mem,
                    "rest": max(0.0, 100 - f64 - hw - mem)})
    return out


def render_scenes(names: list[str], tiers: list[str]) -> dict:
    """Render each point's IR to PNG at every tier so you can SEE the test:
    the scene it draws and how the tier changes the pixels. Returns
    {name: {"ir": json, "png": {tier: data-uri}}}. Builds vyr-cli + dumps the
    scene IR on demand (the same case_ir the M4 measured — one definition)."""
    if not CLI.exists():
        subprocess.run(["cargo", "build", "--release", "-p", "vyr-cli"],
                       cwd=REPO, check=True, capture_output=True)
    if not SCENES.exists() or not any(SCENES.glob("*.json")):
        subprocess.run(["cargo", "build", "-p", "vyr-size", "--no-default-features",
                        "--features", "run-qemu,probe"], cwd=REPO, check=True, capture_output=True)
        subprocess.run([str(SIZE_HOST), "--dump-probe-scenes", str(SCENES)],
                       cwd=REPO, check=True, capture_output=True)
    SCENE_PNG.mkdir(parents=True, exist_ok=True)
    # vy_image srcs resolve under $VYR_ASSETS; the committed checker lives here.
    import os
    env = {**os.environ, "VYR_ASSETS": str(REPO / "vyr-core" / "tests" / "assets")}
    out: dict = {}
    for name in names:
        sj = SCENES / f"{name}.json"
        if not sj.is_file():
            continue
        entry = {"ir": sj.read_text(), "png": {}}
        for tier in tiers:
            png = SCENE_PNG / f"{name}.{tier}.png"
            cmd = [str(CLI), "render", str(sj), str(png)]
            if tier != "exact":
                cmd.append(f"--{tier}")
            r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, env=env)
            if r.returncode == 0 and png.is_file():
                b64 = base64.b64encode(png.read_bytes()).decode()
                entry["png"][tier] = f"data:image/png;base64,{b64}"
        out[name] = entry
    return out


def tests_html(meta: dict, rows: list[dict], scenes: dict) -> str:
    """The 'see the tests' section: one card per distinct scene (anchor
    id=scene-<name>) with the rendered pixels at each tier, the per-tier
    insns/px from the DB, and the scene IR."""
    tiers = sorted({r["tier"] for r in rows})
    by_name_tier = {(r["tier"], r["name"]): r for r in rows}
    # distinct scenes in grid order (skip any the renderer couldn't produce)
    names = [n for n in dict.fromkeys(r["name"] for r in rows) if n in scenes]
    cards = []
    for name in names:
        sc = scenes[name]
        any_row = next((by_name_tier[(t, name)] for t in tiers if (t, name) in by_name_tier), None)
        meta_line = ""
        if any_row:
            meta_line = (f'{any_row["kind"]} · w={any_row["w"]} · α={any_row["alpha"]}'
                         f' · r={any_row["radius"]} · {any_row["px"]:,} px')
        tiles = ""
        for tier in tiers:
            uri = sc["png"].get(tier)
            row = by_name_tier.get((tier, name))
            ipp = f'{row["insns_per_px"]:.0f} insn/px' if row and row["insns_per_px"] else "—"
            img = (f'<img src="{uri}" alt="{name} {tier}" loading="lazy">'
                   if uri else '<div class="noimg">—</div>')
            tiles += (f'<figure><figcaption>{tier} · {ipp}</figcaption>{img}</figure>')
        cards.append(
            f'<div class="scene" id="scene-{name}"><div class="sh">'
            f'<b>{name}</b> <span class="cap">{meta_line}</span>'
            f'<a class="cap" href="#grid-top">↑ grid</a></div>'
            f'<div class="tiles">{tiles}</div>'
            f'<details><summary class="cap">IR scene</summary>'
            f'<pre>{sc["ir"]}</pre></details></div>')
    return "".join(cards)


TESTS_CSS = """
.scenes-wrap{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
.scene{background:var(--surface);border:1px solid var(--line);border-radius:10px;
padding:12px 14px;box-shadow:var(--shadow);scroll-margin-top:14px}
.scene .sh{display:flex;align-items:baseline;gap:10px;margin-bottom:8px}
.scene .sh b{font-size:13px} .scene .sh a{margin-left:auto;text-decoration:none}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.tiles figure{margin:0} .tiles figcaption{font-size:10px;color:var(--ink-mut);margin-bottom:3px}
.tiles img{width:100%;height:auto;border:1px solid var(--line);border-radius:4px;
background:#101418;image-rendering:pixelated}
.tiles .noimg{aspect-ratio:16/9;display:grid;place-items:center;color:var(--ink-mut);
border:1px dashed var(--line);border-radius:4px}
.scene details{margin-top:8px} .scene summary{cursor:pointer}
.scene pre{background:var(--sunken);border-radius:6px;padding:8px;overflow:auto;
max-height:180px;font-size:11px;margin:6px 0 0}
"""


def render(meta: dict, rows: list[dict], scenes: dict | None = None) -> str:
    tiers = sorted({r["tier"] for r in rows})
    kinds = ["rect", "rrect", "disc"]
    n_deep = sum(1 for r in rows if r["f64_share"] is not None)
    # headline cards
    def avg(kind, field, tier="exact"):
        sel = [r[field] for r in rows if r["tier"] == tier and r["kind"] == kind
               and r[field] is not None]
        return sum(sel) / len(sel) if sel else 0.0
    cards = [
        ("points", f"{len(rows)}", f"{len(tiers)} tiers · {n_deep} class-split"),
        ("commit", meta["git_commit"], f"band_h={meta['band_h']} · {meta['machine']}"),
        ("mem share, disc (Exact)", f"{100*avg('disc','mem_share'):.0f}%", "the real per-primitive driver"),
        ("f64 tax, disc (Exact)", f"{100*avg('disc','f64_share'):.1f}%", "warm — memo-defeated (#32/#63)"),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div>'
        f'<div class="n">{n}</div></div>' for k, v, n in cards)

    # class-split stacked bars (Exact)
    bars = class_bars(rows, "exact")
    bar_html = ""
    for b in bars:
        segs = "".join(
            f'<span style="width:{b[c]:.2f}%;background:{CLS_COLORS[c]}" '
            f'title="{c} {b[c]:.1f}%"></span>' for c in ("f64", "hwf32", "mem", "rest"))
        bar_html += (f'<div style="font-size:12px;margin-top:8px">{b["kind"]} '
                     f'<span style="color:var(--ink-mut)">f64 {b["f64"]:.1f}% · '
                     f'hw-f32 {b["hwf32"]:.1f}% · mem {b["mem"]:.0f}%</span>'
                     f'<div class="split">{segs}</div></div>')
    legend = ('<div class="legend">'
              + "".join(f'<b style="background:{CLS_COLORS[c]}"></b>{lbl}'
                        for c, lbl in [("f64", "soft-f64 (tax)"), ("hwf32", "hw-f32"),
                                       ("mem", "memory"), ("rest", "int/other")])
              + "</div>")

    payload = {
        "rows": rows,
        "scaling": {t: scaling_series(rows, t, 255, kinds) for t in tiers},
        "ladder_disc": ladder_series(rows, "disc", 255, tiers),
        "tiers": tiers, "kinds": kinds,
        "cls_colors": CLS_COLORS,
    }
    tests = tests_html(meta, rows, scenes) if scenes else ""
    tests_section = (f'<h2>See the tests — {len(scenes)} scenes, rendered at each tier</h2>'
                     f'<div class=scenes-wrap>{tests}</div>') if scenes else ""

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>vyr micro-benchmark — painter cost landscape (#37)</title>
<link rel=stylesheet href="vendor/tabulator.min.css">
<link rel=stylesheet href="vendor/uplot.min.css">
<style>{THEME}{TESTS_CSS}</style></head><body>
<h1>vyr micro-benchmark — painter cost landscape</h1>
<div class=sub>{len(rows)} points · {meta['git_commit']} · {meta['ts']} · #37/#63 ·
generated by scripts/microbench-html.py from tmp/microbench.db</div>
<div class=cards>{cards_html}</div>

<h2>Cost scaling — insns/px vs width (opaque)</h2>
<div class=charts>
  <div class=chart><h3>Exact tier, by primitive</h3>
    <p class=cap>log y. Small shapes are dominated by per-draw setup + AA fringe.</p>
    <div id=c_exact></div></div>
  <div class=chart><h3>Disc, tier ladder</h3>
    <p class=cap>Exact vs Fast vs Draft — the integer path is orders cheaper on curves.</p>
    <div id=c_ladder></div></div>
</div>

<h2>Where the instructions go — class split (Exact, deep pass)</h2>
<div class=chart style="max-width:640px">{bar_html}{legend}</div>

<h2 id=grid-top>Every point <span class=cap style="font-weight:400">— click a point to see the test it renders ↓</span></h2>
<div id=grid></div>

{tests_section}

<script src="vendor/tabulator.min.js"></script>
<script src="vendor/uplot.min.js"></script>
<script>
const D = {json.dumps(payload)};
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const KIND_C = {{rect: css('--ink-mut'), rrect: '#e0a13c', disc: '#3987e5'}};
const TIER_C = {{exact: '#d9534f', fast: '#e0a13c', draft: '#3987e5'}};

function line(elId, ser, labels, colors, ytitle) {{
  const opts = {{
    width: elId.clientWidth || 380, height: 240,
    scales: {{y: {{distr: 3}}}},   // log
    axes: [{{stroke: css('--ink-mut'), grid: {{stroke: css('--line')}}, label: 'width (px)'}},
           {{stroke: css('--ink-mut'), grid: {{stroke: css('--line')}}, label: ytitle}}],
    series: [{{}}].concat(labels.map((l,i) => ({{
      label: l, stroke: colors[i], width: 2, spanGaps: true,
      points: {{show: true, size: 5}}}}))),
    legend: {{stroke: css('--ink')}},
  }};
  new uPlot(opts, ser, elId);
}}

const ex = D.scaling[D.tiers.includes('exact') ? 'exact' : D.tiers[0]];
line(document.getElementById('c_exact'), ex.series, D.kinds,
     D.kinds.map(k => KIND_C[k]), 'insns / px');
line(document.getElementById('c_ladder'), D.ladder_disc.series, D.tiers,
     D.tiers.map(t => TIER_C[t] || css('--accent')), 'insns / px');

// the grid
const pct = c => c.getValue()==null ? '—' : (100*c.getValue()).toFixed(1)+'%';
new Tabulator('#grid', {{
  data: D.rows, layout: 'fitDataFill', height: '620px', pagination: false,
  initialSort: [{{column: 'insns_per_px', dir: 'desc'}}],
  columns: [
    {{title:'tier', field:'tier', headerFilter:'list',
      headerFilterParams:{{values:['',...D.tiers]}}, width:80}},
    {{title:'point', field:'name', headerFilter:'input', width:130,
      formatter:c=>'<a href="#scene-'+c.getValue()+'">'+c.getValue()+'</a>'}},
    {{title:'kind', field:'kind', headerFilter:'list',
      headerFilterParams:{{values:['','rect','rrect','disc']}}, width:80}},
    {{title:'w', field:'w', sorter:'number', hozAlign:'right', width:64}},
    {{title:'α', field:'alpha', sorter:'number', hozAlign:'right', width:56}},
    {{title:'r', field:'radius', sorter:'number', hozAlign:'right', width:56}},
    {{title:'px', field:'px', sorter:'number', hozAlign:'right',
      formatter:c=>c.getValue().toLocaleString()}},
    {{title:'insns', field:'insns', sorter:'number', hozAlign:'right',
      formatter:c=>c.getValue().toLocaleString()}},
    {{title:'insns/px', field:'insns_per_px', sorter:'number', hozAlign:'right',
      formatter:c=>c.getValue()==null?'—':c.getValue().toFixed(1)}},
    {{title:'f64', field:'f64_share', sorter:'number', hozAlign:'right', formatter:pct}},
    {{title:'hw-f32', field:'f32_hw_share', sorter:'number', hozAlign:'right', formatter:pct}},
    {{title:'mem', field:'mem_share', sorter:'number', hozAlign:'right', formatter:pct}},
    {{title:'vyr/LVGL', field:'lvgl_ratio', sorter:'number', hozAlign:'right',
      formatter:c=>c.getValue()==null?'—':c.getValue().toFixed(1)+'×'}},
  ],
}});
</script></body></html>"""


def render_standalone(meta: dict, rows: list[dict], scenes: dict | None = None) -> str:
    """A SELF-CONTAINED variant for hosting (Artifact / shareable link): uPlot
    inlined from vendor/, and a lightweight vanilla sortable table in place of
    Tabulator (436 KB) so the whole page is one lean file with no sibling
    assets. Same theme, same charts, same numbers as the repo page."""
    vend = REPO / "docs" / "perf" / "vendor"
    uplot_css = (vend / "uplot.min.css").read_text()
    uplot_js = (vend / "uplot.min.js").read_text()
    tiers = sorted({r["tier"] for r in rows})
    kinds = ["rect", "rrect", "disc"]
    n_deep = sum(1 for r in rows if r["f64_share"] is not None)

    def avg(kind, field, tier="exact"):
        sel = [r[field] for r in rows if r["tier"] == tier and r["kind"] == kind
               and r[field] is not None]
        return sum(sel) / len(sel) if sel else 0.0
    cards = [
        ("points", f"{len(rows)}", f"{len(tiers)} tiers · {n_deep} class-split"),
        ("commit", meta["git_commit"], f"band_h={meta['band_h']} · {meta['machine']}"),
        ("mem share, disc (Exact)", f"{100*avg('disc','mem_share'):.0f}%", "the real per-primitive driver"),
        ("f64 tax, disc (Exact)", f"{100*avg('disc','f64_share'):.1f}%", "warm — memo-defeated (#32/#63)"),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div>'
        f'<div class="n">{n}</div></div>' for k, v, n in cards)

    bars = class_bars(rows, "exact")
    bar_html = ""
    for b in bars:
        segs = "".join(
            f'<span style="width:{b[c]:.2f}%;background:{CLS_COLORS[c]}" '
            f'title="{c} {b[c]:.1f}%"></span>' for c in ("f64", "hwf32", "mem", "rest"))
        bar_html += (f'<div style="font-size:12px;margin-top:8px">{b["kind"]} '
                     f'<span style="color:var(--ink-mut)">f64 {b["f64"]:.1f}% · '
                     f'hw-f32 {b["hwf32"]:.1f}% · mem {b["mem"]:.0f}%</span>'
                     f'<div class="split">{segs}</div></div>')
    legend = ('<div class="legend">' + "".join(
        f'<b style="background:{CLS_COLORS[c]}"></b>{lbl}' for c, lbl in
        [("f64", "soft-f64 (tax)"), ("hwf32", "hw-f32"), ("mem", "memory"), ("rest", "int/other")])
        + "</div>")

    # vanilla table
    cols = [("tier", "tier", 0), ("point", "name", 0), ("kind", "kind", 0), ("w", "w", 1),
            ("α", "alpha", 1), ("r", "radius", 1), ("px", "px", 1), ("insns", "insns", 1),
            ("insns/px", "insns_per_px", 1), ("f64", "f64_share", 2),
            ("hw-f32", "f32_hw_share", 2), ("mem", "mem_share", 2),
            ("vyr/LVGL", "lvgl_ratio", 3)]
    head = "".join(f'<th data-c="{i}">{lbl}</th>' for i, (lbl, _, _) in enumerate(cols))
    body = ""
    for r in sorted(rows, key=lambda x: -(x["insns_per_px"] or 0)):
        tds = ""
        for _lbl, f, kind in cols:
            v = r[f]
            if v is None:
                disp, sval = "—", "-1"
            elif kind == 1:
                disp, sval = (f"{v:,.1f}" if f == "insns_per_px" else f"{v:,}"), str(v)
            elif kind == 2:
                disp, sval = f"{100*v:.1f}%", str(v)
            elif kind == 3:  # ratio (vyr/LVGL)
                disp, sval = f"{v:.1f}×", str(v)
            else:
                disp, sval = str(v), str(v)
            if f == "name" and scenes and v in scenes:
                disp = f'<a href="#scene-{v}">{disp}</a>'
            al = "left" if kind == 0 else "right"
            tds += f'<td style="text-align:{al}" data-v="{sval}">{disp}</td>'
        body += f"<tr>{tds}</tr>"

    payload = {"scaling": {t: scaling_series(rows, t, 255, kinds) for t in tiers},
               "ladder_disc": ladder_series(rows, "disc", 255, tiers),
               "tiers": tiers, "kinds": kinds}
    tests = tests_html(meta, rows, scenes) if scenes else ""
    tests_section = (f'<h2>See the tests — {len(scenes)} scenes, rendered at each tier</h2>'
                     f'<div class=scenes-wrap>{tests}</div>') if scenes else ""

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>vyr micro-benchmark — painter cost landscape (#37)</title>
<style>{uplot_css}</style><style>{THEME}
table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:12.5px}}
#grid{{overflow:auto;max-height:640px}}
th,td{{padding:5px 10px;border-bottom:1px solid var(--line);white-space:nowrap}}
th{{position:sticky;top:0;background:var(--sunken);cursor:pointer;text-align:right;
user-select:none;font-weight:600}} th:first-child,th:nth-child(2),th:nth-child(3){{text-align:left}}
tr:hover td{{background:var(--sunken)}}
{TESTS_CSS}
</style></head><body>
<h1>vyr micro-benchmark — painter cost landscape</h1>
<div class=sub>{len(rows)} points · {meta['git_commit']} · {meta['ts']} · #37/#63</div>
<div class=cards>{cards_html}</div>
<h2>Cost scaling — insns/px vs width (opaque)</h2>
<div class=charts>
  <div class=chart><h3>Exact tier, by primitive</h3>
    <p class=cap>log y. Small shapes are dominated by per-draw setup + AA fringe.</p>
    <div id=c_exact></div></div>
  <div class=chart><h3>Disc, tier ladder</h3>
    <p class=cap>Exact vs Fast vs Draft — the integer path is orders cheaper on curves.</p>
    <div id=c_ladder></div></div>
</div>
<h2>Where the instructions go — class split (Exact, deep pass)</h2>
<div class=chart style="max-width:640px">{bar_html}{legend}</div>
<h2 id=grid-top>Every point <span class=cap style="font-weight:400">— click a point to see its test ↓, or a header to sort</span></h2>
<div id=grid><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>
{tests_section}
<script>{uplot_js}</script>
<script>
const D = {json.dumps(payload)};
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const KIND_C = {{rect: css('--ink-mut'), rrect: '#e0a13c', disc: '#3987e5'}};
const TIER_C = {{exact: '#d9534f', fast: '#e0a13c', draft: '#3987e5'}};
function lchart(el, ser, labels, colors, ytitle) {{
  new uPlot({{width: el.clientWidth||380, height: 240, scales:{{y:{{distr:3}}}},
    axes:[{{stroke:css('--ink-mut'),grid:{{stroke:css('--line')}},label:'width (px)'}},
          {{stroke:css('--ink-mut'),grid:{{stroke:css('--line')}},label:ytitle}}],
    series:[{{}}].concat(labels.map((l,i)=>({{label:l,stroke:colors[i],width:2,
      spanGaps:true,points:{{show:true,size:5}}}}))),
    legend:{{stroke:css('--ink')}}}}, ser, el);
}}
const ex = D.scaling[D.tiers.includes('exact')?'exact':D.tiers[0]];
lchart(document.getElementById('c_exact'), ex.series, D.kinds, D.kinds.map(k=>KIND_C[k]), 'insns / px');
lchart(document.getElementById('c_ladder'), D.ladder_disc.series, D.tiers,
       D.tiers.map(t=>TIER_C[t]||css('--accent')), 'insns / px');
// vanilla sortable table
document.querySelectorAll('th').forEach(th => th.onclick = () => {{
  const tb = th.closest('table').querySelector('tbody');
  const ci = +th.dataset.c, asc = th.dataset.asc !== 'y';
  th.closest('tr').querySelectorAll('th').forEach(h=>h.dataset.asc='');
  th.dataset.asc = asc ? 'y' : 'n';
  [...tb.rows].sort((a,b)=>{{
    const x=a.cells[ci].dataset.v, y=b.cells[ci].dataset.v;
    const nx=parseFloat(x), ny=parseFloat(y);
    const c = (!isNaN(nx)&&!isNaN(ny)) ? nx-ny : x.localeCompare(y);
    return asc ? c : -c;
  }}).forEach(r=>tb.appendChild(r));
}});
</script></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(REPO / "docs" / "perf" / "microbench.db"))
    ap.add_argument("--run", type=int, default=None)
    ap.add_argument("--standalone", default=None,
                    help="also write a SELF-CONTAINED page (inlined uPlot, vanilla "
                         "table) to this path — for hosting / a shareable link")
    ap.add_argument("--no-tests", action="store_true",
                    help="skip rendering the per-point scene images (faster)")
    a = ap.parse_args()
    db_path = Path(a.db)
    if not db_path.exists():
        print(f"no DB at {db_path} — run ./dev.py microbench first")
        return 1
    meta, rows = load(db_path, a.run)

    scenes = None
    if not a.no_tests:
        names = list(dict.fromkeys(r["name"] for r in rows))
        tiers = sorted({r["tier"] for r in rows})
        print(f"rendering {len(names)} scenes x {len(tiers)} tiers (the tests) ...")
        scenes = render_scenes(names, tiers)
        print(f"rendered {sum(len(s['png']) for s in scenes.values())} images")

    OUT.write_text(render(meta, rows, scenes))
    print(f"wrote {OUT} ({len(rows)} points, {len(scenes or {})} scenes)")
    if a.standalone:
        sp = Path(a.standalone)
        sp.write_text(render_standalone(meta, rows, scenes))
        print(f"wrote {sp} (self-contained, {sp.stat().st_size // 1024} KB)")
    log = REPO / "tmp" / "microbench-html.log"
    log.write_text(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] wrote {OUT} "
                   f"({len(rows)} points, run {meta['run_id']})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

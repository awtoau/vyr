#!/usr/bin/env python3
"""Verify the generated measurement-ledger page — and RENDER it, in a browser.

``scripts/ledger.py`` writes ``docs/perf/index.html``: one line chart drawn on a
canvas by a vendored uPlot, and three flat tables built in JavaScript by a
vendored Tabulator. That is the whole reason this script exists. A generator can
emit a perfectly well-formed page whose tables never appear and whose chart
paints nothing — an empty ``<div>`` reads identically to a full one in the HTML
source — so the only way to know the page works is to open it in a real browser,
count the rows it actually built, count the pixels it actually inked, drive a
sort and a hover, and look at the picture.

What it checks:

  1. ``history.jsonl`` is untouched — a page regeneration must never be able to
     move a measured number.
  2. every measured value, every ``null``'s written reason and every
     ``fold_provenance`` value in EVERY matrix row of the ledger appears in the
     page. The page is now the whole history rather than its latest row, so
     this check covers the whole history too.
  3. regeneration is deterministic (same input, byte-identical output).
  4. the page fetches nothing at render time: no asset is loaded from a URL, and
     every relative reference resolves to a file on disk. Tabulator and uPlot
     are both vendored, each licence text is present and says MIT (this repo is
     GPL-3.0-only + commercial and its deps must be permissive), and each
     vendored file's sha256 is the one ``vendor/README.md`` records.
  5. theme-aware three ways (light root, OS media query, both ``data-theme``
     scopes), and wide content scrolls in its own container.
  5b. the chart's payload is sound before a browser touches it: no series with
     fewer than two observations (one point is not a trend), none indexed
     against a zero first observation (a ratio to zero has no value), at most
     the three emphasised series taking a categorical slot, and the series that
     never changed still present rather than quietly dropped.
  6. it actually renders and actually works: headless Chrome builds the tables
     and a probe reports each table's row count, re-sorts a numeric column and
     returns the order it produced, and reads back the tooltip on a null cell.
     The sort must be NUMERIC (9,220,422 below 48,239,550 — which a lexical
     sort of formatted strings gets backwards) and the null must still carry
     the reason the harness recorded for it. The same probe reads the chart's
     canvas back pixel by pixel (a configured-but-blank canvas is the failure
     that static inspection cannot see), checks that every series begins at
     exactly index 100, that the holes in the data are holes in the lines, and
     drives a hover to confirm one series is named and the rest are dimmed.
  7. full-page screenshots in light and dark, with the mean luminance of each
     asserted, so a theme that silently failed to apply is caught rather than
     assumed. Headless Chrome reports ``prefers-color-scheme: dark``, so the
     light shot is taken by seeding the page's own theme toggle — which
     exercises the real script rather than a stamped attribute the script would
     (correctly) strip on load.

Usage: scripts/ledger-verify.py [--no-shots]
Output: tmp/ledger-verify.log, tmp/ledger-page-{light,dark}.png
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
PERF = REPO / "docs" / "perf"
OUT: list[str] = []
FAIL = 0
# The page is one fixed-height chart and three fixed-height tables, so unlike the
# 13-chart wall it replaced it does not grow as the ledger grows.
SHOT_W, SHOT_H = 1600, 3100

# Injected into a copy of the page. Tabulator builds asynchronously, so the
# probe hangs off `tableBuilt` (or runs at once if the build already finished)
# and appends what it found to the DOM for --dump-dom to read back.
PROBE = """
<script>
(function(){
  function out(o){
    var d = document.createElement('div');
    d.id = 'vyr-probe';
    d.textContent = JSON.stringify(o);
    document.body.appendChild(d);
  }
  var T = window.vyrLedgerTables || {};
  var ids = Object.keys(T);
  if (!ids.length) { out({error: 'no tables registered on window'}); return; }
  var res = {rows: {}, sorted: null, null_title: null, null_cells: 0, error: null};
  var pending = ids.length;
  /* The chart is a CANVAS built in JS. Its <div> reads identically full or
     empty in the HTML source and even a correctly-configured uPlot can paint
     nothing, so this counts the pixels it actually inked, walks the data for a
     real gap, and drives the hover readout. */
  function probeChart(done){
    var c = {ok: false};
    var u = window.vyrLedgerChart;
    var host = document.getElementById('t-chart');
    c.dead = host ? host.getAttribute('data-dead') : 'no-host';
    if (!u) { c.error = 'no chart registered on window'; done(c); return; }
    c.series = u.series.length - 1;
    c.points = u.data[0].length;
    c.size = [u.width, u.height];
    // a null with a non-null on both sides: a hole the line must break across
    var gaps = 0, isolated = 0, nulls = 0;
    for (var s = 1; s < u.data.length; s++) {
      var d = u.data[s];
      for (var i = 0; i < d.length; i++) {
        if (d[i] == null) { nulls++; if (i > 0 && i < d.length - 1
              && d[i-1] != null && d[i+1] != null) { gaps++; } }
        else if ((i === 0 || d[i-1] == null) && (i === d.length-1 || d[i+1] == null)) {
          isolated++;
        }
      }
    }
    c.interior_gaps = gaps; c.nulls = nulls; c.isolated_points = isolated;
    // index arithmetic: every series' first non-null must be exactly 100
    var bad100 = 0;
    for (var s2 = 1; s2 < u.data.length; s2++) {
      var d2 = u.data[s2];
      for (var j = 0; j < d2.length; j++) {
        if (d2[j] != null) { if (Math.abs(d2[j] - 100) > 1e-9) { bad100++; } break; }
      }
    }
    c.first_not_100 = bad100;
    try {
      var cv = u.ctx.canvas;
      var px = u.ctx.getImageData(0, 0, cv.width, cv.height).data;
      var bg = px.slice(0, 4), inked = 0, total = cv.width * cv.height;
      for (var p = 0; p < px.length; p += 4) {
        if (px[p] !== bg[0] || px[p+1] !== bg[1] || px[p+2] !== bg[2]
            || px[p+3] !== bg[3]) { inked++; }
      }
      c.inked_px = inked; c.canvas_px = total;
      c.inked_pct = Math.round(inked / total * 10000) / 100;
    } catch (e) { c.error = 'canvas read: ' + String(e); }

    /* Hover with a REAL pointer event on uPlot's own overlay, not a
       programmatic setSeries — the thing being tested is that moving a mouse
       near a line picks that line out of 129 and names it. */
    var si = -1;
    for (var k = 1; k < u.series.length; k++) {
      if (u.series[k].label === '__PROBE_KEY__') { si = k; }
    }
    c.hover_series = si;
    if (si < 1) { c.error = (c.error ? c.error + '; ' : '') + 'probe key not a series'; }
    if (c.error) { c.ok = false; done(c); return; }
    try {
      var at = u.data[si].length - 1;
      while (at > 0 && u.data[si][at] == null) { at--; }
      var rect = u.over.getBoundingClientRect();
      var cx = rect.left + u.valToPos(at, 'x');
      var cy = rect.top + u.valToPos(u.data[si][at], 'y');
      c.hover_at = [Math.round(cx), Math.round(cy)];
      u.over.dispatchEvent(new MouseEvent('mousemove', {
        bubbles: true, clientX: cx, clientY: cy, view: window
      }));
    } catch (e2) {
      c.error = 'hover: ' + String(e2); c.ok = false; done(c); return;
    }
    // uPlot settles the cursor on an animation frame; read after it has
    requestAnimationFrame(function(){ requestAnimationFrame(function(){
      var tip = document.querySelector('.charttip');
      var dot = document.querySelector('.chartdot');
      c.tip_on = tip ? tip.getAttribute('data-on') : null;
      c.tip_text = tip ? tip.textContent : null;
      c.dot_on = dot ? dot.getAttribute('data-on') : null;
      c.cursor_idx = u.cursor.idx;
      c.dimmed = u.series.filter(function(s){ return s.alpha < 1; }).length;
      /* Every series key on this page is a short coloured STROKE. A rule scoped
         to the wrong ancestor paints nothing while the markup still reads
         correct, so the painted size and colour of each one is measured. */
      c.keys = [];
      Array.prototype.forEach.call(
        document.querySelectorAll('.legendrow .lk, .charttip .lk'), function(el){
          var s = getComputedStyle(el);
          c.keys.push([Math.round(el.getBoundingClientRect().width),
                       Math.round(el.getBoundingClientRect().height),
                       s.backgroundColor]);
        });
      c.ok = !c.error;
      done(c);
    }); });
  }
  function finish(){
    try {
      var t = T['t-cells'];
      t.setSort('insns_per_frame_total', 'desc');
      res.sorted = t.getRows('active').slice(0, 60).map(function(r){
        return r.getData().insns_per_frame_total;
      });
      var nulls = document.querySelectorAll('.tabulator-cell .nul[title]');
      res.null_cells = nulls.length;
      res.null_title = nulls.length ? nulls[0].getAttribute('title') : null;
      var seen = {};
      Array.prototype.forEach.call(nulls, function(n){
        seen[n.getAttribute('title')] = 1;
      });
      res.null_titles = Object.keys(seen);
    } catch (e) {
      res.error = String(e);
    }
    probeChart(function(c){ res.chart = c; out(res); });
  }
  ids.forEach(function(id){
    var t = T[id];
    function ready(){
      res.rows[id] = t.getRows().length;
      if (--pending === 0) { finish(); }
    }
    if (t.initialized) { ready(); } else { t.on('tableBuilt', ready); }
  });
})();
</script>
"""

ASSET_TAG = re.compile(
    r"<(?:script|link|img|iframe|source|video|audio|object|embed)\b[^>]*>", re.I)
ASSET_URL = re.compile(r'(?:src|href|data)="([^"]+)"', re.I)


def p(*a) -> None:
    s = " ".join(str(x) for x in a)
    OUT.append(s)
    print(s)


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAIL
    p(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAIL += 1


def sh(*args: str):
    return subprocess.run(args, capture_output=True, text=True, cwd=REPO)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def present(needle: str, html: str) -> bool:
    """A recorded string reaches the page either as HTML text or inside the
    page's JSON data island, so accept either spelling of it."""
    return needle in html or _esc(needle) in html


def main(argv: list[str]) -> int:
    global FAIL
    shots = "--no-shots" not in argv

    # 1. the data is not ours to change — the canonical store is SQLite now.
    d = sh("git", "diff", "--stat", "--", "docs/perf/ledger.db")
    check("ledger.db unmodified", d.stdout.strip() == "",
          d.stdout.strip() or "clean")

    import sys as _sys
    _sys.path.insert(0, str(REPO / "scripts"))
    import ledger_store as STORE
    rows = STORE.load_rows()
    meas = [r for r in rows if r.get("kind", "measurement") == "measurement"]
    mrows = [r for r in meas if r.get("matrix")]
    cells = [(r, c) for r in mrows for c in r["matrix"]["cells"]]
    html = (PERF / "index.html").read_text()

    # 2. every measured value survives — across the WHOLE ledger, not one row
    missing, n = [], 0
    for r, c in cells:
        if c.get("status") != "measured":
            continue
        for k, v in (c.get("metrics") or {}).items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            n += 1
            forms = {f"{v:,}", f"{v:,.0f}", str(v)}
            if isinstance(v, float):
                forms |= {f"{v:,.1f}", f"{v:,.4g}"}
            if not any(f in html for f in forms):
                missing.append(f"{r['commit']} {c['platform']}/{c['tier']}/"
                               f"{c.get('opt_level')} {k}={v}")
        fh = (c.get("metrics") or {}).get("frame_hash")
        if fh:
            n += 1
            if fh not in html:
                missing.append(f"{r['commit']} {c['platform']}/{c['tier']} "
                               f"frame_hash={fh}")
    check(f"all {n} measured values present verbatim in the page", not missing,
          "; ".join(missing[:6]))

    reasons = {c.get("reason") for _, c in cells
               if c.get("status") != "measured" and c.get("reason")}
    lost = [x for x in reasons if not present(x, html)]
    check(f"all {len(reasons)} not-measured reason(s) surfaced on the page", not lost,
          "; ".join(lost)[:200])

    notes = {v for _, c in cells for v in (c.get("metric_notes") or {}).values() if v}
    lostn = [x for x in notes if not present(x, html)]
    check(f"all {len(notes)} written reason(s) for a null metric surfaced", not lostn,
          "; ".join(lostn)[:200])

    provs = {(c.get("fold_provenance") or "").split(" (")[0].strip()
             for _, c in cells} - {""}
    gone = [q for q in provs if not present(q, html)]
    check(f"all {len(provs)} fold_provenance value(s) named on the page", not gone,
          "; ".join(gone))

    # 3. determinism
    r = sh("python3", "scripts/ledger.py", "--regen-only")
    check("regeneration is deterministic",
          r.returncode == 0 and (PERF / "index.html").read_text() == html,
          f"rc={r.returncode}")

    # 4. self-contained: served from GitHub Pages AND opened as a local file
    assets = [u for tag in ASSET_TAG.findall(html) for u in ASSET_URL.findall(tag)]
    remote = [u for u in assets if re.match(r"[a-z][a-z0-9+.\-]*:|//", u, re.I)]
    check(f"none of the {len(assets)} page asset(s) load from a URL", not remote,
          "; ".join(remote))
    dangling = [u for u in assets if not (PERF / u.split("#")[0]).exists()]
    check("every asset reference resolves to a file on disk", not dangling,
          "; ".join(dangling))
    vendor = PERF / "vendor"
    want = ("tabulator.min.js", "tabulator.min.css", "LICENSE.tabulator.txt",
            "uplot.min.js", "uplot.min.css", "LICENSE.uplot.txt")
    absent = [f for f in want if not (vendor / f).exists()]
    check("Tabulator and uPlot are vendored, each with its licence", not absent,
          ", ".join(absent) or
          f"{sum((vendor / f).stat().st_size for f in want) // 1024} KiB in docs/perf/vendor")
    # GPL-3.0-only + commercial: CLAUDE.md requires every dep be permissive, so
    # the licence text itself is checked, not a claim about it.
    lic = {"LICENSE.tabulator.txt": "MIT License",
           "LICENSE.uplot.txt": "The MIT License (MIT)"}
    bad_lic = [f for f, needle in lic.items()
               if f in absent or needle not in (vendor / f).read_text()]
    check("every vendored licence is permissive (MIT)", not bad_lic,
          ", ".join(bad_lic) or "tabulator + uplot")
    readme = (vendor / "README.md").read_text()
    unrecorded = [f for f in want if f.endswith((".js", ".css"))
                  and f not in absent
                  and hashlib.sha256((vendor / f).read_bytes()).hexdigest() not in readme]
    check("every vendored file's sha256 is recorded in vendor/README.md",
          not unrecorded, ", ".join(unrecorded))

    # 5. theme-aware three ways, no page-level h-scroll
    check("theme-aware three ways",
          all(s in html for s in ("@media (prefers-color-scheme:dark)",
                                  ':root[data-theme="dark"]',
                                  ':root[data-theme="light"]',
                                  ':root:not([data-theme="light"])')))
    check("wide content scrolls in its own container",
          "overflow-x:auto" in html and "overflow-x:hidden" in html)

    # 5b. the chart's payload, checked against the ledger before a browser sees it
    island = re.search(r'<script id="ledger-data"[^>]*>(.*?)</script>', html, re.S)
    payload = json.loads(island.group(1)) if island else {}
    chart = payload.get("chart") or {}
    cs = chart.get("series") or []
    check("the page carries a chart payload", bool(cs),
          f"{len(cs)} series over {len(chart.get('runs') or [])} runs")
    check("no charted series has fewer than two observations",
          all(sum(1 for x in s["v"] if x is not None) >= 2 for s in cs))
    firsts = [next((x for x in s["v"] if x is not None), None) for s in cs]
    check("no charted series is indexed against a zero first observation",
          all(f not in (0, None) for f in firsts),
          "a ratio to zero has no value; such a series must be excluded, not drawn")
    emph = [k for k in (chart.get("emph") or []) if any(s["k"] == k for s in cs)]
    check("the emphasised series exist and are the only coloured ones",
          len(emph) == sum(1 for s in cs if s["e"]) and len(emph) <= 3,
          f"{len(emph)} of {len(cs)} series take a categorical slot; "
          "the rest are neutral because hues are never cycled past the palette")
    check("every emphasised series is named on the page outside the data island",
          all(present(k.rsplit(" · ", 1)[0], html) for k in emph))
    flat = [s for s in cs if s["f"]]
    check("series that never changed are kept, not dropped", bool(flat),
          f"{len(flat)} flat series retained (de-emphasised, not deleted)")

    # 6/7. render it, drive it, look at it
    if shots:
        chrome = next((c for c in ("google-chrome", "google-chrome-stable",
                                   "chromium", "chromium-browser")
                       if sh("which", c).returncode == 0), None)
        p(f"chrome: {chrome}")
        if not chrome:
            p("[SKIP] no chrome binary — the page was NOT rendered or driven")
        else:
            page = PERF / ".probe.html"
            probe_key = emph[0] if emph else (cs[0]["k"] if cs else "")
            page.write_text(html.replace(
                "</body>", PROBE.replace("__PROBE_KEY__", probe_key) + "</body>"))
            # Wide on purpose: Tabulator renders columns virtually too, so a
            # narrow window would leave the metric columns — the ones whose
            # nulls carry the interesting reasons — unbuilt and unchecked.
            dom = sh(chrome, "--headless", "--disable-gpu", "--no-sandbox",
                     "--window-size=2600,1400", "--virtual-time-budget=20000",
                     "--dump-dom", str(page)).stdout
            page.unlink(missing_ok=True)
            m = re.search(r'<div id="vyr-probe">(.*?)</div>', dom, re.S)
            got = json.loads(m.group(1)) if m else {}
            p("probe: " + json.dumps({k: v for k, v in got.items()
                                      if k not in ("sorted", "chart")}))
            ch = got.get("chart") or {}
            p("chart: " + json.dumps({k: v for k, v in ch.items() if k != "tip_text"}))
            built = got.get("rows") or {}
            want_rows = {"t-cells": len(cells), "t-runs": len(rows)}
            check("every table built its rows in a real browser",
                  all(built.get(k) == v for k, v in want_rows.items())
                  and built.get("t-values", 0) > 0,
                  f"built {built}, expected {want_rows} + a non-empty t-values")
            vals = [v for v in (got.get("sorted") or []) if isinstance(v, (int, float))]
            allv = sorted((c["metrics"]["insns_per_frame_total"] for _, c in cells
                           if isinstance((c.get("metrics") or {}).get(
                               "insns_per_frame_total"), (int, float))), reverse=True)
            check("a numeric column sorts numerically, not lexically",
                  bool(vals) and vals == sorted(vals, reverse=True) and vals[0] == allv[0],
                  f"top of column {vals[:3]} vs top of data {allv[:3]}")
            titles = set(got.get("null_titles") or [])
            recorded = sorted(titles & notes)
            check("rendered null cells carry the reason the ledger recorded",
                  bool(recorded),
                  f"{got.get('null_cells', 0)} null cells rendered, "
                  f"{len(recorded)} distinct recorded reason(s) on screen, e.g. "
                  f"\"{(recorded or ['-'])[0][:70]}\"")

            # the chart is a canvas built in JS: only a browser can say it drew
            check("the chart built every series in a real browser",
                  ch.get("series") == len(cs) and ch.get("points") == len(chart["runs"])
                  and not ch.get("dead"),
                  f"{ch.get('series')} series x {ch.get('points')} runs "
                  f"(expected {len(cs)} x {len(chart.get('runs') or [])}), "
                  f"error={ch.get('error')}")
            check("the chart actually painted pixels", (ch.get("inked_pct") or 0) > 1.0,
                  f"{ch.get('inked_pct')}% of {ch.get('canvas_px')} canvas px inked "
                  f"at {ch.get('size')} — a configured-but-blank canvas is the "
                  "failure this catches")
            check("every series starts at exactly index 100",
                  ch.get("first_not_100") == 0,
                  f"{ch.get('first_not_100')} series whose first observation is not 100")
            check("holes in the data are holes in the lines",
                  (ch.get("interior_gaps") or 0) > 0 and (ch.get("nulls") or 0) > 0,
                  f"{ch.get('nulls')} missing observations, of which "
                  f"{ch.get('interior_gaps')} sit between two present ones and must "
                  f"break the line; {ch.get('isolated_points')} lone observations "
                  "drawn as points")
            tt = ch.get("tip_text") or ""
            check("a real mouse move names one series and dims the rest",
                  ch.get("tip_on") == "1" and ch.get("dot_on") == "1"
                  and probe_key in tt and (ch.get("dimmed") or 0) > len(cs) // 2,
                  f"pointer at {ch.get('hover_at')} -> run {ch.get('cursor_idx')}, "
                  f"readout \"{tt[:88]}\", {ch.get('dimmed')} of {ch.get('series')} "
                  "series dimmed")
            marks = ch.get("keys") or []
            unpainted = [m for m in marks
                         if m[0] < 8 or m[1] < 1 or "rgba(0, 0, 0, 0)" in m[2]]
            check("every series key is a stroke that actually paints",
                  len(marks) >= 6 and not unpainted,
                  f"{len(marks)} keys measured in the legend and the readout, "
                  f"{len(unpainted)} painting nothing {unpainted[:3]}")

            magick = next((mm for mm in ("magick", "convert")
                           if sh("which", mm).returncode == 0), None)
            means = {}
            for theme in ("light", "dark"):
                # Seed the toggle's own storage read: headless Chrome reports
                # prefers-color-scheme dark, and the page script strips a
                # hand-stamped data-theme on load (as it should).
                page = PERF / f".render-{theme}.html"
                page.write_text(html.replace(
                    "localStorage.getItem('vyr-ledger-theme')", f"'{theme}'"))
                png = TMP / f"ledger-page-{theme}.png"
                sh(chrome, "--headless", "--disable-gpu", "--no-sandbox",
                   "--hide-scrollbars", f"--window-size={SHOT_W},{SHOT_H}",
                   "--virtual-time-budget=20000", f"--screenshot={png}", str(page))
                page.unlink(missing_ok=True)
                mean = -1.0
                if magick and png.exists():
                    g = sh(magick, str(png), "-resize", "1x1!", "-format",
                           "%[fx:mean]", "info:").stdout.strip()
                    try:
                        mean = float(g)
                    except ValueError:
                        pass
                means[theme] = mean
                p(f"  {theme:5s} -> {png.name} "
                  f"{png.stat().st_size if png.exists() else 0:>9} B  mean={mean:.4f}")
            check("light theme renders light", means.get("light", 0) > 0.70,
                  f"mean {means.get('light', -1):.4f}")
            check("dark theme renders dark", 0 <= means.get("dark", 1) < 0.30,
                  f"mean {means.get('dark', -1):.4f}")
            p("  LOOK AT THEM — a generator cannot see a table that renders wrong:")
            for t in ("light", "dark"):
                p(f"    code-insiders {TMP / f'ledger-page-{t}.png'}")

    p("ALL CHECKS PASSED" if FAIL == 0 else f"{FAIL} CHECK(S) FAILED")
    TMP.mkdir(exist_ok=True)
    (TMP / "ledger-verify.log").write_text("\n".join(OUT) + "\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

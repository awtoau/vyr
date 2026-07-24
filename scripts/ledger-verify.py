#!/usr/bin/env python3
"""Verify the generated measurement-ledger page — and RENDER it, in a browser.

``scripts/ledger.py`` writes ``docs/perf/index.html``: three flat tables built
in JavaScript by a vendored Tabulator. That is the whole reason this script
exists. A generator can emit a perfectly well-formed page whose tables never
appear — an empty ``<div>`` reads identically to a full one in the HTML source —
so the only way to know the page works is to open it in a real browser, count
the rows it actually built, drive a sort, and look at the picture.

What it checks:

  1. ``history.jsonl`` is untouched — a page regeneration must never be able to
     move a measured number.
  2. every measured value, every ``null``'s written reason and every
     ``fold_provenance`` value in EVERY matrix row of the ledger appears in the
     page. The page is now the whole history rather than its latest row, so
     this check covers the whole history too.
  3. regeneration is deterministic (same input, byte-identical output).
  4. the page fetches nothing at render time: no asset is loaded from a URL, and
     every relative reference resolves to a file on disk. The vendored Tabulator
     and its MIT licence text are present.
  5. theme-aware three ways (light root, OS media query, both ``data-theme``
     scopes), and wide content scrolls in its own container.
  6. it actually renders and actually works: headless Chrome builds the tables
     and a probe reports each table's row count, re-sorts a numeric column and
     returns the order it produced, and reads back the tooltip on a null cell.
     The sort must be NUMERIC (9,220,422 below 48,239,550 — which a lexical
     sort of formatted strings gets backwards) and the null must still carry
     the reason the harness recorded for it.
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
# The page is three fixed-height tables, so unlike the chart wall it replaced it
# does not grow as the ledger grows.
SHOT_W, SHOT_H = 1600, 2400

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
    out(res);
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

    # 1. the data is not ours to change
    d = sh("git", "diff", "--stat", "--", "docs/perf/history.jsonl")
    check("history.jsonl unmodified", d.stdout.strip() == "",
          d.stdout.strip() or "clean")

    rows = [json.loads(ln) for ln in (PERF / "history.jsonl").read_text().splitlines()
            if ln.strip()]
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
    want = ("tabulator.min.js", "tabulator.min.css", "LICENSE.tabulator.txt")
    absent = [f for f in want if not (vendor / f).exists()]
    check("Tabulator is vendored, with its licence", not absent,
          ", ".join(absent) or
          f"{sum((vendor / f).stat().st_size for f in want) // 1024} KiB in docs/perf/vendor")
    check("the vendored licence is permissive (MIT)",
          not absent and "MIT License" in (vendor / want[2]).read_text())

    # 5. theme-aware three ways, no page-level h-scroll
    check("theme-aware three ways",
          all(s in html for s in ("@media (prefers-color-scheme:dark)",
                                  ':root[data-theme="dark"]',
                                  ':root[data-theme="light"]',
                                  ':root:not([data-theme="light"])')))
    check("wide content scrolls in its own container",
          "overflow-x:auto" in html and "overflow-x:hidden" in html)

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
            page.write_text(html.replace("</body>", PROBE + "</body>"))
            # Wide on purpose: Tabulator renders columns virtually too, so a
            # narrow window would leave the metric columns — the ones whose
            # nulls carry the interesting reasons — unbuilt and unchecked.
            dom = sh(chrome, "--headless", "--disable-gpu", "--no-sandbox",
                     "--window-size=2600,1400", "--virtual-time-budget=20000",
                     "--dump-dom", str(page)).stdout
            page.unlink(missing_ok=True)
            m = re.search(r'<div id="vyr-probe">(.*?)</div>', dom, re.S)
            got = json.loads(m.group(1)) if m else {}
            p("probe: " + json.dumps({k: v for k, v in got.items() if k != "sorted"}))
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

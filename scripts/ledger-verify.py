#!/usr/bin/env python3
"""Verify the generated measurement-ledger page — and RENDER it, in both themes.

``scripts/ledger.py`` writes ``docs/perf/index.html`` and a pile of hand-rolled
SVG charts. Hand-rolled SVG fails **silently**: a single ``.sN { fill: … }``
rule once overrode ``fill="none"`` on every polyline and filled each line down
to its closing edge — invisible while a series had three near-horizontal
points, glaring at thirty-one. Nothing in the generator could have caught that.
Only looking at the page could, so this script exists to make looking cheap and
repeatable.

What it checks:

  1. ``history.jsonl`` is untouched — a page regeneration must never be able to
     move a measured number.
  2. every measured value, every ``null`` cell's written reason and every
     ``fold_provenance`` value in the latest matrix row appears on the page.
  3. regeneration is deterministic (same input, byte-identical output).
  4. no ``fill`` declaration can reach a polyline; every polyline still carries
     ``fill="none"``.
  5. the page is self-contained (no CDN, no external CSS/JS/font/image) and
     theme-aware three ways (light root, OS media query, both ``data-theme``
     scopes).
  6. it actually renders: full-page screenshots in light and dark, with the
     mean luminance of each asserted, so a theme that silently failed to apply
     is caught rather than assumed. Headless Chrome reports
     ``prefers-color-scheme: dark``, so the light shot is taken by seeding the
     page's own theme toggle — which exercises the real script rather than a
     stamped attribute the script would (correctly) strip on load.

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
# 15000 px: the whole page fits well inside this today (~11 k) and Chrome
# renders the full window height in one shot, so nothing is cut off.
SHOT_W, SHOT_H = 1400, 15000


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
    mrow = next(r for r in reversed(meas) if r.get("matrix"))
    html = (PERF / "index.html").read_text()

    # 2. every measured value survives the redesign
    missing, n = [], 0
    for c in mrow["matrix"]["cells"]:
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
                missing.append(f"{c['platform']}/{c['tier']}/{c.get('opt_level')} {k}={v}")
        fh = (c.get("metrics") or {}).get("frame_hash")
        if fh:
            n += 1
            if fh not in html:
                missing.append(f"{c['platform']}/{c['tier']} frame_hash={fh}")
    check(f"all {n} measured values present verbatim in the page", not missing,
          "; ".join(missing[:6]))

    reasons = {c.get("reason") for c in mrow["matrix"]["cells"]
               if c.get("status") != "measured" and c.get("reason")}
    lost = [r for r in reasons if _esc(r) not in html]
    check(f"all {len(reasons)} null-cell reason(s) surfaced on the page", not lost,
          "; ".join(lost)[:200])

    provs = {(c.get("fold_provenance") or "").split(" (")[0].strip()
             for c in mrow["matrix"]["cells"]} - {""}
    gone = [q for q in provs if q not in html]
    check(f"all {len(provs)} fold_provenance value(s) named on the page", not gone,
          "; ".join(gone))

    # 3. determinism
    r = sh("python3", "scripts/ledger.py", "--regen-only")
    check("regeneration is deterministic",
          r.returncode == 0 and (PERF / "index.html").read_text() == html,
          f"rc={r.returncode}")

    # 4. the silent-SVG failure mode, structurally
    check("no fill declaration can reach a polyline (.sN sets stroke only)",
          not re.search(r"\.s\d\s*\{[^}]*fill:", html))
    npoly = html.count("<polyline")
    check('every polyline carries fill="none"',
          npoly == len(re.findall(r'<polyline[^>]*fill="none"', html)),
          f"{npoly} polylines")

    # 5. self-contained, theme-aware three ways, no page-level h-scroll
    check("theme-aware three ways",
          all(s in html for s in ("@media (prefers-color-scheme:dark)",
                                  ':root[data-theme="dark"]',
                                  ':root[data-theme="light"]',
                                  ':root:not([data-theme="light"])')))
    check("wide content scrolls in its own container",
          "overflow-x:auto" in html and "overflow-x:hidden" in html)
    check("self-contained: no external asset reference",
          not re.search(r'(?:src|href)="https?://(?!github\.com)', html)
          and "<script src" not in html and "<link" not in html)

    # 6. render it and look at it
    if shots:
        chrome = next((c for c in ("google-chrome", "google-chrome-stable",
                                   "chromium", "chromium-browser")
                       if sh("which", c).returncode == 0), None)
        p(f"chrome: {chrome}")
        if not chrome:
            p("[SKIP] no chrome binary — screenshots not taken")
        else:
            magick = next((m for m in ("magick", "convert")
                           if sh("which", m).returncode == 0), None)
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
                   "--virtual-time-budget=3000", f"--screenshot={png}", str(page))
                page.unlink(missing_ok=True)
                mean = -1.0
                if magick and png.exists():
                    got = sh(magick, str(png), "-resize", "1x1!", "-format",
                             "%[fx:mean]", "info:").stdout.strip()
                    try:
                        mean = float(got)
                    except ValueError:
                        pass
                means[theme] = mean
                p(f"  {theme:5s} -> {png.name} "
                  f"{png.stat().st_size if png.exists() else 0:>9} B  mean={mean:.4f}")
            check("light theme renders light", means.get("light", 0) > 0.70,
                  f"mean {means.get('light', -1):.4f}")
            check("dark theme renders dark", 0 <= means.get("dark", 1) < 0.30,
                  f"mean {means.get('dark', -1):.4f}")
            p("  LOOK AT THEM — a generator cannot see a chart that renders wrong:")
            for t in ("light", "dark"):
                p(f"    code-insiders {TMP / f'ledger-page-{t}.png'}")

    p("ALL CHECKS PASSED" if FAIL == 0 else f"{FAIL} CHECK(S) FAILED")
    TMP.mkdir(exist_ok=True)
    (TMP / "ledger-verify.log").write_text("\n".join(OUT) + "\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

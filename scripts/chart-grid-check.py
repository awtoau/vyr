#!/usr/bin/env python3
"""Interactive check of the chart's per-series show/hide grid (#chart-grid).

The static-screenshot verifier cannot prove a TOGGLE works. This drives the real
page in Chromium: it reads the y-axis range with every series shown, clicks
"Hide outliers", and asserts the range actually shrank — a grid that renders but
does not drive u.setSeries would pass a screenshot and fail here.

Log: ./tmp/chart-grid-check.log
"""

import sys
from pathlib import Path

sys.path.insert(0, "/home/dan/.local/lib/python3.14/site-packages")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "docs/perf/index.html").as_uri()
LOG = ROOT / "tmp" / "chart-grid-check.log"
_lines = []


def log(m):
    print(m, flush=True)
    _lines.append(m)


def yrange(page):
    # uPlot exposes the live scale range; that is what "rescaled" means.
    return page.evaluate(
        "() => { const u = window.vyrLedgerChart;"
        " return u ? [u.scales.y.min, u.scales.y.max] : null; }")


def shown(page):
    return page.evaluate(
        "() => window.vyrLedgerChart.series.filter(s => s.show).length - 1")  # -x axis


def main():
    fails = []
    # The pip playwright wheel and the on-disk browser builds drift; pin to a
    # shell that actually exists rather than the version this wheel expects.
    shell = sorted(Path("/home/dan/.cache/ms-playwright").glob(
        "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=str(shell[-1]) if shell else None)
        pg = b.new_page(viewport={"width": 1400, "height": 1600})
        pg.goto(PAGE)
        pg.wait_for_function("() => window.vyrLedgerChart != null", timeout=15000)
        pg.wait_for_selector("#t-chart-grid .sg", timeout=15000)

        n_rows = pg.eval_on_selector_all("#t-chart-grid .sg", "els => els.length")
        r0 = yrange(pg)
        on0 = shown(pg)
        log(f"grid rows: {n_rows}   series shown: {on0}   y-range: "
            f"[{r0[0]:.1f}, {r0[1]:.1f}]")
        if n_rows < 2:
            fails.append(f"grid has {n_rows} rows")

        # 1. Hide outliers -> fewer shown, and the y-range must contract.
        pg.click("#t-chart-gout")
        pg.wait_for_timeout(200)
        r1 = yrange(pg)
        on1 = shown(pg)
        span0, span1 = r0[1] - r0[0], r1[1] - r1[0]
        log(f"after Hide outliers: shown {on1}, y-range [{r1[0]:.1f}, {r1[1]:.1f}] "
            f"(span {span0:.0f} -> {span1:.0f})")
        if on1 >= on0:
            fails.append(f"Hide outliers did not hide anything ({on0} -> {on1})")
        if span1 >= span0:
            fails.append(f"y-range did not contract ({span0:.0f} -> {span1:.0f}) "
                         "— the toggle is not driving the chart")

        # 2. Show all -> back to the original count and range.
        pg.click("#t-chart-gall")
        pg.wait_for_timeout(200)
        on2 = shown(pg)
        r2 = yrange(pg)
        log(f"after Show all: shown {on2}, y-range [{r2[0]:.1f}, {r2[1]:.1f}]")
        if on2 != on0:
            fails.append(f"Show all did not restore every series ({on0} -> {on2})")

        # 3. Hide all -> nothing shown.
        pg.click("#t-chart-gnone")
        pg.wait_for_timeout(150)
        on3 = shown(pg)
        log(f"after Hide all: shown {on3}")
        if on3 != 0:
            fails.append(f"Hide all left {on3} series shown")

        # 4. A single checkbox toggles exactly one series.
        pg.click("#t-chart-gall")
        pg.wait_for_timeout(150)
        base = shown(pg)
        pg.eval_on_selector("#t-chart-grid .sg input", "el => el.click()")
        pg.wait_for_timeout(150)
        one = shown(pg)
        log(f"single toggle: {base} -> {one}")
        if one != base - 1:
            fails.append(f"one checkbox changed {base - one} series, expected 1")

        # 5. Filter box narrows the visible rows.
        pg.fill("#t-chart-gsearch", "draft")
        pg.wait_for_timeout(150)
        vis = pg.eval_on_selector_all(
            "#t-chart-grid .sg",
            "els => els.filter(e => e.style.display !== 'none').length")
        log(f"filter 'draft': {vis} of {n_rows} rows visible")
        if not (0 < vis < n_rows):
            fails.append(f"filter 'draft' left {vis} of {n_rows} rows (expected some, not all)")

        b.close()

    if fails:
        for f in fails:
            log(f"[FAIL] {f}")
        log(f"{len(fails)} CHECK(S) FAILED")
    else:
        log("ALL CHART-GRID CHECKS PASSED")
    LOG.write_text("\n".join(_lines) + "\n")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

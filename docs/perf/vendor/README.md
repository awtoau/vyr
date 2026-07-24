# Vendored: Tabulator + uPlot

`docs/perf/index.html` is opened both from GitHub Pages and as a local `file://`
URL, and it must fetch **nothing** at render time. The libraries it uses are
therefore committed here and referenced by relative path — never from a CDN.

Both are **MIT**, which is permissive, so both satisfy `CLAUDE.md`'s "deps must
be permissive (MIT/BSD/Apache)" rule for a GPL-3.0-only + commercial repo. Each
licence was read from the package's own `LICENSE` inside the tarball and copied
here verbatim.

## Tabulator — the three sortable tables

| | |
|---|---|
| package | [`tabulator-tables`](https://tabulator.info/) |
| version | **6.5.2** |
| source | `https://registry.npmjs.org/tabulator-tables/-/tabulator-tables-6.5.2.tgz` |
| tarball sha256 | `f239696aa941d34041a0c6ae5813063e122efa1f2779cd177129d16facaaeefe` |
| licence | **MIT** — verified from the package's own `LICENSE`, copied here verbatim as [`LICENSE.tabulator.txt`](LICENSE.tabulator.txt) (© 2015-2025 Oli Folkerd) |
| vendored | 2026-07-24 |

Files, byte-identical to `dist/` in that tarball:

| file | sha256 |
|---|---|
| `tabulator.min.js` | `04802e757fa4189342c666d0f970a01d761c312798f31ffc664c24cbccc7ce3e` |
| `tabulator.min.css` | `b55e204b2f968cecc4d3663d37858093b31dd22d20f01d76f590726ee18f7e1f` |
| `LICENSE.tabulator.txt` | `191a2ee554684e1064c897b432f0e1bc6dfa714ca045d3f6ea2cf692cbd398b7` |

Notes:

* The JS ends with a `//# sourceMappingURL=tabulator.min.js.map` comment and the
  map is deliberately **not** vendored (3.4 MB, devtools-only). Nothing fetches
  it while rendering the page; a browser only looks for it if you open devtools
  with source maps enabled.
* `tabulator.min.css` references no fonts, images or `url(...)` of any kind, so
  the two files are the whole dependency.

## uPlot — the indexed-history line chart

| | |
|---|---|
| package | [`uplot`](https://github.com/leeoniya/uPlot) |
| version | **1.6.32** (latest on npm at the time of vendoring) |
| source | `https://registry.npmjs.org/uplot/-/uplot-1.6.32.tgz` |
| tarball sha256 | `4b8a8191e425658e3ea8c8c1314b0fc679c861ee3a1af2b20c7b16ba50b5133d` |
| licence | **MIT** — verified from the package's own `LICENSE`, copied here verbatim as [`LICENSE.uplot.txt`](LICENSE.uplot.txt) (© 2022 Leon Sorokin) |
| vendored | 2026-07-24 |

Files, byte-identical to `dist/` in that tarball:

| file | source in tarball | sha256 |
|---|---|---|
| `uplot.min.js` | `dist/uPlot.iife.min.js` | `19c8d4c6ad88929a79f4ae49d6f7161566dfd0ba3d15cc495e974f787eb78f1f` |
| `uplot.min.css` | `dist/uPlot.min.css` | `df630c6a8d6f8eeaff264b50f73ce5b114f646ffd9a0bb74f049b0a00135fa04` |
| `LICENSE.uplot.txt` | `LICENSE` | `8f989229699b4fe2f1a0432d0e9edc338a8a911e250e2d1b01ecd770a5f5b1bd` |

Why a library rather than more hand-rolled SVG: the 13 charts this page used to
draw by hand shipped **three separate silent CSS bugs** (a `fill` rule that
overrode `fill="none"` and filled every polyline; a dark-mode block that never
applied because an equal-specificity light rule followed it; a `[data-theme]`
scope that would have out-ranked the `fill:none` defence). All three were
invisible in the markup. The one chart that replaced them plots ~130 series with
gaps, per-series hover focus and box zoom — an amount of geometry and
interaction that is exactly where hand-rolling stops being simpler. 50 KiB of
reviewed, widely-used MIT canvas code is the cheaper correctness.

Notes:

* `uPlot.iife.min.js` is the plain-`<script>` build: it defines a global
  `uPlot` and needs no module loader, so the page works over `file://`.
* It carries **no** `sourceMappingURL` comment, so nothing is looked for.
* `uPlot.min.css` references no fonts, images or `url(...)` of any kind.
* `dist/uPlot.d.ts`, the CJS/ESM builds and the un-minified IIFE are not
  vendored — the page needs one script and one stylesheet.

## Both

The page ships its own colour overrides (`scripts/ledger.py`) so the tables and
the chart follow the page's light/dark theme; both vendored stylesheets are
unmodified. The chart reads its colours from the page's CSS custom properties at
draw time, so a theme change repaints the canvas rather than reloading it.

To update either: download the tarball, re-verify `LICENSE`, copy the `dist/`
files here, update the version and hashes above, and re-run
`scripts/ledger-verify.py`.

# Vendored: Tabulator

`docs/perf/index.html` is opened both from GitHub Pages and as a local `file://`
URL, and it must fetch **nothing** at render time. The table library it uses is
therefore committed here and referenced by relative path — never from a CDN.

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

MIT is permissive, so this satisfies `CLAUDE.md`'s "deps must be permissive
(MIT/BSD/Apache)" rule for a GPL-3.0-only + commercial repo.

Notes:

* The JS ends with a `//# sourceMappingURL=tabulator.min.js.map` comment and the
  map is deliberately **not** vendored (3.4 MB, devtools-only). Nothing fetches
  it while rendering the page; a browser only looks for it if you open devtools
  with source maps enabled.
* `tabulator.min.css` references no fonts, images or `url(...)` of any kind, so
  the two files are the whole dependency.
* The page ships its own colour overrides (`scripts/ledger.py`) so the table
  follows the page's light/dark theme; the vendored CSS is unmodified.

To update: download the tarball, re-verify `LICENSE`, copy `dist/js/tabulator.min.js`
and `dist/css/tabulator.min.css` here, and update the version and hashes above.

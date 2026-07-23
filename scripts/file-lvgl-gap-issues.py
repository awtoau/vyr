#!/usr/bin/env python3
"""file-lvgl-gap-issues.py — file the findings of docs/measurements/lvgl-gap.md
as issues on awtoau/vyr (public repo: repo-relative paths only, no local
filesystem paths in any body).

Usage: python3 scripts/file-lvgl-gap-issues.py [--dry-run]
Log:   tmp/file-lvgl-gap-issues.log
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOG = REPO / "tmp" / "file-lvgl-gap-issues.log"
GH_REPO = "awtoau/vyr"

ISSUES = [
    (
        "perf: the M4 vyr-vs-LVGL comparison counts the benchmark's own FNV hash as render cost",
        """Measured 2026-07-23, plugin QEMU (`netduinoplus2`), exact instruction counts.
Full analysis: `docs/measurements/lvgl-gap.md` §0.

Both M4 vehicles fold every output byte of every band into an FNV-1a hash so the
frame is provably materialised. That is 388,800 bytes/frame on both sides and it
is **not rendering**:

| | fold, insns/frame | how measured |
|---|--:|---|
| vyr (`workload::render_frame_banded`, inlined) | **3,110,434** | `scripts/harness-overhead.py` — build with the fold over an empty slice, diff. Identical to 34 insns across Exact and Draft; 8.00 insns/byte |
| LVGL (`flush_cb`, own symbol) | **3,888,561** | `scripts/m4-attribute.py` per-symbol attribution; 10.0 insns/byte |

The fold is **54.7 % of LVGL's published frame and only 36.2 % of Draft's**, so
subtracting it changes the headline:

| firmware | published | fold | render only | insn/px | vs LVGL |
|---|--:|--:|--:|--:|--:|
| vyr Exact | 64,422,179 | 3,110,434 | 61,311,745 | 473.1 | **19.0x** |
| vyr Fast | 49,585,035 | 3,110,434 | 46,474,601 | 358.6 | 14.4x |
| vyr Draft | 8,604,184 | 3,110,434 | 5,493,716 | 42.4 | **1.70x** |
| LVGL 9.6.0-dev | 7,112,541 | 3,888,561 | 3,223,980 | 24.9 | 1.00 |

**"vyr Draft costs 4.6 % fewer instructions than LVGL" is an artefact of the
shared harness.** Renderer against renderer, Draft costs 70 % *more* while doing
less (no AA, square corners); Exact is 19.0x, not 9.06x.

Second, independent problem in the same table: the `insn/px` column mixes two
normalisers. LVGL's is `insns / 129,600` (delivered pixels); vyr's is
`insns / pixels_written`, the **overdraw-inclusive touched-pixel** counter
(Exact 212,272, Draft 182,216 — printed by every run). Per delivered pixel vyr
Exact is 497.1, not 291.9, and Draft is 66.4, not 52.4.

**Actions**
- [ ] report render-only figures (published minus the measured fold) alongside
      the raw ones, or subtract the fold in the harness window
- [ ] normalise every `insn/px` by delivered pixels (129,600), or label the
      touched-pixel column as a different metric
- [ ] update `docs/performance.md` §3 and `scripts/lvgl-m4-bench/compare.md`
""",
    ),
    (
        "perf(core): curve flattening re-runs libm f32 trig — computed in software f64 on M4F — per band, per frame (~11.4 M insns/frame)",
        """Measured 2026-07-23; full analysis `docs/measurements/lvgl-gap.md` §2.2.
The single largest *identified* item in the Exact frame.

Per-symbol attribution of the M4 frame (hotblocks + ELF symbols,
`scripts/m4-attribute.py`) with entry-block call counts:

| symbol | calls/frame | insns/frame | insns/call |
|---|--:|--:|--:|
| `libm::cosf` | 4,976 | 130,482 | 26.2 |
| `libm::sinf` | 4,976 | 129,848 | 26.1 |
| `libm::k_sinf` | 4,840 | 246,840 | 51.0 |
| `libm::k_cosf` | 4,840 | 232,320 | 48.0 |
| `__muldf3` | 72,816 | 5,534,687 | 76.0 |
| `__adddf3` | 48,888 | 5,106,293 | 104.4 |

72,816/9,952 = **7.3 software double multiplies per trig call** (and 4.9 adds):
`libm`'s f32 kernels evaluate in `f64`, and the M4F FPU is single-precision, so
every one is a compiler-builtins call. **Each `cosf` costs ~1,145 M4
instructions.** The chain totals **≈ 11.4 M insns/frame = 17.7 % of Exact,
22.7 % of Fast — 3.5x LVGL's entire render (3.22 M).**

It is the flattening path and nothing else:
- Draft executes **zero** trig and **zero** soft-f64 (its arcs are integer,
  `vyr-core/src/painter.rs` `isqrt_i64`);
- Fast pays the same bill as Exact (`__muldf3` 5,528,883 vs 5,534,687) because
  it builds the same curve contours.

And it is *repeated work*: `circle_points` / `rrect_points`
(`vyr-core/src/painter.rs`) are **pure functions of (centre, radius)** and are
re-evaluated for every band a shape touches (17 bands here), every frame.

For contrast, LVGL computes its circle coverage profile **once per radius, in
integers** (an eighth of a Bresenham circle upscaled 4x) and caches it globally
by radius; its entire AA bill is 175 k insns/frame.

**Proposal (invariant-safe): memoise the flattened contours** for the life of a
`Request`. A pure memo returns the identical `f32` values, so the polygons, the
1/64-px quantisation and the exact-integer band translation are unchanged and
the goldens must not move — that is the acceptance test. Recovers ≈ 10.7 M
insns/frame at Exact and ≈ 10.6 M at Fast.

Implementation note: the cache has to outlive the per-band canvas, so it needs
threading through `render_with_quality` beside `Fonts`/`Assets`. `vyr-core` is
`no_std` + alloc and `forbid(unsafe_code)`, so a hidden global is not an option.

**Rejected alternative:** an f32-only `sinf`/`cosf`. Same win, but different
values ⇒ different polygons ⇒ a re-bless of every golden. Only if the memo is
impossible.
""",
    ),
    (
        "perf: release-mcu builds opt-level=\"z\" (-Oz) but the LVGL anchor compiles -Os — 29–43 % of the frame",
        """Measured 2026-07-23; `docs/measurements/lvgl-gap.md` §0.3.

`release-mcu` sets `opt-level="z"`, which is `-Oz`: on top of size-first
codegen it gives up inlining and enables the machine outliner. That is far more
expensive for tiny-skia's generic, SIMD-shim-heavy Rust than for LVGL's flat C —
`OUTLINED_FUNCTION_*` stubs alone are 5.3 M insns/frame (8.2 %) at Exact, and
tiny-skia's non-inlined `u16x16` wrappers another 12.3 M (19 %).

Same ELF pipeline, same scene, `scripts/m4-attribute.py --sweep`:

| tier | `z` (published) | `s` | `3` |
|---|--:|--:|--:|
| Exact | 64,422,179 | 45,561,610 (−29 %) | 36,971,836 (−43 %) |
| Fast | 49,585,035 | 35,014,927 (−29 %) | 29,273,858 (−41 %) |
| Draft | 8,604,184 | 7,283,781 (−15 %) | 6,694,736 (−22 %) |
| .text+.rodata | 478,773 B | 676,865 B | 706,427 B |

C's `-Os` corresponds to Rust's `opt-level="s"`, not `"z"`. The flash price is
real (+198 KB at `s`), so `z` may well stay the default — but a `z` number must
not be published against a `-Os` number as apples-to-apples.

**Actions**
- [ ] publish an `opt-level="s"` row next to the `z` row in
      `docs/performance.md` §3, or state the flag asymmetry in the caveat
- [ ] decide whether the MCU perf profile and the MCU *size* profile should be
      the same profile at all
- [ ] confirm the frame hash is identical at every opt-level (it is expected to
      be; the sweep runs already print it)
""",
    ),
    (
        "perf(core): IR attributes are re-parsed from strings on every band — 23 % of Draft's render cost",
        """Measured 2026-07-23; `docs/measurements/lvgl-gap.md` §2.3.

Per-symbol M4 attribution, per frame (17 bands), at Draft:

| symbol | insns/frame |
|---|--:|
| `memcmp` (attribute-name comparison) | 478,279 |
| `vyr_core::ir::Node::str_attr` | 160,775 |
| `alloc::collections::btree::map::BTreeMap::get` | 96,495 |
| `ir::walk` | 94,059 |
| `<f32 as FromStr>::from_str` | 92,776 |
| `core::str::trim_matches` | 44,242 |
| **total** | **≈ 1,271,353** |

That is 2.0 % of the Exact frame but **23.1 % of Draft's render-only cost**
(5,493,716 insns/frame) — and Draft is the tier that matters on an MCU. The work
is repeated per band: the paint walk resolves attribute *names by string compare*
and re-parses values on each of the 17 bands.

LVGL's equivalent — its style-property cascade lookup — costs 458 k insns/frame
in the same measurement, and it is doing a genuine cascade across parts and
states.

**Proposal:** resolve each node's attributes once per `Request` into a typed
representation, and have the per-band paint walk read that. Same values ⇒ same
pixels ⇒ no re-bless. Care needed so that I6 honest-failure semantics stay
band-independent: validation must still reject a broken widget identically
whether or not its band is culled.
""",
    ),
    (
        "perf(core): Draft allocates and zeroes the tiny-skia scratch pixmap for every band although it never uses it",
        """Measured 2026-07-23; `docs/measurements/lvgl-gap.md` §2.3.

`TinySkiaCanvas::new_with_quality` allocates the tiny-skia `Pixmap`
unconditionally, including for `Quality::Draft`, whose paint path never touches
tiny-skia — the pixmap is only read by the rare rounded-clip fallback and by the
clip A8 mask.

Cost at Draft: 480x16x4 = 30,720 B allocated **and zeroed** per band, 17 bands =
**522 KB of zeroing per frame**. `__aeabi_memclr` measures 681,675 insns/frame
at Draft, roughly 8–12 % of Draft's render-only cost (5,493,716), plus the
allocator churn (`alloc` 163,161 + `dealloc` 109,910 insns/frame).

**Proposal:** allocate the scratch pixmap lazily on first use. It is scratch —
nothing reads it before it is written — so this cannot change any pixel; the
Draft goldens are the check. It also takes 30,720 B off every Draft band, which
matters directly for the F405-class heap budget.
""",
    ),
    (
        "perf(core): the Fast tier's scratch seed/demul round-trip is area-scaled for discs",
        """Measured 2026-07-23; `docs/measurements/lvgl-gap.md` §4.

`scripts/disc-scaling.py` renders one disc of radius r on a fixed 320x320
canvas under callgrind and subtracts the empty-canvas run; the log-log slope of
the marginal cost says whether a path is perimeter- or area-scaled.

| tier | r=8 | r=16 | r=32 | r=64 | slope | doubling ratios |
|---|--:|--:|--:|--:|--:|---|
| Exact | 98,366 | 179,090 | 333,464 | 656,734 | 0.91 | 1.82x, 1.86x, 1.97x |
| **Fast** | 164,670 | 319,990 | 723,625 | 1,919,213 | **1.18** | 1.94x, 2.26x, **2.65x** |
| Draft | 21,789 | 32,300 | 54,333 | 103,936 | 0.75 | — |

Exact is perimeter-scaled: tiny-skia already coalesces full-coverage interior
runs, so it is **not** paying per-pixel coverage over solid interiors (a
hypothesis this test was written to falsify, and did).

Fast drifts toward area because `fill_into`'s seed/demultiply round-trip is
per pixel of the `carry` region, and `disc()` passes the whole bbox. The ring
already trims its hole with `frame_strips`; the disc trims nothing.

**Proposal:** derive the disc's carry from its own geometry (e.g. per-row spans,
or an inscribed-square split like the ring's) so the round-trip covers only rows
the path can ink. **Risk to state plainly:** under-covering `carry` silently
drops AA fringe — the carry must be derived from the shape with an outward
margin, exactly as `frame_strips` is, and the band-equivalence goldens are the
gate.
""",
    ),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    lines = []
    created = []
    for title, body in ISSUES:
        for bad in ("/mnt/", "/home/", "git_mirror", "git_debris"):
            if bad in title or bad in body:
                print(f"REFUSING: local path {bad!r} in issue {title!r}")
                return 2
        if a.dry_run:
            print(f"--- would file: {title}\n{body[:400]}\n")
            continue
        r = subprocess.run(["gh", "issue", "create", "--repo", GH_REPO,
                            "--title", title, "--body", body],
                           capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip()
        lines.append(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {title}\n    {out}")
        print(out)
        if r.returncode != 0:
            LOG.write_text("\n".join(lines) + "\n")
            return 1
        created.append(out.splitlines()[-1])
    LOG.write_text("\n".join(lines) + "\n")
    print(json.dumps(created, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# The painter's cost on a SIMD-less core — measurement plan for #37

> **Status: a harness and a plan, not a decision and not an implementation.**
> #37 says explicitly *"this is a design issue, not a task. Do not start
> implementing."* Nothing in this branch changes the painter, the tiers, the
> goldens or any published number. It adds the instruments the decision needs,
> plus first-light readings from them.

## 1. What #37 actually has to decide, and why hand-measurement cannot

The issue records ~21 M insns/frame attributed to tiny-skia's `u16x16` shims
plus their `memcpy` traffic — about a third of `Quality::Exact` — on a core
with no SIMD. Its three acceptance items are:

1. how much of the ~21 M **each tier** actually pays;
2. a written position on whether vyr accepts the cost on MCU targets;
3. for any option chosen, what happens to band equivalence, the golden
   hashes, and the three-ISA determinism proof.

The two issue comments then move the ground: re-blessing goldens is *allowed*
(what must survive is cross-ISA **agreement**, not a particular hash), and the
"structural to the painter choice" framing is challenged — tiny-skia already
dispatches between a 16-lane `lowp` and an 8-lane `highp` pipeline, so a
narrow pipeline is an extension of an existing seam, and the real cost may be
**stage-boundary spill** (~256 B of live `u16x16` state on a core with 8
usable registers) rather than wasted lanes.

Spill and lane-waste predict *different measurements*, and both predict a
different one again from "the cost is per draw, not per pixel". None of the
three can be told apart by reading a whole-frame profile, because a frame
mixes every effect together. So the first deliverable is not an opinion — it
is an instrument that can separate them, and can be re-run against any
candidate change.

## 2. The harness

Four pieces, all scripted, all re-runnable, none of them a gate.

### 2.1 `vyr-size --features probe` + `scripts/painter-probe.py`

A sweep of synthetic scenes in which **draws, 16-px pipeline chunks and
painted pixels vary independently**, rendered through the exact same banded
path as the published fixture (`workload::render_frame_banded` — the probe
calls it, it does not reimplement it). Each case is bracketed by a semihosting
`bkpt`, so `libinsn,match=bkpt,trace=on` prices it exactly, the same vehicle
and the same rule as every other published M4 number.

The script fits

    insns(case) = null + a·draws + b·chunks + c·pixels

where `null` is measured (an empty frame through the same band loop) rather
than fitted, and reports the coefficients with residuals. `a` is per-`fill_path`
setup, `b` is per-16-px-chunk pipeline structure — the coefficient a narrower
pipeline would move — and `c` is irreducible per-pixel arithmetic.

**The decisive family is `b15 … b33`**: fixed draw count, width alone moving
across a multiple of 16. `b16 → b17` is +6 % pixels but +100 % chunks. If the
pipeline charges by the chunk, that is a cliff; if it charges by the pixel, it
is a smooth line. There is no third answer.

    ./dev.py probe --tiers exact,fast,draft            # the sweep
    ./dev.py probe --tiers exact --attribute           # + per-symbol profile
    ./dev.py probe --tiers exact --band-h 8,16,32      # the working-set axis
    ./dev.py probe --tiers exact --host                # same cases on x86-64

`--host` dumps the *same* case definitions (`vyr-size --dump-probe-scenes`)
and prices them with `vyr-cli` under callgrind, so "the vectorised shape costs
more without SIMD" can be stated as a per-coefficient ratio instead of an
assertion.

### 2.2 `scripts/insn-mix.py` (+ `scripts/insn_static.py`)

Exact **instruction-class × symbol** attribution: a hotblocks log gives every
translation block as `(pc, insns, exec count)`, a static disassembly gives the
mnemonic at every address, so walking each block and weighting by its exec
count reproduces the run's instruction mix exactly. The reconstruction is
self-checking — it must equal the plugin's own total (measured: 100.0000 %),
and the script refuses to call a number publishable when it does not.

This is what separates spill from lane waste: **spill is loads and stores**.
It also produces an exact **call census** (a `bl` executes as often as the
block containing it), which closes two of the three gaps
`docs/measurements/lvgl-gap.md` §8 admits to.

    ./dev.py insn-mix --tiers exact,fast,draft   # reuses m4-attribute's logs

### 2.3 The band-height axis

`BAND_H` is now `option_env!("VYR_BAND_H")`-overridable (default 16,
`build.rs` tracks the variable, every report line prints the value in force).
lvgl-gap §8 lists band-count sensitivity as **not measured** precisely because
it was a hard-coded constant. It is also the working-set knob — see §4.

### 2.4 What is deliberately NOT here

* **No painter change**, no vendored tiny-skia, no `[patch]`, no cfg-gated
  paths. The issue asks for a decision first.
* **No new gate.** The probe is diagnostic. Nothing in `./dev.py ci` changes.
* **No board leg yet.** See §4: it is required before any conclusion about
  *cache* behaviour, and it is the one rung this branch does not build.

## 3. First-light readings (this branch, 2026-07-25, `release-mcu`, `-Oz`)

Preliminary — one run, not blessed, not published, and the fixture numbers in
`docs/measurements/` are untouched. They are here because they already change
the shape of the decision.

**At `Quality::Exact`, cost is per-draw and per-pixel, with no detectable
16-px-chunk component.** Fit: `null 7,743,984 + 81,215/draw + 8.36/px`, worst
residual 0.5 %. The boundary family says the same thing directly:

| case | w | pixels | chunks | insns above null | insn/px |
|---|--:|--:|--:|--:|--:|
| b15 | 15 | 54,000 | 3,600 | 18,731,498 | 346.9 |
| b16 | 16 | 57,600 | 3,600 | 18,766,163 | 325.8 |
| b17 | 17 | 61,200 | **7,200** | 18,794,436 | 307.1 |
| b31 | 31 | 111,600 | 7,200 | 19,202,192 | 172.1 |
| b33 | 33 | 118,800 | 10,800 | 19,253,421 | 162.1 |

Doubling the chunk count at `b16 → b17` moves the cost by **+0.15 %**. More
than doubling the pixels (`b16 → b33`) moves it by **+2.6 %**. Partial-lane
waste is not the mechanism, and neither is per-pixel pipeline cost — at this
tier **the bill is ~81 k instructions per `fill_path` per band.**

Corroborating, from the probe's own symbol profile (`--attribute`): the
largest single symbols are `scan::path::fill_path_impl` (11.8 %),
`SuperBlitter::blit_h` (8.9 %), `memcpy` variants (14.3 % combined),
`Edge::as_line` (3.5 %) — scan conversion and edge setup, i.e. per-draw work.
The `u16x16` shims are ~7 % *of this workload*, against 23.8 % of the fixture
frame, which is consistent with the fixture's cost being dominated by curved,
anti-aliased, blended content rather than by flat rects.

And from the call census on the committed fixture profile, correcting a
documented inference: lvgl-gap §8 records the `memcpy` traffic as *"consistent
with 32-byte wide types being passed by value through non-inlined pipeline
stages, but that is an inference"*. It is now measured — `__aeabi_memcpy4`'s
callers are **56.8 % `core::slice::sort::…::insert_tail`** (edge sorting),
20.4 % `Vec::push`, **16.4 % `RasterPipelineBuilder::compile`**. That is
per-draw machinery, not wide-value passing. Likewise the `OUTLINED_FUNCTION_*`
bucket (12.1 % of the frame, 71 % of it loads and stores) is called
overwhelmingly from the `u16x16` operators — so those stubs *are* tiny-skia
wide-type work, and the "outlined has no source identity" unknown is closed.

Other readings worth recording:

* **An empty Exact frame costs 7.74 M insns** — the band loop and the
  gutter-pixmap clear, ~15 % of a real frame, before anything is drawn. This
  is the same seam as #38/#40 (`GUTTER`).
* **Partial alpha is expensive — but the "fallback" diagnosis was wrong
  (#60, resolved).** `blend480` vs `w480` is +892 % at Exact, +356 % at Fast,
  +387 % at Draft. The obvious read — that the integer fast path does not carry
  alpha, so a translucent rect falls all the way back to the tiny-skia
  source-over pipeline — is **false for Draft and Fast**: `draft_span` carries
  an integer `d255` source-over, so a flat translucent rect stays **100 %
  fast-path** (proven by `tests/blend_golden.rs::blend_draft_fast_full_fastpath`)
  and comes out **byte-identical to Exact's tiny-skia result**
  (`blend_tiers_agree_byte_exact` — the opaque-dst identity). The +356/387 % is
  the inherent cost of a per-pixel blend over a memset-cheap opaque baseline,
  not a fallback. Only **Exact** routes it through tiny-skia `lowp::source_over`
  (+892 %) — the oracle price, by design. The blend cases are now gated so the
  number cannot be silently rediscovered as a "bug".
* **Fast and Draft price flat rects identically** and diverge only on curves
  (`rrect120`: +81 % Fast vs +4.6 % Draft) — exactly what the tier definitions
  promise, now measured rather than asserted.

**What this does to #37's option list, if it survives replication:** the
premise that a narrower pipeline is the lever is not supported at Exact for
flat geometry. Before any painter work, the probe should be re-run on
content that *does* exercise the pipeline hard (curves, alpha, gradients),
because that is where the fixture's 21 M lives. The right next measurement is
a probe family built from those, not a decision.

## 4. Prior art — how other systems handle this, and what they give up

#37's hardest constraint is not performance, it is that vyr claims to be a
conformance oracle: the same IR must produce the same bytes on x86-64, on an
emulated M4 and on an F429. Everyone else in this space has solved the
per-CPU specialisation problem *without* that constraint, which is why their
solutions cannot be copied wholesale — but their **shapes** are instructive.

| system | mechanism | selection time | output identical across implementations? |
|---|---|---|---|
| **LVGL** | `LV_USE_DRAW_SW_ASM` = `NONE`/`NEON`/`HELIUM`/`RISCV_V`/`CUSTOM`; per-format blend files (`lv_draw_sw_blend_to_rgb888.c` …) with hook macros (`LV_DRAW_SW_COLOR_BLEND_TO_RGB888(...)`) that default to `LV_RESULT_INVALID` and fall back to the C loop | build time | **not required** — no oracle claim |
| **Skia** | `SkOpts` runtime CPU dispatch; the same sources compiled into several ISA variants; the `lowp`/`highp` raster pipeline tiny-skia ports | run time | not guaranteed |
| **Blend2D** | JIT-compiles a pipeline per CPU | run time | not guaranteed |
| **Linux — raid6** | `raid6_select_algo()` *benchmarks every implementation at boot* and picks the fastest | boot time, by measurement | yes (same parity result) |
| **Linux — crypto** | many implementations per algorithm, priority-ordered, plus `kernel_neon_begin/end` for SIMD | run time | **yes, and enforced** — every implementation must pass the same known-answer tests |
| **Linux — general** | `arch/` trees, `ALTERNATIVE` instruction patching, static keys | boot/build time | n/a |

The pattern vyr should copy is the **crypto** one, not the raid6 one: *many
implementations, one conformance vector set, all must agree bit-for-bit.*
That is precisely what the golden hashes already are. The raid6 pattern —
pick by measurement at run time — is exactly what vyr must **not** do, since
a renderer whose output depends on which implementation the machine happened
to choose is not a reference renderer. If vyr ever grows a second painter
path, selection must be a **build-time** decision, and the KAT is the existing
golden set extended to cover it.

That maps directly onto #37's options: option 4 (cfg-gated scalar path for
ARM-without-SIMD) is the LVGL/Skia shape, and it is the one that breaks the
oracle *by construction*, because the selector is "what CPU am I". A narrow
pipeline selected by **what the draw needs** — the way `lowp`/`highp` already
are — has no such problem, which is the second comment's central point and
remains correct regardless of §3's findings.

### On caches, and on being naive

"Make it cache-friendly" is the obvious next thought, and it deserves a
warning label: **this branch cannot see caches at all.** Every number here is
an architectural instruction count from an emulator that models no cache, no
flash wait states and no store buffer. Instruction count is a proxy that has
already been useful, but the thing silicon charges is cycles.

* The Cortex-M4 in the F429 has **no data cache**; it has flash wait states
  and the ART accelerator, so *instruction* fetch locality matters and *data*
  locality mostly does not. On an M7 (I/D cache + TCM) the answer is
  different again. So "cache-friendly" is a **per-part** question, not a
  per-ISA one — the same reason Linux carries `L1_CACHE_BYTES` per
  architecture rather than one global constant.
* The band height is vyr's working-set knob, and it is now sweepable
  (§2.3). On a part with a data cache, band size vs cache size is the whole
  question; on the F429 it is instead a question about SRAM footprint and
  per-band fixed work — and §3 already shows per-band fixed work is 15 % of
  an Exact frame.
* The honest instrument for any cache claim is the **board rung**:
  `--features board` counts DWT_CYCCNT on real silicon. Cycles-per-instruction
  from the same build, on the same scene, is where wait states and stalls
  appear. QEMU's `libcache.so` plugin can *model* a hypothetical D-cache, and
  that is worth doing for the M7 story, but a model is not a measurement.

It is fine for the first answer to be naive — a single build-time
implementation, chosen by measurement, guarded by the existing hash gate. What
is not fine is a naive answer that *sounds* like a cache result without a
cycle count behind it.

## 5. Decision rule (carried forward from the issue comments)

For any option, the question is not "do the pixels change" but:

| change | verdict |
|---|---|
| output moves **uniformly on every ISA** (x86-64, emulated M4, F429 all agree on a new hash) | acceptable — re-bless as its own reviewed commit, stating what changed, why, differing pixel count and max channel error |
| output differs **between** ISAs | fatal — that is losing the oracle, not re-blessing it |
| band equivalence broken (banded ≠ full-frame) | fatal — day-1 invariant |
| tier tiers diverge from each other | fine by design; each tier must still be deterministic across ISAs |

The cross-ISA check already exists (`./dev.py qemu-m4` compares the M4 hash
against the host leg; `scripts/board-run.py` adds silicon). Any candidate
change is required to run all three legs and show agreement **before** its
performance number is quoted.

## 6. Next steps, in order — each is a tracked issue

1. **Replicate §3** — re-run the probe on a clean tree, all tiers, `--opt z`
   and `--opt 3`, `--band-h 8,16,32`, and record it properly. One run is not
   a measurement. (Part of #37; also gated by the band sweep, #58.)
2. **A curve/alpha probe family — #61.** §3 prices flat rects; the fixture's
   21 M lives in anti-aliased curves and blends. Until that family exists,
   #37's central number has not been decomposed — only bounded.
3. **The board rung — #62.** The same probe cases on F429 silicon: cycles,
   not instructions, and therefore the first honest word on locality.
4. **Then** write the position document #37 asks for, with the option table
   scored against measurements rather than expectations, and file scoped
   implementation issues from it.

Until step 4, the correct summary of #37 remains: *the cost is real, the
mechanism is not yet established, and the leading hypothesis has changed.*

## 7. Gaps this branch opened as tracked issues

The attribution gaps `docs/measurements/lvgl-gap.md` §8 lists, and the new
findings §3 surfaced, are now issues rather than prose — all tagged
`performance`:

| gap | issue | status |
|---|---|---|
| `OUTLINED_FUNCTION_*` had "no source identity" (§8) | — | **resolved here** — called from the `u16x16` operators, 71 % loads/stores; see §3 |
| memcpy attributed by callee not caller, "an inference not a measurement" (§8) | — | **resolved here** — 57 % edge-sort, 16 % `RasterPipelineBuilder::compile`; the §8 inference was wrong; see §3 |
| band-count sensitivity never run (§8) | **#58** | `VYR_BAND_H` unblocks it; the 1/2/17-band diff is unrun |
| the ~3.1 M unattributed tail (§8) | **#59** | `insn-mix` reconstructs to 100.0000 %; the tail just needs reporting |
| partial alpha falls back to the Exact pipeline in Draft/Fast (§3) | **#60** | +356–892 % for a flat translucent rect |
| curve/alpha/gradient probe family — decompose the real 21 M (§3, §6.2) | **#61** | the flat sweep bounds but does not decompose it |
| cycles on silicon, not emulator instructions (§4, §6.3) | **#62** | no cache/wait-state claim is falsifiable without it |

The two resolved rows should be folded back into `lvgl-gap.md` §8 when §3's
readings are replicated — deliberately not done yet, because one run is not a
measurement.

## 8. The micro-benchmark landscape, and where vyr actually stands vs LVGL

The §3 flat-rect sweep grew into a ~225-point matrix — every primitive the IR
can express (rect, rounded rect, disc, the `vy_chart` **diagonal** AA polyline,
`vy_line`, `vy_gauge` ring, bordered frame, glyph runs, image blit, and the
composite widgets) × size × alpha × radius × the three tiers — priced on the
emulated M4 (`./dev.py microbench`, `vyr-size/src/probe.rs`,
`scripts/microbench.py`), stored in SQLite and viewable as a grid + graphs
(`docs/perf/microbench.html`). Gradients are the one primitive absent — the IR
cannot express one (**#67**).

### 8.1 The gap is anti-aliased curves, and nothing else

A common read of the LVGL numbers is "vyr is ~19× behind LVGL." The matrix
shows that is true of **one tier on one class of content**, and false
everywhere else. Per-primitive, plugin-exact, insns above the background-only
null (`scripts/lvgl-microbench.py` — LVGL's own `main.c` compiled to render one
matching primitive via `-DLV_PROBE`, both sides measured with libinsn, never
SYS_CLOCK; the script refuses to store a figure above the whole-scene anchor —
the #5-error guard, since `lvgl-gap.md` records four past errors all flattering
vyr):

| primitive | vyr **Exact** / LVGL | vyr **Draft** / LVGL |
|---|--:|--:|
| border (rounded + border) | 27.1× | **0.73× — vyr faster** |
| gauge (ring) | 17.3× | **0.21× — vyr ~5× faster** |

On the tier that actually ships to an MCU (**Draft**), vyr **beats** LVGL on
both. The 17–27× is the **Exact** (oracle) tier — the byte-exact AA reference,
routed through tiny-skia by design.

**The caveat is the finding.** Draft has no anti-aliasing; LVGL's arc and
border are anti-aliased, so part of Draft's win is a quality trade. The
quality-matched tier is **Fast** (integer spans + AA curves through the SAME
tiny-skia pipeline as Exact), and it sits ≈ Exact for curves:

| primitive | Draft (no AA) | Fast (AA curves) | Exact (AA oracle) | insns/px |
|---|--:|--:|--:|---|
| gauge | 28 | **592** | 632 | Fast ≈ Exact — both pay tiny-skia's AA blitter |
| border | 6 | 91 | 125 | |

So the gap is precisely: **anti-aliased curves and strokes, through tiny-skia's
SIMD-shaped blitter on a SIMD-less core (#37).** Its class split is 35–37 %
memory traffic, ~1 % f64 — spill, not arithmetic (§3). LVGL hand-writes a
scalar AA blitter; vyr borrows Skia's vectorised one — free on desktop, ~17×
on M4. Turn AA off and vyr is faster; flat fills are already ahead
(`exact-flat-fast` made Exact rects **30 insns/px, below Draft's 47**).

### 8.2 What this means for the #37 decision

- **If the product ships Draft** (hard-edged, no AA — normal on small/low-DPI
  LCDs, pairs with #51 bitmap fonts): vyr is at-or-ahead of LVGL; there is
  little to chase, and the 17× oracle cost is just the price of *having* an
  oracle.
- **If it needs AA curves on the MCU**: the 17× is tiny-skia's, and #37's
  options (a scalar M4 blitter behind the Fast tier, or a narrow-width
  pipeline — the second issue comment's route) are how to close it. The matrix
  now gives the per-primitive target any such fix is measured against.

### 8.3 Honest limits of the comparison

- Only **border** and **gauge** have clean count-1 LVGL equivalents measured so
  far; **line** is too cheap to resolve above null. `--lv-probe` supports more
  primitives — each is a slow full LVGL recompile.
- Ratios are **indicative**: cross-renderer content matching is imperfect
  (LVGL's arc sits half a pixel inward, rounding maths differ). They agree with
  the independent whole-scene anchor (Draft ≈ 1.7×, Exact ≈ 19×), which is the
  cross-check that they are not error #5.
- The text gap is the one embedded-tier deficit the matrix surfaces that is
  NOT tiny-skia: glyph runs cost ~1,500 insns/px at every tier (runtime
  rasterisation), where LVGL blits compile-time bitmap fonts — tracked as #51.

Reproduce: `./dev.py microbench --deep --deep-full` then
`./dev.py lvgl-microbench --tier draft` (and `--tier exact`).

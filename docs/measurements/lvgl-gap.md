# Why LVGL is cheaper than vyr — a measured attribution

**Measured 2026-07-23**, all figures from the plugin QEMU (`netduinoplus2`,
STM32F405/M4F, exact architectural instruction counts — never `SYS_CLOCK`, see
[`../performance.md`](../performance.md) §5) plus callgrind on the host. Every
number below has a command in §1.1 that regenerates it. **The figures are
perishable; the model is not.**

The starting question was: *LVGL anti-aliases and still costs 54.9 insn/px
where vyr's no-AA `Draft` tier costs 66.4 — where does the 9.06x on `Exact` go?*

The short answer, in one line: **about half of the published gap was not the
renderer at all, and of what remains, the single largest identified item is not
anti-aliasing — it is `libm`'s f32 trigonometry being computed in *double*
precision on a single-precision FPU, once per polygon vertex, per band, per
frame.**

---

## 0. Headline corrections, before anything else

Two things in the current published comparison are measuring something other
than what they claim.

### 0.1 More than half of LVGL's "frame" is the benchmark harness

Both M4 vehicles fold every output byte of every band into an FNV-1a hash so
the frame is provably materialised. That is 388,800 bytes per frame on both
sides, and it is not rendering.

| | fold cost, insns/frame | how measured |
|---|--:|---|
| vyr (`workload::render_frame_banded`, inlined) | **3,110,434** | build with the fold over an empty slice, diff (`scripts/harness-overhead.py`) — identical to 34 insns across Exact and Draft, 8.00 insns/byte |
| LVGL (`flush_cb`, its own symbol) | **3,888,561** | per-symbol attribution (`scripts/m4-attribute.py`), 10.0 insns/byte |

Subtracting it changes the story completely, because the fold is **54.7 % of
LVGL's frame and only 36.2 % of Draft's**:

> **The vyr rows below are the pre-#32 firmware** (Exact 64.4 M, before the
> contour memo). The *model* — that the fold is not rendering, and that it is a
> far bigger share of LVGL's frame than of vyr's — is what this section is for;
> for current per-tier counts see §0.3, and note the fold's own cost moves with
> `opt-level` too (3.11 M at `z`, 1.91 M at `3`), so render-only figures must be
> re-derived per level, not carried across.

| firmware | published insns/frame | harness fold | **render only** | **insn/px** | vs LVGL |
|---|--:|--:|--:|--:|--:|
| vyr **Exact** (`opt-level="z"`) | 64,422,179 | 3,110,434 | **61,311,745** | **473.1** | **19.0x** |
| vyr **Fast** (`z`) | 49,585,035 | 3,110,434 | 46,474,601 | 358.6 | 14.4x |
| vyr **Draft** (`z`) | 8,604,184 | 3,110,434 | **5,493,716** | **42.4** | **1.70x** |
| vyr Exact (`opt-level=3`) | 36,971,836 | 1,912,121 | 35,059,715 | 270.5 | 10.9x |
| vyr Draft (`opt-level=3`) | 6,694,736 | 2,430,136 | 4,264,600 | 32.9 | 1.32x |
| **LVGL 9.6.0-dev** (`-Os`) | 7,112,541 | 3,888,561 | **3,223,980** | **24.9** | 1.00 |

Fast's fold is the only *inferred* cell: it was not separately rebuilt, but the
fold is the same code over the same 388,800 bytes, and Exact's and Draft's
measured values agree to 34 instructions, so 3,110,434 is used for it.

**The claim "vyr Draft costs 4.6 % fewer instructions than LVGL" is an artefact
of the shared harness.** Renderer against renderer, Draft costs **70 % more**
than LVGL while doing *less* (no AA, square corners). The Exact gap is bigger
than published, not smaller: **19.0x**, not 9.06x.

### 0.2 The published `insn/px` column mixes two different normalisers

`docs/performance.md` §3 gives vyr 291.9 / 230.9 / 52.4 insn/px and LVGL 54.9.
LVGL's is `insns / 129,600` — instructions per **delivered** pixel. vyr's is
`insns / pixels_written`, the counter's **overdraw-inclusive touched-pixel**
total (Exact 212,272; Draft 182,216 — measured, printed by every run). Per
delivered pixel vyr Exact is **497.1**, Draft **66.4**. The column as published
flatters vyr by 1.4–1.6x and is not comparable to LVGL's row.

### 0.3 `opt-level="z"` is not the analogue of `-Os` — #33, re-measured

**Re-measured 2026-07-24 at `a9c8a4f`** (`scripts/optlevel-matrix.py`). The
figures first published here were taken before #32's contour memo and are
superseded; every number below is from one session, one commit, one tool per
axis.

The LVGL harness compiles `-Os`. `release-mcu` sets `opt-level="z"`, which is
`-Oz`: it additionally gives up inlining and turns on the machine outliner —
both far more expensive for tiny-skia's generic, SIMD-shim-heavy Rust than for
LVGL's flat C.

**insns/frame** (plugin QEMU + `libinsn`, exact architectural counts), same
ELF pipeline, same scene, Δ against the shipped `z`:

| tier | `z` (shipped) | `s` (the `-Os` analogue) | `2` | `3` |
|---|--:|--:|--:|--:|
| Exact | 51,349,644 | 32,887,551 (−36 %) | **25,336,538 (−51 %)** | 24,564,434 (−52 %) |
| Fast | 36,618,969 | 22,413,829 (−39 %) | **16,764,151 (−54 %)** | 16,761,137 (−54 %) |
| Draft | 8,621,557 | 7,290,627 (−15 %) | **6,513,113 (−24 %)** | 6,504,319 (−25 %) |
| ratio vs LVGL, Exact | 7.22x | 4.62x | 3.56x | 3.45x |
| ratio vs LVGL, Draft | 1.21x | 1.02x | **0.92x** | 0.91x |

(LVGL anchor: 7,112,541 insns/frame, re-confirmed 2026-07-24 on the same
plugin QEMU from a freshly built stock-mirror ELF — `62f343b54`,
`tmp/qemu-insn-lvgl-33.json`. The ratios are whole-firmware, so they still
include both harnesses' FNV fold: §0.1 applies on top, and the fold's own cost
is itself opt-level-dependent, so a *render-only* ratio must be re-derived per
level rather than carried across from §0.1.)

**flash** (`arm-none-eabi-size`, Berkeley `text+data`, `release-mcu`, the size
matrix's own three configs and its own method — comparable cell for cell with
`docs/measurements/f9-static.md`):

| config | `z` | `s` | `2` | `3` |
|---|--:|--:|--:|--:|
| code-only | 424,541 B | 626,433 (+197 KiB) | **611,963 (+183 KiB)** | 647,491 (+218 KiB) |
| font | 588,965 B | 790,737 (+197 KiB) | **776,547 (+183 KiB)** | 811,947 (+218 KiB) |
| font,image | 594,149 B | 796,241 (+197 KiB) | **784,987 (+186 KiB)** | 818,483 (+219 KiB) |
| font,image, % of 2 MiB | 28.3 % | 38.0 % | 37.4 % | 39.0 % |
| font,image, % of 1 MiB | 56.7 % | 75.9 % | 74.9 % | 78.1 % |

**RAM — the binding constraint — does not move at all.** M4 heap peak is
bit-identical at every level (Exact 112,473 B, Fast 114,873 B, Draft 82,881 B
against the 122,880 B arena), and the stack high-water mark
(`--features stack-probe`, #33: the dead CCM stack is painted at boot and
scanned after the workload) moves by **320 B across the whole sweep**:

| tier | `z` | `s` | `2` | `3` |
|---|--:|--:|--:|--:|
| stack watermark, all tiers | 19,044 B | 19,036 / 19,068 B | 18,748 B | 18,820 B |

So `opt-level` is a **pure flash-for-instructions trade**. It buys nothing and
costs nothing on the resource that is actually tight.

**The frame hash is unchanged at every level, for every tier** — Exact
`0x24dcaff531c6eb01`, Fast `0x930d03610b07ea6f`, Draft `0xf98cbbdddd6da1ba`,
12 of 12 cells, and again in the 12 instrumented stack-probe builds. Optimisation
level does not change a pixel, as required; `./dev.py qemu-m4`'s hard gates
(cross-ISA hash + heap peak) are therefore **opt-level-invariant**, and only its
warn-only insns/frame line and the size matrix move with this setting.

Three things the old table got wrong or could not see:

1. **`s` is not on the Pareto frontier.** `opt-level=2` is both *faster*
   (−51 % vs `z`, where `s` is −36 %) and *smaller* than `s` (611,963 vs
   626,433 B code-only). `s` is dominated on both axes; there is no reason to
   pick it except as the flag-for-flag match to LVGL's `-Os` (§0.3.1).
2. **`3` is not worth its extra 34 KB.** Over `2` it buys 3 % at Exact and
   nothing measurable at Fast or Draft (16,761,137 vs 16,764,151 — 0.02 %).
3. **The frontier is `{z, 2}`**, and the whole question is whether ~187 KiB of
   flash is available: it halves Exact and Fast, and takes Draft below LVGL.

### 0.3.1 The comparison cannot be fixed from LVGL's side

The obvious symmetric fix — rebuild the LVGL anchor at `-Oz` so both sides use
size-at-all-costs — **does not exist on this toolchain**. Measured
(`scripts/lvgl-m4-bench/run.py --opt=-Oz`, arm-none-eabi-gcc 15.2.0):

- all **382** LVGL+harness translation units compile **byte-identical** at
  `-Os` and `-Oz`, and so does the linked ELF
  (`d4cbbded…`, 196,348 B `.text` either way);
- `gcc -Q --help=optimizers` and `--help=params` are **identical** between the
  two levels for this target.

GCC's `-Oz` is a near-no-op over `-Os` here; LLVM's `"z"` over `"s"` is 36 % of
vyr's frame. The asymmetry is real, it is between the two *compilers*, and it
can only be closed from vyr's side — by publishing the `s` row next to the `z`
row, never by moving LVGL.

### 0.3.2 The decision: keep `z`, publish the `s` and `2` columns

`release-mcu` is not only the perf profile — it is also the profile the F9
**size** verdict is quoted from ("19 % of 2 MiB"), the profile the board
firmware is flashed at, and the profile the ledger records one row per commit
for. The setting therefore has to be chosen for the smallest part the plan
contemplates, not the most comfortable one, and `docs/plan.md` F9 leaves the
board choice open between an **F429-DISC1 (2 MiB flash)** and an
**H735-DK — 1 MiB**. The runnable vehicle's own linker script
(`vyr-size/link-qemu.ld`) models 1 MiB as well.

At 1 MiB the `font,image` renderer goes from **56.7 % to 74.9 %** of flash: it
still links, but it takes what is left for the application from 454,427 B
(443.8 KiB) to 263,589 B (257.4 KiB) — a **42 % cut to everything that is not
vyr**. That is not a call this document can make on a product's behalf, and it
is the only reason `z` survives:

> **Not a recommendation — a dimension.** `opt-level` is measured at all four
> levels every run and belongs in the matrix, not in a decision made once and
> baked in. `release-mcu` ships `"z"` because it fits the smallest part
> `plan.md` §5 contemplates (a 1 MiB H735-DK), which makes it a safe DEFAULT —
> not the best choice for any particular product. A 2 MiB F429 application has
> ~256 KiB spare and `2` halves the frame at Exact and Fast for zero RAM and
> zero pixel change; a flash-tight application does not. **Choose from the
> matrix at deployment time**, per application, with the numbers in front of
> you. Any column is reproducible in ~12 s:
> `cargo build --config profile.release-mcu.opt-level=2 …`

Concretely: `2` costs +183 KiB of flash and buys −51 % Exact / −54 % Fast /
−24 % Draft, with heap peak, stack depth and every pixel unchanged. On a 2 MiB
F427/F429 that is 9 percentage points of flash for half the render cost and
should simply be taken; on a 1 MiB part it is a real trade against the
application. `s` should never be shipped — `2` beats it on both axes — and `3`
is +34 KiB over `2` for ~3 % at Exact and 0 % elsewhere.

**A second profile is the wrong shape for this.** `release-mcu-perf` at `2`
alongside `release-mcu` at `z` would double the anchor surface: insns/frame,
heap peak, frame hash, flash, silicon cycles and the ledger row would each
acquire a profile qualifier, and the failure this repo keeps having
(`../performance.md` §5) is *mixing* provenance, not lacking numbers. The
opt-level is a sweep axis, not a build configuration: `--config
profile.release-mcu.opt-level=…` reproduces any column in ~12 s
(`scripts/optlevel-matrix.py`), and a downstream product overrides it in its own
`Cargo.toml` in one line. One profile, one set of published numbers, and a
committed table of what the other levels cost.

### 1.1 Regenerating everything here

| numbers | command | output |
|---|---|---|
| **§0.3 opt-level × tier matrix — insns, flash, heap, stack, hash** | `python3 scripts/optlevel-matrix.py` | `tmp/optlevel-matrix.json`, `tmp/optlevel-matrix.md` |
| one tier at one level | `python3 scripts/tier-insns.py --opt 2 --tiers exact` | `tmp/tier-insns-O2.json` |
| the LVGL anchor at a different `-O` | `python3 scripts/lvgl-m4-bench/run.py --opt=-Oz` | `tmp/lvgl-m4-Oz-result.json` |
| per-symbol M4 attribution, all tiers + LVGL; opt-level sweep | `python3 scripts/m4-attribute.py --tiers exact,fast,draft --sweep` | `tmp/m4-attribute.json`, `tmp/m4-attribute.log` |
| harness fold cost (temporarily patches, then restores, `vyr-size/src/workload.rs`) | `python3 scripts/harness-overhead.py --tiers exact,draft [--opt 3]` | `tmp/harness-overhead.json` |
| AA area-vs-perimeter scaling (host, callgrind Ir) | `python3 scripts/disc-scaling.py --shapes disc,gauge,rect` | `tmp/disc-scaling/disc-scaling.json` |
| published per-tier frame counts | `python3 scripts/tier-insns.py --repeat 2` | `tmp/tier-insns.json` |

---

## 2. Where vyr's instructions actually go (measured, not theorised)

Method: `contrib/plugins/libhotblocks.so` with `limit=0` reports every
translation block as `pc, tcount, icount, ecount`; block PC → symbol via the
ELF symbol table gives an exact per-function instruction count. `release-mcu`
strips symbols, so the profiled build is `--config
profile.release-mcu.strip=false` and is cross-checked against `libinsn`: it
reproduces the published count to **within 2,400 of 64.4 M** (0.004 %), so it
is the same code.

### 2.1 Exact — 64,422,179 insns/frame, grouped

| group | insns/frame | % of frame |
|---|--:|--:|
| tiny-skia software SIMD (`u16x16` splat/mul/shr/add/sub) | 12,325,798 | 19.1 % |
| `memcpy`/`memclr`/`memmove` (incl. `__aeabi_mem*`) | 11,297,956 | 17.5 % |
| **software `f64` (`__muldf3`, `__adddf3`, `__aeabi_d*`)** | **11,296,843** | **17.5 %** |
| tiny-skia other (`fill_path_impl`, `SuperBlitter::blit_h`, `blit_rect`, edge clipping, alpha runs, pipeline stages) | 8,020,814 | 12.5 % |
| LLVM machine-outlined stubs (`OUTLINED_FUNCTION_*`, an `-Oz` artefact) | 5,290,828 | 8.2 % |
| **harness FNV fold (not rendering)** | 3,110,434 | 4.8 % |
| premultiply / demultiply | 1,418,064 | 2.2 % |
| IR attribute re-lookup from strings | 1,269,810 | 2.0 % |
| `libm` bodies (`sinf`/`cosf`/`k_sinf`/`k_cosf`/`roundf`) | 986,099 | 1.5 % |

(top-80 symbols cover 61.3 M of the 64.4 M; the residue is a long tail of
sub-0.3 % symbols.)

### 2.2 The `f64` is trigonometry, and the trigonometry is flattening

> **FIXED 2026-07-23 (#32) — this section describes the state BEFORE the fix.**
> The contours are now memoised per caller-owned `Shapes` cache
> (`vyr-core/src/shapes.rs`), so each distinct shape flattens once instead of
> once per band per frame. Same `f32` in, same `f32` out — a pure memo, so
> **every tier's frame hash is unchanged** (`Exact` still
> `0x24dcaff531c6eb01`, identical on x86-64 and the emulated M4).
> Re-measured with the same tool (`scripts/tier-insns.py`, plugin QEMU,
> `release-mcu`):
>
> | tier | before | after | Δ |
> |---|--:|--:|--:|
> | Exact | 64,422,179 | **51,349,644** | −13,072,535 (−20.3 %) |
> | Fast | 49,585,035 | **36,618,969** | −12,966,066 (−26.2 %) |
> | Draft | 8,604,184 | 8,621,557 | +17,373 (+0.2 %, `-Oz` layout drift — Draft caches nothing because it flattens nothing) |
>
> The soft-`f64` group below (11,296,843 insns/frame) is now **208,258** — a
> 98.2 % cut — and `cosf`/`sinf`/`k_sinf`/`k_cosf` have left the top-symbol
> table entirely. Cost: 6,064 B of M4 heap at Exact, 7,984 B at Fast, against
> a `Shapes::DEFAULT_BUDGET` ceiling of 8,192 B and a 122,880 B arena.
>
> The rejected alternative — an f32-native `sinf`/`cosf` — is still rejected
> and now worth much less: the residue it could reach is ~208 k insns/frame
> (0.4 % of Exact), and it would produce different values, hence different
> polygons, hence a re-bless of every golden.

Call counts, per frame, from the same run (entry-block execution counts):

| symbol | calls/frame | insns/frame | insns/call |
|---|--:|--:|--:|
| `libm::cosf` | 4,976 | 130,482 | 26.2 |
| `libm::sinf` | 4,976 | 129,848 | 26.1 |
| `libm::k_sinf` | 4,840 | 246,840 | 51.0 |
| `libm::k_cosf` | 4,840 | 232,320 | 48.0 |
| `__muldf3` | **72,816** | 5,534,687 | 76.0 |
| `__adddf3` | **48,888** | 5,106,293 | 104.4 |

72,816 / 9,952 = **7.3 software double multiplies per `sinf`/`cosf` call**, and
4.9 adds — exactly the shape of musl-lineage `k_sinf`/`k_cosf`, which evaluate
an f32 kernel *in f64*. The M4F FPU is single-precision only, so every one of
those is a compiler-builtins call.

**Each `cosf` therefore costs ~1,145 M4 instructions.** The whole trig chain
(libm bodies + the soft-`f64` they call) is **≈ 11.4 M insns/frame — 17.7 % of
Exact, 22.7 % of Fast, and 3.5x LVGL's entire render.**

Two independent confirmations that this is the flattening path and nothing
else:

- **Draft executes zero trig and zero soft-`f64`** (`0` in both groups above).
  Draft's arcs are integer (`painter.rs:288` `isqrt_i64`); Exact's and Fast's
  come from `circle_points` (`vyr-core/src/painter.rs:148`) and `rrect_points`
  (`:163`), which call `libm::cosf`/`sinf` once per vertex.
- **Fast pays the same bill as Exact** (`__muldf3` 5,528,883 vs 5,534,687)
  although it routes only curved geometry to tiny-skia — because it builds the
  same curve contours.

And the work is *repeated*: `circle_points` is a pure function of
`(cx, cy, r)`, and it is re-evaluated **for every band the shape touches, every
frame** — 17 bands per frame here.

### 2.3 Draft — 8,604,184 insns/frame (5,493,716 rendering)

| group | insns/frame | % of render-only |
|---|--:|--:|
| `mem*` (band `rgb` fill via `copy_within`, per-band buffer zeroing, allocator churn) | 2,075,383 | 37.8 % |
| **IR attribute re-lookup from strings** (`memcmp` 478,279; `Node::str_attr` 160,775; `BTreeMap::get` 96,495; `f32::from_str` 92,776; `trim_matches` 44,242; `ir::walk` 94,059) | 1,271,353 | **23.1 %** |
| painter integer spans (`fill_rgb_triple` 119,040, `isqrt_i64` 51,486, …) | 731,582 | 13.3 % |
| text / skrifa (`read_fonts::table_data` 217,600 per frame) | 636,768 | 11.6 % |
| tiny-skia (allocated but barely used) | 229,867 | 4.2 % |

Two avoidable items are visible here:

- `__aeabi_memclr` is 681,675 insns/frame at Draft. Draft **allocates and zeroes
  the tiny-skia scratch `Pixmap` for every band** (`painter.rs:522`, uncondition-
  al) even though no Draft op touches tiny-skia — 30,720 B × 17 bands = 522 KB
  of zeroing per frame that only the rare rounded-clip fallback ever reads.
- The IR is **re-interpreted from strings on every band**: attribute names are
  compared with `memcmp`, values re-parsed with `f32::from_str`. 17 bands ⇒ 17x.
  LVGL's equivalent (its style-property cascade lookup) costs 458 k insns/frame.

---

## 3. What LVGL does differently (from reading its source)

Read from the local read-only LVGL mirror at upstream commit
`62f343b540340a1f14a79afa99b721c09b1679e6` (the same tree
`scripts/lvgl-m4-bench/run.py` builds; paths below are LVGL-repo-relative).
No LVGL code is copied, transcribed or paraphrased into this repo; what follows
is a description of its architecture.

### 3.1 Its whole render, per symbol (3,223,980 insns/frame)

| symbol | insns/frame |
|---|--:|
| `lv_color_24_24_mix` | 657,276 |
| `lv_draw_sw_blend_color_to_rgb888` | 510,920 |
| `lv_memcpy` / `lv_memset` | 301,242 / 215,296 |
| style-cascade lookups (`get_selector_style_prop`, `get_prop_core`, `lv_style_prop_get_default`, `lv_obj_get_style_prop`, `lv_style_get_prop_inlined`) | 457,850 |
| `lv_font_get_bitmap_fmt_txt` (+ text bucket) | 135,490 (158,016) |
| **all anti-aliasing** (`lv_draw_mask_radius` 114,303 + `lv_draw_sw_mask_radius_init` 39,461 + `lv_draw_sw_mask_apply` 21,330) | **175,094** |
| event dispatch (`lv_event_send`, `event_send_core`, …) | 116,000 |

**LVGL's entire anti-aliasing bill is 175 k insns/frame — 5.4 % of its render,
1.35 insns per delivered pixel.** That is the number to hold next to vyr's
11.4 M of trigonometry.

### 3.2 Why its AA is nearly free

Four decisions, all visible in the source:

1. **The coverage profile is computed once per radius, in integers, and
   cached.** `circ_calc_aa4` (`src/draw/sw/lv_draw_sw_mask.c:1065`) walks an
   integer Bresenham circle upscaled 4x (`circ_init(&cp, &tmp, radius * 4)`,
   `:1096`), computes **one eighth** of the circle, and mirrors it. The result
   lives in a global radius-keyed cache (`_circle_cache`, lookup at `:314`), so
   a second widget with the same radius — or the same widget on the next band,
   or the next frame — costs a hash-table hit. **No floating point is involved
   anywhere in it.**

2. **Only boundary pixels get coverage arithmetic.** `lv_draw_mask_radius`
   (`:834`) writes `aa_len` coverage bytes at the two arc crossings of the row
   and `lv_memzero`s the outside; everything between stays at the row's
   initialised opacity. Rows that are entirely inside the straight part return
   early (`:854`).

3. **The interior is one unmasked rect blend.** `lv_draw_sw_fill`
   (`src/draw/sw/lv_draw_sw_fill.c`) masks only the `rout` corner rows
   (`:148–253`) and issues the whole centre band as a **single** unmasked
   `lv_draw_sw_blend` (`:258–264`) — which lands in a specialised solid-fill
   blitter.

4. **A blitter per (format, opacity, mask-state).** `lv_draw_sw_blend_to_rgb888.c`
   dispatches on four separate paths — plain colour, with-opacity, with-mask,
   mixed — (`:87–100`) so the common case never executes a mask test or an
   alpha multiply. There is no premultiplied round-trip: it blends straight
   RGB888 with `u8` arithmetic.

### 3.3 What that costs it, and what it buys vyr's architecture

LVGL's model is *widget-shaped*: a rounded rect and a circle are the same
primitive with a cached per-radius profile. It has no general path fill; it
cannot render an arbitrary polygon at all. vyr's oracle contract is the
opposite — arbitrary quantised polygons into a general rasteriser, byte-exact
across band splits. That is a real capability difference, not a missing
optimisation.

---

## 4. The "AA only on edges" hypothesis — tested, and falsified for vyr

The hypothesis: tiny-skia walks coverage over a shape's whole area where LVGL
touches only the boundary, so vyr pays O(area) for what LVGL pays O(perimeter).

Test (`scripts/disc-scaling.py`): render one disc of radius r on a fixed
320x320 canvas, subtract the empty-canvas run, and read the log-log slope of
the marginal cost. 1 = perimeter, 2 = area. Host, callgrind `Ir`, exact.

| shape / tier | r=8 | r=16 | r=32 | r=64 | slope | verdict |
|---|--:|--:|--:|--:|--:|---|
| disc / Exact | 98,366 | 179,090 | 333,464 | 656,734 | **0.91** | perimeter |
| ring (gauge) / Exact | 148,998 | 299,182 | 585,934 | 1,214,000 | **1.00** | perimeter |
| flat rect / Exact | 51,476 | 82,564 | 145,507 | 277,670 | 0.81 | sub-linear |
| disc / **Fast** | 164,670 | 319,990 | 723,625 | 1,919,213 | **1.18** (ratios 1.94x → **2.65x**) | drifting to area |
| disc / Draft | 21,789 | 32,300 | 54,333 | 103,936 | 0.75 | sub-linear |

**Exact's marginal disc cost doubles when the radius doubles.** tiny-skia's
`SuperBlitter` already coalesces full-coverage interior runs and hands them to
the solid blitter; vyr is *not* paying per-pixel coverage over solid interiors.
The hypothesis is false for Exact and Fast-as-rasterisation, and this line of
attack should be dropped.

Two things it did find:

- **Fast's scratch round-trip *is* area-scaled.** `fill_into`
  (`painter.rs:1511`) seeds and demultiplies the op's `carry` region per pixel;
  `disc()` (`:1795`) passes the whole bbox, so the ratio climbs from 1.94x to
  2.65x per doubling. The ring already trims its hole via `frame_strips`; the
  disc has nothing to trim, but the round-trip could be limited to the rows the
  path actually inks.
- The linear-in-r term is **not** boundary-pixel coverage — it is per-vertex and
  per-edge work: `circle_points` emits `4·ceil(r/2)` vertices, so vertex count,
  edge count and scanline count all scale with r together. On the M4 that term
  is the trig of §2.2; on the host it is edge walking. **The host profile cannot
  see the M4's dominant cost** — which is why this had not shown up before.

---

## 5. Ranked opportunities

Ordered by measured instructions per frame recovered. "Invariant risk" is
against I1 (banding is the only path), I2 (determinism), byte-exact band
equivalence, polygon-only-to-tiny-skia and 1/64-px quantisation.

| # | change | recovers (insns/frame) | invariant risk | cost |
|---|---|--:|---|---|
| 1 | **Memoise flattened contours** per `(shape, radius)` for the life of a `Request` — no trig after first use | **≈ 10.7 M Exact** (11.4 M less one band's worth), **≈ 10.6 M Fast**; 0 at Draft | **None if it is a pure memo**: same `f32` values, same polygons, same quantisation, same order. Verified by the existing goldens (hash must not move). | Medium: the cache must outlive the per-band canvas, so it needs threading through `render_with_quality` beside `Fonts`/`Assets`. `no_std`+alloc, `forbid(unsafe_code)` — no statics. |
| 2 | **Publish/adopt a higher `opt-level` for MCU perf claims** — re-measured at HEAD, §0.3: `2` dominates `s` | at `2`: **26.0 M Exact / 19.9 M Fast / 2.1 M Draft**; at `s`: 18.5 M / 14.2 M / 1.3 M | None to pixels (hash unchanged in 24 of 24 measured builds) and **none to RAM** (heap peak bit-identical, stack ±320 B); it is a pure **flash trade**: +183 KiB (`2`) / +197 KiB (`s`) | Trivial (a profile line), but it is a product decision, not a free win — verdict in §0.3.2: keep `z`, ship `2` where the flash exists |
| 3 | **Resolve IR attributes once per `Request`** instead of re-parsing strings per band | ≈ 1.27 M (2 % of Exact; **23 % of Draft's render**) | None — same parsed values; validation semantics must stay per-band-independent (I6 honest failure) | Medium: a resolved-attribute representation in `ir.rs` |
| 4 | **Allocate the tiny-skia scratch `Pixmap` lazily** (Draft only ever needs it for the rounded-clip fallback) | ≈ 0.4–0.7 M (**8–12 % of Draft's render**), and 30,720 B off every Draft band | None — it is scratch; nothing reads it before it is written | Small |
| 5 | **Trim Fast's seed/demul carry for discs** to the inked rows rather than the bbox | grows with radius; 2.65x→~2x per doubling at r=64 on the host disc test | Low, but real: under-covering `carry` silently drops AA fringe. Must be derived from the shape's geometry with an outward margin, as `frame_strips` already is | Small |
| 6 | **A cheaper `sinf`/`cosf` that stays in f32** | up to ≈ 11 M, overlapping #1 | **Breaks byte-exactness** — different values ⇒ different polygons ⇒ re-bless of every golden. Only worth it if #1 is impossible. | Medium, plus a re-bless |

Items 1, 3 and 4 together recover **≈ 12.4 M/frame from Exact (20 %)** and
**≈ 1.9 M/frame from Draft (34 % of its render-only cost)** with **no pixel
change**. Draft render-only would land at ≈ 3.6 M/frame — parity with LVGL's
3.22 M, honestly measured, while still not anti-aliasing.

---

## 6. What we ruled out (do not spend time here)

| suspect | measured | verdict |
|---|--:|---|
| premultiply/demultiply round-trip | 1,418,064 insns/frame (2.2 % of Exact); `demultiply` is 129,600 calls at **10.0 insns each** | Not a problem. tiny-skia already short-circuits `alpha == 255` (`tiny-skia-0.11.4/src/color.rs:155`), so the "float round-trip" is a byte copy in practice. |
| the 8 px gutter | ~~pixmap zeroing is 853,267 insns/frame **total**; the gutter's share is ≈ 440 k (0.7 % of Exact)~~ **superseded — see the correction below** | ~~Costs **RAM** (63,488 B vs 30,720 B per band), not time. Not a perf lever.~~ It costs both: **9.2 M insns/frame (17.9 %)** and 22,560 B of arena. |
| per-band pixmap allocation/zeroing generally | 853 k insns/frame at Exact | 1.3 % of Exact — matters only at Draft (§2.3), where it is 12 % of a much smaller number |
| tiny-skia computing coverage over solid interiors | slope 0.91 (§4) | Falsified. |
| path flattening *density* | `4·ceil(r/2)` vertices; halving it was already superseded in #27 | The vertices are not the cost — the **trig per vertex** is (§2.2), and that is fixable without changing vertex count |

**Correction, #38 (2026-07-23): the gutter IS a lever, by 26×.** The 440 k
figure priced only the extra *zeroing*. Rebuilding the same tier with the
constant changed and counting with the same tool (`scripts/tier-insns.py`,
plugin-exact, `--profile release-mcu`, same commit) prices the whole thing:

| GUTTER | M4 heap peak | insns/frame |
|---|--:|--:|
| 8 (shipped) | 112,473 B | 51,349,644 |
| 4 | 89,913 B | 42,156,216 |

**−9,193,428 insns/frame (−17.9 %) and −22,560 B (−18.4 % of the 122,880 B
arena)**, frame hash unchanged (`0x24dcaff531c6eb01`). The rasterization and
the per-band clip/edge work over the overscan rows were never counted, only the
memset. It is not a free win today — 4 is not a sufficient overscan (see
`GUTTER`'s table in `vyr-core/src/painter.rs`, and #40) — but it is the size of
the prize for fixing the reason the overscan exists.

---

## 7. What is structural, and not worth chasing

- **LVGL has no general path rasteriser.** Its rounded rect and its arc are the
  same cached integer circle profile; it cannot fill an arbitrary polygon. vyr's
  oracle contract requires exactly that. Matching LVGL's AA cost by adopting its
  algorithm means giving up the general path, or writing a second rasteriser and
  accepting a pixel re-bless.
- **tiny-skia's raster pipeline is SIMD-shaped, and the M4 has no SIMD.** The
  `u16x16` wide type plus its `memcpy` traffic is ≈ 21 M insns/frame — a third
  of Exact — and it exists because tiny-skia is a port of Skia's vectorised
  lowp pipeline. `opt-level=3` recovers a large part of it via inlining; the
  rest is inherent to the painter choice, not to a bug in vyr. Removing it means
  a different painter behind the `Canvas` seam (F13/F8's `vello_cpu` line), not
  a tuning pass.
- **LVGL renders a widget tree it owns end to end**; vyr renders an IR it must
  re-interpret. Some of §2.3's string-lookup cost can be cached away (#3), but
  the IR-authoritative contract (I5/I8) is the product, not an overhead to
  delete.
- **Content is still not identical** — `scripts/lvgl-m4-bench/compare.md` audits
  ~9.6 % of the frame (theme colours, Montserrat vs subset-Roboto, border
  placement, half-pixel arc radius). The text paths in particular are not
  comparable: LVGL blits compile-time bitmap fonts (158 k insns/frame), vyr
  runtime-rasterises through skrifa into a glyph cache (637 k insns/frame at
  Draft). Closing that would move LVGL's number **up** somewhat; it does not
  explain a 19x.

---

## 8. What we still cannot attribute

Stated plainly, because a precise "we do not know" beats a confident story:

- The top-80 symbols cover **95 %** of Exact's frame; the remaining ~3.1 M is a
  tail of sub-0.3 % symbols that has not been broken down.
- `OUTLINED_FUNCTION_*` (5.3 M, 8.2 %) is attributed to LLVM's machine outliner
  but **not** to the code it was outlined *from* — outlined stubs have no source
  identity. Part of it is certainly tiny-skia's pipeline; the split is unknown.
  It largely disappears at `opt-level=3`.
- The `memcpy` traffic (11.3 M, 17.5 %) is attributed by callee, not caller.
  118,809 calls/frame averaging 55.6 insns each is consistent with 32-byte wide
  types being passed by value through non-inlined pipeline stages, but that is
  an inference from the call-size distribution, not a measurement.
- **Per-band re-work has been measured only for the trig path.** How much of the
  rest of Exact's cost is repeated 17x per frame (edge lists, clip decisions,
  pipeline setup) has not been isolated; the band-count sensitivity experiment
  (render the same frame in 1 / 2 / 17 bands and diff) has not been run because
  `BAND_H` is a compile-time constant in the M4 vehicle.

---

## 9. Filed issues

| issue | what |
|---|---|
| [#31](https://github.com/awtoau/vyr/issues/31) | M4 comparison counts the harness FNV fold as render cost (§0.1) and mixes `insn/px` normalisers (§0.2) |
| [#32](https://github.com/awtoau/vyr/issues/32) | Curve flattening re-runs `libm` f32 trig — computed in soft `f64` on M4F — per band, per frame: ≈ 11.4 M insns/frame (§2.2, opportunity #1) |
| [#33](https://github.com/awtoau/vyr/issues/33) | `release-mcu` `opt-level="z"` vs LVGL's `-Os`: 29–43 % of the frame, +198 KB flash (§0.3, opportunity #2) |
| [#34](https://github.com/awtoau/vyr/issues/34) | IR attributes are re-parsed from strings on every band: 23 % of Draft's render cost (§2.3, opportunity #3) |
| [#35](https://github.com/awtoau/vyr/issues/35) | Draft allocates and zeroes the tiny-skia scratch pixmap it never uses (§2.3, opportunity #4) |
| [#36](https://github.com/awtoau/vyr/issues/36) | Fast's scratch seed/demul round-trip is area-scaled for discs (§4, opportunity #5) |

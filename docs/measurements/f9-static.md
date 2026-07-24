# F9 static numbers — flash + static RAM on thumbv7em-none-eabihf (#9)

**Status:** measured 2026-06-11 (toolchain `1.95.0`, pinned `Cargo.lock`).
This is the STATIC half of the F9 embedded spike: what the renderer costs in
flash and link-time RAM on an STM32F427-class M4F. The DYNAMIC half
(on-target ns/px, band size vs frame time, RGB565 convert cost, glyph-cache
boot fill) stays open pending the board decision — see "What remains" below.
**Update 2026-06-12:** the EMULATED dynamic half is now measured — the
vehicle BOOTS on a qemu Cortex-M4 and renders banded frames; see
"Dynamic-environment numbers (measured)" at the end of this doc.

Reproduce: `./dev.py size-mcu` (the table below is its output); symbol
ranking: `scripts/size-rank.py` on an unstripped build (instructions in the
script header).

## The measurement vehicle

`vyr-size` is a workspace bin crate that links vyr-core's REAL render path
(IR JSON parse → render tree → layout → paint → pixels) for
`thumbv7em-none-eabihf` against the F427 memory map (`vyr-size/link.ld`:
2 MiB flash @ 0x08000000, 192 KiB SRAM @ 0x20000000), with `--gc-sections`
and `ENTRY(_start)`. A real **linked ELF** is the only honest size number —
an rlib/staticlib overcounts because dead-code elimination has not run.
It is **not runnable firmware**: no vector table, no `.data`/`.bss` init, no
clocks (and no cortex-m-rt — nothing in the numbers that isn't renderer).
`_start` renders one 120×16 band of each fixture into a heap buffer through
a 64 KiB bump-arena `#[global_allocator]` and folds every output byte into
an observable sink, so the optimizer must keep the whole path.

Features select which **assets** are baked into `.rodata`, not which code is
linked: every widget arm of the IR interpreter is statically reachable from
`render()` (JSON content is runtime data — LTO can never prove text or
images away), so the text and image **code** is all-in even in the
code-only build. `font` bakes the real `fonts/roboto.ttf` (160,310 B);
`image` bakes the 24×24 RGBA checker (2,304 B; raw twin of the committed
F6 test PNG, decode-at-build — the F15 model).

Profiles: `release` (the desktop oracle profile, opt-level 3) and
`release-mcu` (workspace root: inherits release, `opt-level = "z"`,
`lto = "fat"`, `codegen-units = 1`, `panic = "abort"` — the thumbv7em target
spec aborts regardless — `strip = true`).

## The table (`arm-none-eabi-size`, Berkeley: text includes .rodata)

| config | profile | text | data | bss | flash (text+data) | % of 2 MiB | static RAM net¹ |
|---|---|--:|--:|--:|--:|--:|--:|
| code-only | release | 888,472 | 8 | 65,552 | 888,480 | 42.4% | 24 B |
| code-only | **release-mcu** | 398,317 | 0 | 65,548 | **398,317** | **19.0%** | 12 B |
| font | release | 1,053,440 | 8 | 65,552 | 1,053,448 | 50.2% | 24 B |
| font | **release-mcu** | 562,741 | 0 | 65,548 | **562,741** | **26.8%** | 12 B |
| font,image | release | 1,060,352 | 8 | 65,552 | 1,060,360 | 50.6% | 24 B |
| font,image | **release-mcu** | 567,933 | 0 | 65,548 | **567,933** | **27.1%** | 12 B |

> **Two things to know before quoting this table (2026-07-24).**
> 1. **It has drifted.** The same three `release-mcu` configs measure
>    **424,541 / 588,965 / 594,149 B** at `a9c8a4f` (`./dev.py size-mcu`,
>    `tmp/size-mcu.json`, and the ledger's `size_flash` series) — the renderer
>    has grown ~26 KiB since. The percentages below move with it; the *method*
>    is unchanged.
> 2. **Every figure here is `opt-level="z"`**, which is `-Oz`, and that costs
>    36 % of the Exact frame against `opt-level=2`. What each level costs in
>    flash and buys in instructions — and why `z` stays the default — is
>    measured in [`lvgl-gap.md`](lvgl-gap.md) §0.3
>    ([#33](https://github.com/awtoau/vyr/issues/33)). Short version: `2` is
>    +183 KiB of flash for −51 % Exact / −54 % Fast / −24 % Draft, with the M4
>    heap peak and stack watermark unchanged.

¹ static RAM net = data + bss − 65,536 B (the vehicle's bump arena is
measurement scaffolding, not renderer). The renderer's own link-time RAM is
**~12 bytes** — by design: vyr-core owns no static buffers; all working
memory is caller-provided or heap (invariant I7/I1).

Deltas worth reading off the table (release-mcu):

- **Code-only renderer: 389 KiB flash = 19.0%** of the F427's 2 MiB.
- **`font` − `code-only` = +164,424 B** ≈ the 160,310 B TTF + ~4 KiB
  registration/fixture glue: the text *code* was already in the code-only
  build (see above); a font costs what the file costs.
- **`font,image` − `font` = +5,192 B** ≈ the 2,304 B baked RGBA + ~2.9 KiB
  glue (incl. the image fixture's IR string).
- **`release` is 2.2× `release-mcu`** — opt-level z + fat LTO + one codegen
  unit is the difference between 42% and 19% of flash. The MCU profile is
  not optional on this class of part.

Section split of the full config (font,image / release-mcu):
`.text` 344,868 · `.rodata` 223,049 (160,310 of it the TTF, 2,304 the
checker) · `.ARM.exidx` 16 · `.bss` 65,548 (65,536 arena).

## What dominates flash (code-only / release-mcu, unstripped, `size-rank.py`)

373,839 B attributed across 2,671 text+rodata symbols:

| crate / bucket | bytes | share | reading |
|---|--:|--:|---|
| tiny-skia | 104,708 | 28.0% | the painter; includes pipeline stages vyr never calls (bicubic 5.9 K, bilinear 3.1 K, soft_light 2.4 K, gradients…) kept live by its runtime stage dispatch |
| skrifa | 73,506 | 19.7% | glyph outlines; single biggest symbol in the ELF is its TrueType **hinting VM** `dispatch_inner` (14.8 K) — vyr draws **unhinted**, but hinted/unhinted is a runtime `DrawSettings` choice so the VM stays reachable |
| read-fonts | 47,468 | 12.7% | font table parsing; ~15 K of it six monomorphized CFF/CFF2 charstring `Evaluator`s — Roboto is glyf, but the format arrives at runtime |
| (llvm outlined) | 28,068 | 7.5% | opt-z machine outlining — shared code factored out of all crates |
| core | 25,251 | 6.8% | fmt + str + the float-PARSE tables (`dec2flt::POWER_OF_FIVE_128`, 10.4 K rodata) |
| vyr-core | 20,884 | 5.6% | **the renderer itself** — parse-to-paint, all widgets |
| zmij | 13,788 | 3.7% | float-PRINT tables (`POW10_SIGNIFICANDS` 9.9 K) pulled by serde_json |
| serde_json | 11,338 | 3.0% | + its own `POW10` (2.5 K) |
| libm | 7,624 | 2.0% | deterministic float math (the policy) |
| alloc / compiler_builtins / tiny-skia-path / rest | ~19,400 | 5.2% | incl. f64 softfloat shims (`__aeabi_d*` — M4F has no double FPU) |

Bloat findings (none of them blockers at 19%; all are recorded trim levers):

1. **vyr-core is 5.6% of its own renderer's flash.** The footprint is the
   libraries' generality, not our code.
2. **~30 KiB of dead-at-runtime font machinery** (hinting VM + CFF
   evaluators) is kept by runtime-dispatch reachability. If flash ever
   matters, that is an upstream-feature/patch conversation (skrifa has no
   "unhinted-only" / "glyf-only" features today), not a rewrite.
3. **~26 KiB of float text↔binary tables** (dec2flt + zmij + POW10) for
   parsing/printing JSON numbers. The IR uses small integers almost
   exclusively; a leaner number path (or a binary IR form, long-term) would
   drop most of it. `panic = "abort"` is already in force (target default +
   profile); error-`String` `format!` machinery is part of the honest-failure
   design and is counted in the core/alloc rows.
4. **No multi-MB surprise existed** — but a subtler one did, the other way:
   see the next section.

## The measurement lesson (why `black_box` is load-bearing)

First build of the `font` config measured **2.8 KiB** — LLVM had inlined the
bump allocator, constant-folded the 160 KB `to_vec` of the TTF against the
64 KiB arena, proven the allocation must fail, and dead-coded the ENTIRE
render path behind the guaranteed alloc-error panic. The optimizer was
right; the vehicle was wrong. Fix: every baked input (TTF, RGBA, IR strings)
passes through `core::hint::black_box` — "these bytes arrive from a flash
address at run time", which is exactly the deployment truth. Sizes are
honest only because the inputs are opaque.

## Working-RAM model (what the static numbers do NOT include)

Link-time RAM is ~12 B because the renderer's memory is caller-provided +
heap by design. The real per-frame working set, from the painter's
architecture (8 px gutter on every band side; numbers exact):

| item | formula | 480×16 band | 120×120 frame |
|---|---|--:|--:|
| band pixmap (internal, premultiplied RGBA8888) | (w+16)·(h+16)·4 | 63,488 B | 73,984 B |
| caller's RGB888 band buffer | w·h·3 | 23,040 B | 43,200 B |
| clip mask (lazy — **only** when widgets overflow a clip) | (w+16)·(h+16)·1 | 15,872 B | 18,496 B |
| glyph cache (measured, F5 fixture: 19 masks @ 14/20 px) | A8 masks | 2,209 B | 2,209 B |
| parsed IR tree + transient parse | scene-dependent | ~few KiB | ~few KiB |

So a **480×16-band text UI ≈ 90–105 KiB** of working RAM (pixmap + band
buffer + glyph cache + tree, clip mask only if clipping overflows) —
**~47–55% of the F427's 192 KiB SRAM**, comfortable, with the 64 KiB CCM
(core-only, no DMA) still free for stacks/scratch. A full 480×320 frame
pixmap is 666,624 B — external-SDRAM territory, exactly the
full-framebuffer-vs-banded split F9's on-target half is scoped to measure.

**API finding (the big one for the on-target half):**
`Fonts::register(name, bytes: Vec<u8>)` *owns* its bytes — registering
Roboto **copies the 160 KB TTF into heap RAM**, which together with the
working set does not fit 192 KiB. On MCU the TTF should be borrowed straight
from flash: a `register`-by-reference (`&'static [u8]`) variant (or F15
baked glyph tables) is needed **before** on-target text work. Flagged for
the F9 verdict; not changed now (vyr-core untouched by this measurement).

## What remains for the F9 verdict

- **Board decision**: F429-DISC1 vs H735-DK (plan §5.1) — blocks everything
  below.
- On-target **ns/px** per op class (f32 AA on the single-precision FPU) and
  **band size vs frame time** (the I4 scaling law, on real silicon).
- **RGB565 output** convert cost (real panels are 565; feeds F13 DMA2D).
- **Glyph-cache boot fill** time (F5's at-boot rasterization).
- The **`Fonts` borrow-from-flash API** above.
- Hot-path identification → the verdict doc: ship-it / fixed-function
  painter tier behind the Canvas trait / C++-tier fallback.

**Static-half reading:** 19.0% flash code-only (27.1% with a full vector
font + image baked) and ~12 B static RAM leave generous headroom on a 2 MiB
F427-class part — nothing in the static numbers argues against the
embedded target. The open risks are speed (ns/px on a 180 MHz M4F) and the
font-RAM API, both squarely the dynamic half's job.

## Dynamic-environment numbers (measured) — 2026-06-12

This section carries **MEASURED dynamic-environment numbers**, not models:
the vehicle now RUNS. `vyr-size --features run-qemu` (`./dev.py qemu-m4`) is
a bootable ARMv7-M program — vector table, crt0 (`.data` copy-up, `.bss`
zero), FPU enable, semihosting I/O, and a COUNTED real alloc/dealloc heap —
that renders a 480×270 text+image+widgets fixture as **480×16 horizontal
bands** under `qemu-system-arm -machine netduinoplus2` (Cortex-M4F). Logs:
`tmp/qemu-m4.log`; the cross-environment table is produced by awto-vyvanse's
`scripts/memprofile-vyr-lvgl.py --vyr-repo <this repo>`.

### The machine reality (boot lessons, measured the hard way)

- **netduinoplus2 (STM32F405) in qemu ≥ 8 has 128 KiB SRAM @ 0x20000000 +
  64 KiB CCM @ 0x10000000** — NOT the 192 KiB contiguous the original qemu
  model had (first boot: stack writes just under 0x20030000 BusFault →
  "Lockup: can't escalate 3 to HardFault"). That budget is strictly TIGHTER
  than the F427's 192 KiB + 64 KiB CCM, so fitting here fits the F427 with
  64 KiB to spare. Placement follows the classic F4 discipline (the CCM
  decision the static doc deferred): **heap arena (120 KiB) in SRAM; stack +
  the 23,040 B band buffer in CCM** (CPU-only memory — exactly the buffer
  you'd never hand to DMA).
- **The M4F's FPU is architecturally DISABLED at reset** and eabihf codegen
  uses VFP freely: without a CPACR enable as the first instructions of
  `reset`, the first FP instruction is a NOCP UsageFault escalating to a
  boot lockup. (Second boot. The third one ran.)
- The vehicle's bump allocator cannot run a banded frame (17 gutter pixmaps,
  never freed, overflow any F4 SRAM) — the run-qemu config swaps in a real
  first-fit heap (`linked_list_allocator`, counted with the same
  live/peak semantics as vyr-cli's CountingAlloc).

### The cut-down font (LVGL-style, #19 context)

`vyr-size/assets/roboto-ascii.ttf`: printable-ASCII subset of
`fonts/roboto.ttf`, hinting + all OpenType layout dropped (vyr renders
unhinted and shapes by plain advances) — **8,084 B vs 162,876 B full
(5.0%)**. Regenerate: `scripts/make-subset-font.py`; provenance + license:
`vyr-size/assets/roboto-ascii.md`. The goldens' `fonts/roboto.ttf` is
untouched.

### The table (composition as rows, environments as columns)

Same 480×270 fixture in every vyr column (the M4 workload scene); bytes are
exact counting-allocator numbers, not sampled. x86-64 + ARM32 are vyr-cli
full-frame renders; ARM32 = static armv7-musleabihf build under
qemu-arm-static; m4-banded = the qemu-system run above.

| metric | x86-full | x86-subset | arm32-full | arm32-subset | m4-banded | lvgl-runner |
|---|--:|--:|--:|--:|--:|--:|
| gutter pixmap | 567,424 | 567,424 | 567,424 | 567,424 | 63,488 | (pool) |
| out buffer | 388,800 full | 388,800 full | 388,800 full | 388,800 full | 23,040 band¹ | (pool) |
| font copy | 162,876 | 8,084 | 162,876 | 8,084 | 8,084 | (pool) |
| glyph cache | 3,349 | 3,349 | 3,349 | 3,349 | 3,349 | (pool) |
| parsed IR + misc² | 56,203 | 56,205 | 51,165 | 51,167 | 31,488 | (pool) |
| TOTAL heap peak | 1,178,652 | 1,023,862 | 1,173,614 | 1,018,824 | 106,409 | 8,914,884³ |
| Max RSS (KiB) | 4,568 | 4,608 | 9,488⁴ | 9,420⁴ | n/a (no OS) | 98,348 |

¹ the M4's band buffer is a CCM **static**, deliberately outside its heap —
still RAM, priced by its own row.
² residual = peak − (font + pixmap + out-buffer-if-heap + glyph cache):
parse tree, registries, transients.
³ lvgl-runner (vyvanse-runner, measured 2026-06-11, rig frame 0 — same
480×270, different scene, desktop context): massif peak that **excludes**
its `LV_MEM_SIZE` 64 MiB internal pool (lv_malloc is invisible to massif)
and rides a 96 MiB RSS of SDL/EGL driver noise — a desktop baseline, not an
MCU number.
⁴ qemu-arm-static EMULATOR process RSS, not a target number.

### Cross-ISA determinism, now THREE ways

The frame hash `0x6b0c51567a991741` (FNV-1a over all 388,800 RGB888 bytes)
is identical for: the x86-64 banded render, the x86-64 FULL-frame render
(band equivalence), and **the banded render on the emulated Cortex-M4
itself** — plus x86 vs ARM32-user-mode PNGs byte-identical (full and subset
fonts). Determinism holds across ISA, word size, and band decomposition.

### Virtual-time numbers (icount — deterministic, and what they do NOT say)

Under `-icount shift=0,sleep=off` the virtual clock advances exactly 1 ns
per guest instruction, so semihosting SYS_CLOCK deltas are instruction
counts (resolution 1 cs = 10⁷ insns):

- **4 warmed frames (glyph cache full) in 30 cs ⇒ ~75.0 M insns/frame ≈
  579 insn/px** — a deterministic regression metric (counts don't flake;
  the ±1 cs quantization is the only wobble, and code changes between
  builds legitimately move the count a couple of percent).
- **@180 MHz that is ~417 ms/frame ESTIMATE** — labelled hard: it assumes
  CPI = 1.0, which a real M4 does not have (flash wait states, write
  buffers, dual-issue absent); calibration against real silicon is exactly
  the remaining board half of F9. Even ±2× says: full-screen 60 fps full
  redraws are NOT the M4 story — the incremental/dirty-rect path (measured
  ~8× cheaper per step on the panel fixture, F3) plus band-limited updates
  are, and a fixed-function painter tier remains on the table (the verdict
  doc's existing option).

Per-phase heap (M4, live/peak B): font-reg 8,186/8,186 → asset-reg
10,864/10,864 → parse 17,429/17,429 → first-band 23,286/92,609 → frame
23,286/106,409. Steady-state live between bands is just 23.3 KB; the peak
is the in-band transient (gutter pixmap + painter scratch).

### What are we missing — the deltas this table answers

- **The font copy is THE lever**: −154,792 B everywhere (full → subset), and
  on the M4 it is the difference between "cannot even start" (162,876 B copy
  > the whole 128 KiB SRAM) and an 8 KB line item. #19 (borrow-from-flash
  registration) stays open: even 8,084 B is a copy that should be 0, and
  with it the FULL font becomes viable on-MCU (read in place from the 27%
  flash config).
- **32-bit shrinks the residual, not the surfaces**: ARM32 saves ~5,038 B vs
  x86-64 (56,203 → 51,165 residual) — usize-sized overheads in the parse
  tree/registries; pixmap, buffers, font and glyph bytes are identical.
- **Banded vs full is the 9.5× heap difference**: 106,409 vs 1,018,824 B at
  the same word size and font — the gutter pixmap + out buffer rows shrink
  ~10× (63,488 + 23,040 vs 567,424 + 388,800), everything else carries over.
- **M4 measured peak vs the F9 static model**: the model said "480×16-band
  text UI ≈ 90–105 KiB"; measured is 106,409 B heap + 23,040 B CCM ≈
  126 KiB working set — the extra is exactly what the model listed as
  unpriced (the font heap copy 8,084 B, the image asset 2,304 B, a bigger
  scene's parse tree ~10 KB, parser transients). **~66% of an F427's 192 KiB
  SRAM**, and it RAN inside the strictly-smaller F405 budget.
- **vyr vs LVGL context**: vyr's whole 480×270 frame on the M4 fits in a
  heap smaller than 1/80th of the pool the desktop LVGL runner merely
  RESERVES (64 MiB) — different contexts (see ³), but the scale gap is the
  point of the row.

Reproduce: `./dev.py qemu-m4` here; the table:
`python3 scripts/memprofile-vyr-lvgl.py --vyr-repo <this repo>` in
awto-vyvanse. Still open for the BOARD half: real-silicon CPI calibration of
the insn counts, RGB565 convert cost, glyph-cache boot-fill wall time, and
the #19 zero-copy font registration.

### F16 Draft tier — measured insns/frame on the SAME emulated M4 (#16) — 2026-06-12

The Exact frame above costs **~75 M insns/frame** because the tiny-skia
float-coverage AA path runs on every fill — the M4 benchmark exposed a ~7.4×
per-pixel gap vs an LVGL equivalent (~10 M insns/frame). F16's **Draft** tier
is the lever against it. The **v1** set accelerated only the OPAQUE
axis-aligned `fill_rrect` (radius 0) — the dominant UI fill (backgrounds,
panels, track/bar fills, the backdrop) — as a direct integer span fill (no
path, no coverage, no tiny-skia), and recovered ~23–27 % of the gap.

**The v2 set (this update) extends the integer no-AA fast path to the CURVE
primitives** — `disc`, `ring`, `line` — which were the bulk of the residue on
the curve-heavy panel fixture (the gauge ring, the slider/toggle knobs, the
footer rule):

- **disc**: per scanline the span `[cx−dx, cx+dx]` from the exact integer test
  `dx = isqrt(r²−dy²)` (pure integer; no libm `sqrt`, no rounding-mode
  dependence). Hard edge.
- **ring**: outer filled-circle spans minus inner-circle spans per scanline —
  two spans where the row crosses the hole, one where it doesn't.
- **line**: the SAME butt-capped quad the Exact path builds, rasterized as
  integer per-row spans from the quad's edges in 1/256-px fixed point (a pure
  integer function of the world row; pixel-centre coverage rule).

Each handles the OPAQUE write (a plain premultiplied store) AND the TRANSLUCENT
write (the d255 integer source-over the glyph/image blits use), so a
translucent disc/line is still a fast-path pixel. All four stay
`forbid(unsafe_code)` + `no_std` + safe slice indexing — the safe-Rust integer
cost is what the numbers below price. Still falling back to Exact in v2:
radius>0 fills (drawn SQUARE), `stroke_rrect`, `fill_linear_gradient`, glyphs,
images. `RenderStats::fastpath_pixels` records exactly what the fast path
carried; Draft is its own deterministic, band-exact tier (own goldens,
`tests/draft_golden.rs`), NEVER compared byte-for-byte to Exact.

**The headline, same vehicle, same 480×270 banded frame, same icount clock**
(`./dev.py qemu-m4` vs `./dev.py qemu-m4 --draft`):

| tier | insns/frame | insn/px | fast-path coverage | cross-ISA hash |
|---|---|---|---|---|
| **Exact** (default, oracle) | **~75 M** (31 cs) | 598 | 0.0 % | M4 == x86-64, banded == full |
| Draft **v1** (rects only) | 60 M (24 cs) | 463 | 80.9 % | M4 == x86-64, banded == full |
| **Draft v2** (+ integer curves) | **20 M** (8 cs) | **154** | **87.4 %** | M4 == x86-64, banded == full |
| _LVGL anchor_ (the gap target) | _~10 M_ | _~77_ | — | — |

- **Draft v2 = 3.75× the Exact frame** (75 M → 20 M; 8 cs vs 31), and against
  the 75 M-Exact / 10 M-LVGL anchors **Draft v2 recovers 85 % of the
  Exact→LVGL gap** (55 M of 65 M insns/frame) — up from the **23–27 %** of v1
  (rects only). The integer curves were the single biggest lever: on this
  fixture the gauge ring + two slider knobs + toggle knob + rule were paying
  the full tiny-skia float-coverage cost, and removing it tripled the recovery.
  Draft v2 stays fully deterministic and band-exact cross-ISA (M4 hash == host
  hash == full-frame hash `0x5489ab9b708fa077`, a DIFFERENT hash from Exact —
  the no-AA bytes).
- **Fast-path coverage 87.4 %** of delivered pixels (was 80.9 %). The
  remaining ~13 % is the radius>0 corner fills (drawn square but still routed
  through the AA fill for the body in Exact-fallback — see below), the
  gradient, the text runs, and the image blit — those still pay the
  tiny-skia/Exact cost and ARE the remaining residue. (Note the insns/frame
  fell far more than coverage rose: coverage counts PIXELS, but the
  curve ops were disproportionately EXPENSIVE per pixel under tiny-skia —
  flatten + stroke + float coverage — so moving them off it is worth more
  insns than their pixel share suggests.)
- **Host micro-numbers** (`./dev.py bench`, x86-64 release): the raw rect lever
  is still **24× faster per pixel** (0.83 → 0.03 ns/px). The blended DEMO_IR
  scene is now **3.65× faster** end-to-end (Exact 5.87 → Draft 1.61 ns/px; was
  ~1.5× when only rects were fast). Fidelity delta Draft-v2 vs Exact on
  DEMO_IR: **1088/14400 px differ (7.6 %), max channel error 220/255** — up
  from v1's 628 px / 4.4 % because the disc/ring/line AA fringe pixels now
  differ too. That extra ~3.2 % of the frame is the honest cost of the
  hard-edged curves: every difference is an antialiased edge pixel Exact
  smooths and Draft does not.

> **Superseded by Draft v3** (the "Draft v3 — FULLY integer + gutter-less"
> subsection below): v3 acted on items 1 and 2 here (radius>0 fills + gradient
> → integer; gutter dropped) and reached **13.0 M = 1.30× LVGL**, recovering
> 95 % of the gap. The v2 read below is kept for the record.

**Honest read — is integer-curves-in-Draft enough, or is an own fixed-function
painter the real "match LVGL" answer?** Draft v2 is **2.0× the LVGL anchor**
(20 M vs ~10 M) — close enough that the gap is no longer dominated by AA. What
remains, named:

1. **tiny-skia setup + the radius>0 fills and gradient still on the float
   path** — the v2 curves are off it, but rounded-corner fills and the
   two-stop gradient still build a polygon and run the float rasterizer.
2. **The 8 px GUTTER overscan** — every band still rasterizes (w+16)×(h+16);
   the future `Fast` knob (gutter-off) removes that per-band fixed cost. At
   480×16 bands the gutter is ~12 % of raster pixels, unpriced by Draft.
3. **Text** — glyph blits are already a hand-rolled integer source-over (not
   tiny-skia), but the run still costs; bilevel masks (a Draft glyph knob, #16)
   would shrink it.

The read: **the integer fast path is most of the answer, and an own
fixed-function painter is now a SMALL remaining step, not a rewrite.** The last
2× is (1) routing radius>0 fills + gradient through the same span machinery (a
fixed-function tier behind the `Canvas` trait — the F9 verdict's existing
option, now scoped to a handful of ops, NOT a from-scratch rasterizer), plus
(2) the gutter-off `Fast` knob. Draft v2 proves the lever closes ~85 % of the
gap with pure safe-Rust integer code and no tiny-skia in the curve hot path;
the residual is bounded and named. The full-frame 60 fps story was never the
M4 plan anyway — the F3 dirty-rect path is ~8× cheaper per step and composes
with Draft (Draft is the per-pixel-cost half; dirty-rects the per-frame half).

> Note on the icount resolution: the SYS_CLOCK tick is 1 cs = 10⁷ insns, so
> these counts quantize to ±10 M/frame÷4 frames = ±2.5 M/frame. v1 reads 24 cs
> (60 M) here; the original v1 doc recorded 23 cs (57.5 M / 27 %) — the same
> ±1 cs wobble the doc flagged. The v2 8 cs (20 M) is a 16-cs drop, far outside
> the quantization, so the 3× insns reduction is real, not a tick artefact.

Reproduce: `./dev.py qemu-m4 --draft` (Draft) vs `./dev.py qemu-m4` (Exact);
host deltas `./dev.py bench` (the `F16 Draft …` log lines + the
`scene/ir_full_{exact,draft}` / `prim/fill_rect0_{exact,draft}` baseline
rows).

### Draft profile — where the 20M goes (#16, profile-first) — 2026-06-12

Before attacking the last 2× to LVGL, we PROFILED Draft to find exactly where
the instructions land — and the answer was not where the prose above guessed.
Two callgrind runs on the 480×270 M4 `FIXTURE_IR` scene (the vyr-callgrind
driver, 20 full-frame renders in one process so per-function totals are
render-path-dominated; `scripts/draft-profile.py`, x86 Ir, `--cache-sim=no`):

**Per-FUNCTION instruction breakdown (Draft, the top consumers):**

| Ir share | function | what it is |
|--:|---|---|
| **58.9 %** | `finish_into_rgb888` | **per-band demul + RGB888 convert over the GUTTER pixmap** |
| 13.1 % | `__memset_avx2` (libc) | the per-band `Pixmap::new` zero-fill of `(w+16)(h+16)·4` + the span fills |
| 6.4 % | `glyph_run` | the four text runs (already integer source-over) |
| 3.1 % | `fill_rrect` | the **radius>0 fills still on the tiny-skia path** (frame, slider/progress tracks+fills, toggle body, circle) |
| 2.1 % | `tiny_skia::fill_path_impl` | the float rasterizer those radius>0 fills feed |
| ~5 % | `tiny_skia::pipeline::lowp::*` + `alpha_runs` + `blit_*` | the AA coverage pipeline for the radius>0 residue |
| 0.9 % | `draft_span` | the integer span writer (disc/ring/line/rect fast path) — cheap, as designed |
| 0.6 % | `ring` / `disc` | the integer curve fast paths — cheap |

The headline finding: **`finish_into_rgb888` is 59 % of Draft's instructions**
— and its absolute cost is **IDENTICAL in Exact and Draft** (46,738,560 Ir over
20 frames in BOTH profiles; it is tier-independent per-pixel demul+convert). In
Exact it is 34 % of a 136M-Ir frame, hidden under the tiny-skia AA cost; once
Draft removes the AA, the demul+convert is laid bare as the dominant remaining
cost. It iterates the inner `w×h` of a `(w+2·GUTTER)×(h+2·GUTTER)` pixmap, and
the `__memset` row is mostly the per-band gutter-inclusive pixmap clear. **The
gutter is the lever**, not the radius>0 residue — exactly the STEP 3 target.

**M4 per-phase icount** (`./dev.py qemu-m4 --draft`): the single timed
quantity is the 4-warmed-frame loop — there is no finer per-phase icount split
on target (the heap phases are RAM, not insns). The host callgrind above is the
per-phase insn story; the M4 number is the cross-ISA-validated headline
(17.5 M/frame this run — 7 cs, the ±1 cs neighbour of the doc's 20 M / 8 cs).

**Per-op-class coverage** (`RenderStats`, M4 fixture): **87.4 %** of delivered
pixels take the integer fast path; the **12.6 % residue is the radius>0 fills**
(frame card, the rounded slider/progress tracks + fills, the toggle body, the
stadium circle) **plus the glyph runs and the image blit**. No gradient is on
this fixture's hot path (no widget lowers to `fill_linear_gradient` — only the
hand-built `demo_scene` bench does), so the integer-gradient work is for
coverage completeness, not this scene's number.

**What the profile says to do, in order of leverage:**
1. **Drop the gutter for Draft** (kills most of the 59 % + 13 % = ~72 % that is
   gutter-pixmap demul/convert/clear). The big lever.
2. **Route radius>0 fills through integer rounded-rect spans** (kills the 3.1 %
   + 2.1 % + ~5 % AA-pipeline residue, and lifts coverage toward 100 %).
3. Gradient → integer (coverage completeness; not on this fixture).
Glyphs (6.4 %) are already integer; bilevel masks are a separate future knob.

### Draft v3 — FULLY integer + gutter-less, re-measured vs LVGL (#16) — 2026-06-12

Acting on the profile, v3 closes the last two tiny-skia paths and drops the
gutter:

- **radius>0 `fill_rrect` → integer rounded-rect spans** (`rrect_row_span`): the
  middle rows are full-width spans, the corner rows carve the ends with the
  SAME integer circle test the disc uses (`dx = isqrt(rad² − dy²)`) — hard
  rounded corners, no AA. Covers the frame cards, the rounded slider/progress
  tracks + fills, the toggle body, the stadium circle, plus translucent fills.
- **rounded `stroke_rrect` → integer annulus** (outer rounded-rect spans minus
  inner, same centred-stroke geometry as the Exact lowering) — the rounded
  borders.
- **`fill_linear_gradient` → integer per-pixel linear ramp**
  `round((1−t)·from + t·to)`, `t` a 1/256 fixed-point ramp across the rect
  (deterministic, no float per pixel; the rounded shape reuses the carve).
- **gutter dropped for Draft**: the pixmap is exactly `area.w × area.h` (no
  `2·GUTTER` overscan) because no Draft op touches tiny-skia ⇒ no AA fringe to
  bound. `GUTTER` is now **Quality-conditional** (8 for Exact, 0 for Draft);
  Exact's gutter and its goldens are UNCHANGED (verified byte-identical).

After v3 the Draft tier touches tiny-skia **nowhere** on the paint path.
Band-equivalence under the gutter-off is PROVEN byte-identical (full-frame vs
even-30 + uneven-17 bands) on DEMO_IR, on the gradient/rounded `demo_scene`,
AND on the M4 text+image+widgets fixture (the workload host leg:
`full-frame fnv1a == banded`). The integer-span argument holds: a span clipped
at a band edge writes the same bytes with or without the gutter (no AA to bleed).

**The headline, same vehicle, same 480×270 banded frame, same icount clock**
(20 warmed frames now, so the icount quantizes to ±0.5 M/frame — finer than the
v1/v2 4-frame ±2.5 M):

| tier | insns/frame | insn/px | fast-path coverage | heap peak (M4) | cross-ISA hash |
|---|---|---|---|---|---|
| **Exact** (default, oracle) | **~75 M** | 598 | 0.0 % | 94 KB | M4 == x86-64, banded == full |
| Draft v1 (rects only) | 60 M | 463 | 80.9 % | 94 KB | ✓ |
| Draft v2 (+ integer curves) | 20 M | 154 | 87.4 % | 94 KB | ✓ |
| **Draft v3** (fully integer + gutter-less) | **13.0 M** (26 cs / 20) | **100** | **96.8 %** | **60 KB** | ✓ (`0xf98cbbdddd6da1ba`) |
| _LVGL anchor_ (the gap target) | _~10 M_ | _~77_ | — | — | — |

- **Draft v3 = 13.0 M insns/frame = 1.30× the LVGL ~10 M anchor.** It does NOT
  quite MEET LVGL — the honest read — but it recovers **95 % of the Exact→LVGL
  gap** (62 M of 65 M insns/frame), up from v2's 85–88 %. v3 is **5.77× the
  Exact frame** (75 M → 13 M).
- **Coverage 96.8 %** of delivered pixels via integer spans; the remaining
  3.2 % is the text glyph runs + the image blit, which are ALSO integer (a
  hand-rolled source-over, never tiny-skia) — they just aren't counted into
  `fastpath_pixels` (which tracks the span-fill ops). So tiny-skia carries
  **0 %** of the Draft frame.
- **Heap peak fell 94 KB → 60 KB on the M4** — the gutter-less band pixmap is
  `480×16×4 = 30,720 B` vs the gutter pixmap `63,488 B`, a 32 KB working-set win
  on top of the speed.

**Host micro-numbers** (`./dev.py bench`, x86-64 release): the raw opaque
radius-0 lever is **25.5× faster per pixel** (0.82 → 0.03 ns/px). The blended
DEMO_IR scene is now **5.41× faster** (Exact 5.87 → Draft 1.09 ns/px; was 3.65×
at v2). **Fidelity delta Draft-v3 vs Exact on DEMO_IR: 766/14400 px differ
(5.3 %), max channel error 220/255** — DOWN from v2's 7.6 %, because the rounded
corners now match the Exact corner SHAPE (only the AA fringe of the rounded
edges differs, not the whole square the v2 fallback drew). Every differing pixel
is an antialiased edge Exact smooths and Draft's hard edge does not — the
gutter-off changed ZERO pixels (band-equivalence proves it), only the integer
rounding did, and it improved fidelity.

**Honest read — does vyr meet/beat LVGL on the M4 now?** Not yet: **13.0 M vs
~10 M = 1.30×**. But the gap is no longer AA, no longer tiny-skia, and no longer
the gutter — it is the irreducible-ish per-pixel work every software renderer
pays, named:

1. **`finish_into_rgb888` (demul + RGB888 convert)** — 59 % of Draft's x86
   insns. vyr renders into a PREMULTIPLIED RGBA8888 band then demultiplies +
   converts to RGB888 in a second pass; LVGL writes its native format (RGB565/
   RGB888) straight from the blend with no premultiply round-trip. Fusing the
   convert into the span write (or rendering Draft directly in RGB565/RGB888, an
   F13 pixel-format axis) is the next single biggest lever — and it is squarely
   the "own fixed-function painter tier behind the Canvas trait" the F9 verdict
   already scoped, now reduced to one fused output path, not a rewrite.
2. **Glyph blits** — already integer; bilevel (1-bpp) masks (a Draft glyph knob,
   #16) would shrink the per-pixel cost further.

The read stands: **the integer fast path + gutter-off are most of the answer
(95 % of the gap, pure safe-Rust integer code, zero tiny-skia), and the last
1.3× is a single fused-convert / native-pixel-format step, not a from-scratch
rasterizer.** And the M4 plan was never full-frame 60 fps anyway — the F3
dirty-rect path is ~8× cheaper per step and composes with Draft (Draft = the
per-pixel-cost half, dirty-rects = the per-frame half).

> Note on resolution: v3 reads **26 cs / 20 frames** (finer than v1/v2's
> 4-frame loop; `TIMED_FRAMES` bumped to 20 here + in dev.py). At 4 frames the
> same build quantized to 5 cs = 12.5 M (the optimistic edge of its ±2.5 M
> band); 20 frames gives 13.0 M ±0.5 M — the trustworthy figure. The 13.0 M is
> a 7-cs-equivalent drop from v2's 20 M, far outside quantization.

Reproduce: `./dev.py qemu-m4 --draft`; per-function profile
`python3 scripts/draft-profile.py`; host deltas `./dev.py bench`.

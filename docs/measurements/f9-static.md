# F9 static numbers — flash + static RAM on thumbv7em-none-eabihf (#9)

**Status:** measured 2026-06-11 (toolchain `1.95.0`, pinned `Cargo.lock`).
This is the STATIC half of the F9 embedded spike: what the renderer costs in
flash and link-time RAM on an STM32F427-class M4F. The DYNAMIC half
(on-target ns/px, band size vs frame time, RGB565 convert cost, glyph-cache
boot fill) stays open pending the board decision — see "What remains" below.

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

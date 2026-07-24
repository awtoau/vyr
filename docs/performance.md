# vyr — performance model and measured numbers

**Status:** snapshot, 2026-07-24, commit `5482dad`. **Every number here is
perishable.** They were taken at specific commits with specific tools, and the
renderer changes under them. Treat the *model* as durable and the *figures* as
expiring — §6 gives the exact command to regenerate each one. A number quoted
from this file without re-running its command is a claim about the past.

**The live version of every figure below is the ledger**,
[`docs/perf/index.html`](perf/index.html), rebuilt from
[`history.jsonl`](perf/history.jsonl) — one row per measured commit, one
instrument, full per-cell provenance.

**Chronology:** [`measurements/perf-history.md`](measurements/perf-history.md) records how
every number here was arrived at — including the **four measurement errors**
found so far, each of which flattered vyr. Read it before trusting any figure
in this file.

This document exists because the project spent a long time quoting a
performance figure that was not measuring what it claimed. The countermeasure
is not better numbers, it is written-down provenance (§5).

---

## 1. The partial-framebuffer model

Invariant **I1** — *"partial framebuffer is the only code path"* — is the single
most load-bearing performance decision in vyr, and it is an architectural one,
not an optimisation. There is no separate full-frame renderer that could drift
from the banded one:

```rust
render(tree, area: Rect, buf: &mut [u8], stride: usize)
```

`area` is an arbitrary rectangle. Full-frame rendering is not a special case of
this — it is just the call where `area` happens to be the whole screen.

### 1.1 Banding and partial rendering are NOT the same thing

They are routinely conflated, including in this project's own discussions, and
the distinction decides what a display path costs.

| | Banding | Partial rendering |
|---|---|---|
| What it is | successive full-width strips covering the **whole** frame | render only an **arbitrary sub-rect** |
| What it saves | **memory** — a 480x270 frame renders through an 11.5 KB buffer | **work** — untouched pixels are never rendered or transferred |
| Frame coverage | 100 %, in pieces | only what changed |
| API | repeated `render()` calls with successive `area` strips | `dirty_rects(prev, next)` → `render()` per rect |

Banding is why vyr fits on a part with 192 KB of SRAM at all: a full 480x270
RGB888 gutter pixmap is 567,424 B and cannot exist there. Partial rendering is
why it can animate. **Banding alone still pays full-frame cost every frame.**

`vyr_core::dirty_rects(prev, next)` diffs two IR `Request`s and returns the
screen regions that actually changed. `vyr-core/tests/dirty_rects.rs` proves an
incremental repaint is byte-identical to a full render — and that was
re-confirmed on real silicon (§4.3), not only on the host.

### 1.2 Presentation modes

I1 constrains renderer internals, **not display topology**. The presentation
strategy belongs to the caller, and one entry point serves every standard
embedded layout (see [`plan.md`](plan.md) §2):

| Mode | How the caller uses it | Cost note |
|---|---|---|
| **Single full framebuffer** (LTDC scanning SDRAM) | `buf` = the framebuffer, `area` = the dirty rects | scan-out is hardware DMA and free to the CPU; only dirty pixels are *written*. **Measured against the row below in §4.4** |
| **Double buffered** (two FBs, pointer swap on vsync) | `buf` = the BACK buffer | **the trap** — see below |
| **Partial + flush** (small working buffer, per-band flush) | bands through a working buffer | the small-SRAM mode; what the DISC1 does today |

Both resident-framebuffer rows have now been measured on silicon against each
other, same scene, same tier, same clock — **§4.4**. Direct rendering needed
**no vyr-core change**: `buf` was always the caller's memory and its origin was
never assumed to be the screen's, so pointing it at the FMC aperture is purely
a caller decision.

**The double-buffer trap (the TouchGFX SMOC lesson).** The back buffer is *two
frames stale*, so dirty-rect mode must repaint the union of **this** frame's and
the **previous** frame's dirty regions, or copy forward. Updating only the
current dirty rects into an alternating back buffer leaves pixels from two
frames ago on screen. Single-buffered LTDC and GRAM-holding panels do not have
this problem. This bookkeeping lives in the flush/presentation layer — **never
in the painter**.

### 1.3 Consequence for display hardware

Because partial rendering is universal, the choice of display path is *not*
"static versus animated". It reduces to a single question: **how expensive is
writing one dirty pixel?**

- **GRAM-holding panels** (ILI9341 over SPI): the controller retains untouched
  pixels, so **no framebuffer is needed anywhere**. Cost = dirty pixels x wire
  time. This is the most memory-elegant fit for I1.
- **LTDC / parallel RGB**: the panel has no memory, so a framebuffer must be
  resident and hardware re-scans it continuously. That scan costs the CPU
  nothing; only dirty-pixel *writes* cost. Needs SDRAM.

---

## 2. Measurement hierarchy — what to trust

Ranked by trustworthiness. **Do not mix tiers in one comparison.**

| Tier | What it measures | Deterministic? | Use for |
|---|---|---|---|
| **Real silicon cycles** (DWT_CYCCNT) | actual F429 cycles incl. flash wait states, ART, bus contention | yes — 0 spread over 5 runs | the honest on-target cost |
| **QEMU plugin insn counts** (`libinsn`) | architectural instructions executed | yes — bit-identical under host load | algorithmic cost, cross-firmware comparison |
| **Host bench** (`vyr-bench`, ladder) | ns/px on x86-64 | yes, statistically | regression gates, scaling laws |
| ~~**qemu SYS_CLOCK**~~ | **host wall time wearing an instruction-count costume** | **NO** | **nothing** — the story is error 1 in [`measurements/perf-history.md`](measurements/perf-history.md) |

**One instrument, one entry point.** Every figure in the first two rows comes
from `scripts/perf-harness.py`, which fills a matrix of platform × tier ×
opt-level and records, per cell, what it could NOT measure and why. History is
the same instrument replayed (`scripts/perf-replay.py`) — an old commit is
measured with today's tools, never with the tooling that shipped beside it.

---

## 3. Numbers — instruction counts (QEMU + `libinsn` plugin)

480x270 scene, `netduinoplus2` (STM32F405/M4F), warmed steady state.
Independently verified: bit-identical across repeats and under host load;
doubling the frame count gives a marginal cost with **remainder exactly 0**;
fixed window overhead 7-11 instructions (the clock-read call itself).

**Measured 2026-07-24 at `5482dad`** by `scripts/perf-harness.py` — one
instrument, one session, every firmware, and the same figures replayed over the
whole history into [`perf/history.jsonl`](perf/index.html).

> ### Quote the RENDER-ONLY column, and check how it was obtained
>
> A benchmark that hashes its own output inside the timed window is measuring
> itself. That fold is a roughly fixed cost, so it inflates a cheap frame far
> more than an expensive one — it was 6.1 % of vyr Exact and **36.1 % of vyr
> Draft, against 54.7 % of LVGL** — and a "total" comparison between a big
> renderer and a small one is therefore substantially a comparison of two FNV
> loops. Since **#44** — now on BOTH sides — the fold runs in an *untimed
> verification pass* that must reproduce the reference hash before any timed
> pass starts. vyr skips the fold behind a flag in its timed loop; LVGL's
> `flush_cb` does the same. **Render-only is structural on both sides, with
> nothing subtracted.** Each harness then runs a SECOND timed pass *with* the
> fold, so `total` and `fold` are measured in the same cell rather than carried
> over from another tier, optimisation level or compiler. Every ledger cell
> still records `fold_provenance`, which now reads `structural` throughout.

`insn/px` is **instructions per DELIVERED pixel** (`insns / 129,600`) on every
row — the normaliser LVGL's row always used (`measurements/lvgl-gap.md` §0.2).

| Firmware (`opt-level="z"`, shipped) | total | harness fold | **render only** | insn/px | vs LVGL |
|---|--:|--:|--:|--:|--:|
| vyr **Exact** | 48,239,550 | 0 *(absent by build)* | **48,239,550** | 372.2 | **14.96x** |
| vyr **Fast** (#27) | 33,508,475 | 0 *(absent by build)* | **33,508,475** | 258.6 | 10.39x |
| vyr **Draft** | 5,511,165 | 0 *(absent by build)* | **5,511,165** | 42.5 | **1.71x** |
| LVGL 9.6.0-dev (`62f343b54`), content-corrected (`-Os`) | 7,112,592 | 3,888,188 *(measured, same cell)* | **3,224,404** | 24.9 | 1.00x |

Draft is **8.75x** cheaper than Exact; Fast recovers only **25 %** of the
Exact→Draft gap — see §3.1. **vyr Draft costs 71 % more than LVGL per frame
while drawing less** (no AA, square corners). The old "Draft is 4.6 % cheaper
than LVGL" compared two totals, i.e. substantially two FNV loops.

> **Same provenance on both sides (#44 complete).** Nothing in this table is a
> subtraction across builds. Both harnesses are measured by the same instrument
> — qemu + the `libinsn` TCG plugin, exact instruction counts — in the same
> session, each reporting `render_only` / `total` / `fold` from two timed passes
> of the same binary.
>
> The structural figures agree with the differential ones they replace to
> **101 instructions in 3.2 M** (LVGL render-only) and **50 instructions in
> 3.9 M** (the fold), and with `scripts/m4-attribute.py`'s independent
> per-symbol attribution of `flush_cb` to **373 in 3.9 M**. vyr's fold is
> **3,110,433 on all three tiers** — bit-identical, as a fixed cost per output
> byte must be, which is itself a check that the flag is doing what it claims.
>
> Reproduce: `python3 scripts/fold-split-check.py`.

### 3.0 `opt-level` is a dimension, not a decision

`release-mcu` ships `opt-level="z"` (`-Oz`: no inlining, machine outliner on);
LVGL's anchor builds `-Os`. Which level to *ship* depends on the part, the
flash budget and the compiler — it is a deployment choice, so the ledger
records all four rather than blessing one. **Render only**, same instrument,
same commit:

| tier | `z` (shipped) | `s` | `2` | `3` |
|---|--:|--:|--:|--:|
| Exact | 48,239,550 | 30,165,894 | 23,100,873 | 22,328,886 |
| Fast | 33,508,475 | 19,692,171 | 14,528,101 | 14,330,940 |
| Draft | **5,511,165** | 4,568,990 | **4,277,394** | 4,268,521 |
| Draft vs LVGL render-only | 1.71x | 1.42x | **1.33x** | 1.32x |
| *(vyr's fold, when it was still in the window)* | *3,110,4xx* | *2,721,65x* | *2,235,7xx* | *2,235,7xx* |

**A fold figure is only valid for the cell it was measured in** — the last row
is why. Before #44 took it out of the window, the fold's own cost moved with
the optimisation level by 39 %, so a render-only number scaled from another
level was arithmetic, not measurement. Concretely: reading *totals* at
`opt-level=2` before #44 gave Draft 6,513,113 against LVGL's 7,112,541 and the
tempting headline "vyr wins by 8 %"; render-only says Draft is **33 % dearer**.
That is error 4 recurring in a new guise — caught by the schema, which now
records how every render-only figure was obtained. Flash price and the full
matrix: `measurements/lvgl-gap.md` §0.3
([#33](https://github.com/awtoau/vyr/issues/33)).

> **Fidelity caveat — do not quote the above without it.** Draft has **no
> anti-aliasing** and draws `radius > 0` **square**. Fast and Exact both
> anti-alias curves; on this fixture their output is **byte-identical**
> (0 differing pixels). The two scenes are **still not content-identical**:
> the LVGL harness uses its own theme colours for slider tracks and knobs and
> its own Montserrat font, and its arc sits half a pixel inward. Those are
> listed in `scripts/lvgl-m4-bench/compare.md`; until they are closed, treat
> any vyr-vs-LVGL ratio as indicative. Tracked in
> [#27](https://github.com/awtoau/vyr/issues/27).

### 3.1 What the `Fast` tier costs, and why it is not near Draft

`Quality::Fast` (#27) keeps Draft's integer span fills for everything
straight-edged and routes only CURVED geometry — disc, ring/arc, rounded-rect
and rounded-stroke corners — back through the same tiny-skia polygon path Exact
uses. It buys the quality outright and almost none of the speed:

| | Exact | Fast | Draft |
|---|--:|--:|--:|
| M4 insns/frame, render-only (`5482dad`, `opt-level="z"`) | 48,239,550 | 33,508,475 | 5,511,165 |
| M4 heap peak / stack high-water | 112,473 / 19,044 B | 114,873 / 19,044 B | 82,881 / 19,044 B |
| host ns/frame, 480x270 panel (`vyr-bench scene/panel_*`) | 256,816 | 242,939 | 59,390 |
| blend px in the gauge region (12,100 px) | 567 | **567** | 0 |
| differing px vs Exact, 480x270 | — | **0** | 3,408 |
| M4 band heap, 480x16 | 63,488 B pixmap | 46,848 + 23,040 B | 30,720 + 23,040 B |

The reason is measurable and not subtle: **tiny-skia's anti-aliased fill costs
roughly 1.3k M4 instructions per curve pixel**, and the fixture's curve area
(~17,000 px of 129,600) is therefore worth more than Draft's entire frame. A
host profile of `scene/panel_fast` puts 40 % of the frame in tiny-skia and 15 %
in the scratch round-trip that composites its output into Draft's RGB888 band.
Sending tiny-skia less geometry helps a lot — the rounded rects are cut at
integer scanlines into four AA corner squares plus integer spans, which takes a
456x44 radius-8 frame from 20,064 AA pixels to 256 — but the ring and the discs
have no flat part to cut away, and they dominate.

**A tier that anti-aliases curves through tiny-skia cannot be near Draft's
cost.** Getting there needs a coverage-aware integer curve rasteriser obeying
the same 1/64-px quantization contract — a separate piece of work, not a
routing change.

### 3.2 Why the measurement is a MATRIX

The axes are **platform** (x86-64 host · qemu M4 · real F429 · armv7 musl),
**tier** and **opt-level**. Word size and float capability are *properties of a
platform*, not free axes, so every cell records them:

| platform | word | float |
|---|--:|---|
| host | 64 | hardware f32 **and** f64 |
| qemu-m4 / board | 32 | hardware f32 only (VFPv4-SP); **every f64 op is software** |
| arm32 (musl) | 32 | hardware f32 and f64 (VFPv3-D16) |

That third column is not trivia. **#32's soft-`f64` trig bill — 11.4 M
insns/frame, 17.7 % of Exact — hid for months because x86-64 computes `sinf`
and `cosf` in hardware and the M4F does not.** The host bench moved 8.2 % where
the M4 moved 20.3 %; nobody had a view that put the two side by side, so a 2.5x
divergence read as noise. In a matrix that divergence is a *cell*, and a row
where one platform's column moves and another's does not is a platform
pathology by construction.

The corollary is a rule: **optimisation targets are per-platform.** There is no
point optimising transcendentals for x86 — it has them in hardware. The
platform that pays is the one to measure on.

The arm32 column earns its place cheaply: it is 32-bit like the M4 but has
hardware `f64`, so it separates "32-bit" from "no double". It also carries the
frame hash, which is the cross-ISA determinism proof — Exact
`0x24dcaff531c6eb01`, Fast `0x930d03610b07ea6f`, Draft `0xf98cbbdddd6da1ba`,
**byte-identical on x86-64, armv7 and the emulated M4**.

---

## 4. Numbers — real silicon (STM32F429I-DISC1)

F429ZI at a debugger-verified 180 MHz (HSE crystal → PLL, 5 flash wait states,
ART prefetch + I/D cache), DWT_CYCCNT.

### 4.1 Per-frame render cost, 480x270

| Tier | cycles/frame | ms @180 MHz | heap peak | cycles/insn |
|---|--:|--:|--:|--:|
| Exact | 112,328,558 | 624.05 | 106,409 B | **1.750** |
| Draft | 12,609,945 | 70.06 | 82,881 B | **1.487** |

Zero spread across 5 freshly-flashed runs. That is genuine but unremarkable:
single-threaded, interrupts never enabled, no DMA, deterministic ART — there is
no noise source.

**The cycles/insn column is the value of having a board at all.** It is the cost
emulation cannot model. Exact pays more per instruction than Draft because
tiny-skia's float coverage pipeline is less cache- and prefetch-friendly than
Draft's integer span fills.

Frame hash `0x24dcaff531c6eb01` is **identical on x86-64, emulated M4 and
physical silicon** — determinism verified across three ISAs, not asserted.

### 4.2 Dirty-rect animation over SPI (240x320 ILI9341, SPI5 @ 5.625 MHz)

*Agent-reported; the fps figures below have not been independently re-derived.
The two totals marked verified were checked against the raw JSON.*

| Tier | Scene | dirty % | render | flush | total | fps |
|---|---|--:|--:|--:|--:|--:|
| Draft | readout | 6.65 % | 8.2 ms | 14.6 | 26.0 | 38.5 |
| Draft | trace | 18.8 % | 15.1 | 41.1 | 59.6 | 16.8 |
| Draft | panel | 44.5 % | 43.8 | 97.5 | 145.0 | 6.9 |
| Draft | full | 100 % | 62.8 | 218.9 | 284.1 | 3.5 |
| Exact | readout | 6.65 % | 42.9 | 14.6 | 60.6 | 16.5 |
| Exact | full | 100 % | 720.7 | 218.9 | 942.0 | 1.06 |

Corroborated by a continuous 60-frame run: 8.64 s = 143.95 ms/frame, matching
the summed breakdown to 0.7 %.

**Cost tracks dirty area, exactly.** Flush measured 513-515 cycles per dirty
pixel in *every* cell. At PCLK2/16, 2 bytes x 256 cycles/byte = 512 — the wire
runs at ~99.6 % efficiency and scales linearly from 6.65 % to 100 % dirty.
Verified: full frame = 153,600 B = 218.45 ms.

Also flat, and worth knowing: IR build + parse ~2.3-2.5 ms per frame (9 % of the
fastest frame), and the dirty diff itself ~0.9-1.2 ms.

### 4.3 What this says about display paths

The intuition that "the SPI wire is the bottleneck" is **only true at Draft**:

- **At Exact, render dominates flush 3.3:1** (720 vs 219 ms full-screen). A
  faster display path cannot fix that; only rendering less can.
- **At Draft, flush dominates** (219 vs 63 ms full-screen). Full-screen motion —
  scrolling, transitions, video — stays under ~4 fps on SPI.
- **Dirty-rects fix both.** LTDC fixes only the second.

Untried levers that would change the picture before more hardware is added: ST
runs this panel at 5.625 MHz but the ILI9341's rated **write** maximum is
10 MHz (~1.8x), and the flush currently **busy-waits on TXE** — SPI DMA would
take that entirely off the core and let flush overlap the next render.

### 4.4 Presentation modes: direct-to-SDRAM vs banded-plus-flush (#45)

Measured 2026-07-24 at `5482dad`, `python3 scripts/board-present.py`
(`--features board,ltdc,present`), one flash, DWT_CYCCNT at 180 MHz. All three
tiers in one image — `Quality` is a runtime argument.

- **banded** = render a 240x16 band into **CCM**, then blit it into the RGB565
  SDRAM framebuffer converting RGB888→RGB565 on the way (what the `ltdc` leg
  does today).
- **direct** = `render(area = the band, buf = &mut fb[row_offset..],
  stride = 720)` straight into an **RGB888** SDRAM framebuffer
  (`L1PFCR = 1`, 230,400 B). No second pass at all.

Render and blit are separate DWT windows; hashing and read-back are outside
both. `dirtyN` is `dirty_rects(prev, next)` = 2 rects, 9,240 px (12 % of screen).

| tier | rect | area px | banded render | banded blit | banded total | direct render | direct total | winner |
|---|---|--:|--:|--:|--:|--:|--:|---|
| Exact | full 240x320 | 76,800 | 102,618,291 | 2,265,766 | 104,884,057 | 102,836,478 | 102,836,478 | direct, **1.9 %** |
| Exact | 96x32 readout | 3,072 | 3,568,156 | 91,280 | 3,659,436 | 3,577,034 | 3,577,034 | direct, **2.3 %** |
| Exact | dirtyN | 9,240 | 18,457,055 | 273,679 | 18,730,734 | 18,482,754 | 18,482,754 | direct, **1.3 %** |
| Fast | full | 76,800 | 79,923,718 | 2,269,073 | 82,192,791 | 79,965,963 | 79,965,963 | direct, **2.7 %** |
| Fast | 96x32 | 3,072 | 2,824,375 | 91,306 | 2,915,681 | 2,826,107 | 2,826,107 | direct, **3.1 %** |
| Fast | dirtyN | 9,240 | 16,354,029 | 273,533 | 16,627,562 | 16,412,674 | 16,412,674 | direct, **1.3 %** |
| Draft | full | 76,800 | 9,989,845 | 2,265,554 | 12,255,399 | 10,033,810 | 10,033,810 | direct, **18.1 %** |
| Draft | 96x32 | 3,072 | 882,393 | 91,205 | 973,598 | 883,928 | 883,928 | direct, **9.2 %** |
| Draft | dirtyN | 9,240 | 2,146,532 | 273,810 | 2,420,342 | 2,205,292 | 2,205,292 | direct, **8.9 %** |

**Every cell produced byte-identical pixels** — the RGB888 read back out of
SDRAM folds to the same FNV-1a as the CCM band buffer, and both equal the
SPI-to-GRAM path's `0xc8a77478f7f9055a` (Exact) / `0x8af0208ab4cbd221` (Draft).

#### Reproducibility, and what its pattern shows

Two freshly-flashed runs (`scripts/present-compare-runs.py`), worst deviation
**2,186 ppm = 0.22 %**, an order of magnitude under the smallest margin above.
The *pattern* is the interesting part:

| window | run-to-run spread |
|---|--:|
| banded **render** (writes CCM only) | **0 ppm — bit-identical, every cell** |
| direct **render** (writes SDRAM) | ≤ 192 ppm |
| banded **blit** (writes SDRAM) | ≤ 2,186 ppm |

**Only the windows that touch SDRAM move at all**, because only they share the
bus with an LTDC scan-out whose phase is not synchronised to the render. That
is bus contention observed directly, in the noise floor.

#### Why direct wins, and why the margin is a tier story

Direct removes the blit **entirely** and costs almost nothing to do so:

| | full-frame |
|---|--:|
| blit removed | 2,265,554 c = **12.59 ms**, 29.5 cycles/px |
| extra cost of writing 230,400 B to SDRAM instead of CCM (Draft) | **43,965 c**, 0.19 cycles/byte |

So the blit was never paying for SDRAM — **it was paying for the RGB888→RGB565
conversion**. The blit is a FIXED per-pixel cost, so its share of the frame is
what varies: it is 18.5 % of a Draft frame and 2.2 % of an Exact one. **The
margin tracks the tier, not the dirty fraction.**

Neither path is close to memory-bound. Achieved SDRAM write bandwidth:
12.2 MB/s in the banded blit, 0.3–4.1 MB/s across a direct render — against
108 MB/s the memory delivers to a plain store loop.

#### The memory system (same 8 KiB store/load loop, only LTDC differs)

| target | write | read |
|---|--:|--:|
| CCM | 119 MB/s | 119 MB/s |
| SDRAM, LTDC scanning RGB565 | 108 MB/s | 32 MB/s |
| SDRAM, LTDC scanning RGB888 | 104 MB/s | 30 MB/s |
| SDRAM, layer OFF (contention-free control) | 119 MB/s | 35 MB/s |
| SDRAM, 8 MiB bulk walk (`sdram_test`) | 55 MB/s | 23 MB/s |

- **CCM and SDRAM writes are indistinguishable for a resident working set** —
  posted writes are absorbed, and the store loop is CPU-bound, not memory-bound.
  The 8 MiB bulk figure is 2x lower because it crosses every SDRAM row and pays
  a row activation each time; quoting it as "SDRAM write speed" understates a
  framebuffer by half.
- **Reads are the slow direction, ~3.5x.** Any presentation scheme that reads
  the framebuffer back pays dearly (see the R/B swap below).
- **Scan-out contention is real and small**: 10 % on CPU writes with the RGB565
  layer, **14 % with RGB888** (1.5x the scan-out read traffic: 15.05 vs
  10.03 MB/s at the measured 65.330 Hz).
- **Writes during active scan-out are 9 % slower than during blanking**
  (13,544 vs 12,319 cycles for the same 8 KiB, 8 reps; the blanking figure is
  bit-identical to the layer-off control, i.e. blanking contention is *zero*).
- **No LTDC FIFO underrun or transfer error in any run**, including RGB888.

#### The catch: RGB888 out is not RGB888 in memory

LTDC's RGB888 layer reads **B at the lowest address** (RM0090 pixel-data
mapping, same little-endian order as its ARGB8888). vyr-core writes R, G, B. So
a framebuffer written directly by the renderer displays **red and blue
swapped** — the bytes are right, the channel order is not.

Correcting it in place costs **10,746,227 c = 59.7 ms** (read-modify-write over
230,400 B at 3.9 MB/s effective — read-bound). That is **4.7x the 12.59 ms blit
it was supposed to replace.** With today's RGB888-only output:

> **direct wins on cycles in all nine cells, but only if swapped red/blue is
> acceptable. Add the correcting pass and banded wins everywhere, by a lot.**

#### What #22 `OutFmt` would be worth here (not implemented)

1. **A byte-order variant alone (BGR888 out)** removes the 59.7 ms swap and
   makes the table above the whole story: direct wins outright, by 1.3–18.1 %.
   This is a much smaller change than #22 proper.
2. **RGB565 native out** additionally makes direct genuinely *single-touch*:
   2 B/px instead of 3 into a 153,600 B framebuffer, no conversion anywhere, no
   1.5x SDRAM, and scan-out read traffic back to 10.03 MB/s — worth the measured
   4 percentage points of write contention (14 % → 10 %) and 76,800 B of SDRAM.
   The conversion cost it removes is the 29.5 cycles/px the blit spends today.

#### Tearing

Single buffered, so tearing is **structural, not incidental**: a full-frame
update spans 3.6 refresh periods at Draft (55.7 ms vs the measured 15.31 ms
frame) and 37 at Exact; even the 96x32 readout at Draft (4.9 ms) is unsynchronised
with scan-out. Firmware cannot observe tearing — only a human looking at the
panel can — and no attempt is made here to hide it. Double buffering is not a
free upgrade; see §1.2's trap.

---

## 5. The rules this document exists to enforce

**The narrative lives in
[`measurements/perf-history.md`](measurements/perf-history.md)** — four
measurement errors, what each one said, and how each was found. That is
deliberately the ONE place it is told; this section keeps only the rules that
came out of it.

1. **A tool asserting determinism is not evidence of it.** Run the workload
   under host load. If the number moves, it is not counting instructions.
2. **Cross-validate the window.** Doubling the frame count must give a marginal
   cost with zero remainder. This catches a mis-attributed measurement window
   that repetition alone will not.
3. **Record provenance with the number.** An anchor whose source is not recorded
   is not an anchor — it is a rumour with a decimal point.
4. **The instrument is constant; only the renderer varies.** A commit is
   measured with TODAY's harness, never with the tooling that shipped beside
   it. `scripts/perf-replay.py` exists to make that cheap enough to actually do.
5. **Never publish a number the benchmark contributed to.** Both M4 vehicles
   hash their own output inside the timed window; `render_only`, `fold` and
   `total` are three separate recorded fields per cell so that cost can never
   again be inside a headline without being seen.

---

## 6. Regenerating every number here

**Do this before quoting anything above.** Each command writes JSON with full
provenance — tool version, source commit, ELF SHA-256, every run's raw values.

| Numbers | Command | Output |
|---|---|---|
| **§3 — the whole matrix at one ref, render-only separated from the harness fold** | `python3 scripts/perf-harness.py --ref HEAD --lvgl --stack` | `tmp/perf-harness-HEAD.json` |
| **the same matrix replayed over history** (one instrument, every specimen) | `python3 scripts/perf-replay.py` | `tmp/perf-replay.jsonl` |
| — then rebuild the ledger and the page from it | `python3 scripts/ledger.py --rebuild-from-replay tmp/perf-replay.jsonl` | `docs/perf/history.jsonl`, `docs/perf/index.html` |
| §3 instruction counts, ALL vyr tiers in one run (totals only — no fold split) | `python3 scripts/tier-insns.py --repeat 2` | `tmp/tier-insns.json` |
| §3 instruction counts, one ELF | `python3 scripts/qemu-insn.py --name <n> <elf> --repeat 3` | `tmp/qemu-insn-<n>.json` |
| §3 flag caveat — the whole `opt-level` × tier matrix (insns, flash, heap peak, stack watermark, frame hash) | `python3 scripts/optlevel-matrix.py` | `tmp/optlevel-matrix.json`, `tmp/optlevel-matrix.md` |
| §3 one tier at one `opt-level` | `python3 scripts/tier-insns.py --opt 2 --tiers exact` | `tmp/tier-insns-O2.json` |
| §3.1 / #27 fidelity plates + edge-quality numbers | `python3 scripts/fidelity-compare.py --lvgl-raw tmp/fidelity/lvgl-frame.rgb888` | `docs/quality-tiers/`, `tmp/fidelity/fidelity.json` |
| — build the plugin QEMU first | `python3 scripts/qemu-plugins-build.py` | `/mnt/2tb/git_debris/qemu-plugins-build/` |
| — LVGL comparison ELF | `python3 scripts/lvgl-m4-bench/run.py` | `tmp/lvgl-m4-result.json` |
| §4.1 silicon cycles | `python3 scripts/board-run.py` | `tmp/board-result.json` |
| §4.2 dirty-rect animation | `python3 scripts/board-anim.py` | `tmp/board-anim.json` |
| §4.4 presentation modes (direct vs banded, all tiers, memory probes) | `python3 scripts/board-present.py` | `tmp/board-present.json` |
| §4.4 reproducibility of two such runs | `python3 scripts/present-compare-runs.py A.json B.json` | `tmp/present-compare-runs.log` |
| Host ladder / ns-px | `./dev.py ladder`, `./dev.py bench` | `tmp/rig-ladder.json` |
| Register-level board debugging | `python3 scripts/board-diag.py` | `tmp/board-diag.json` |
| **Record all of the above as one dated row** | `./dev.py track` | `docs/perf/history.jsonl` + `docs/perf/index.html` |

Notes that will cost time if forgotten:

- **`libinsn.c` lives in `tests/tcg/plugins/`**, not `contrib/plugins/`.
  `contrib/plugins/` builds fine and simply has no instruction counter.
- **`--enable-capstone` is mandatory.** QEMU has no in-tree ARM disassembler, so
  without it `qemu_plugin_insn_disas()` returns nothing, `match=bkpt` silently
  matches **zero** instructions, and the plugin then segfaults on exit.
- The LVGL anchor is **deliberately unpinned** — it tracks current upstream from
  the mirror. Refresh with `git -C /mnt/2tb/git_mirror/lvgl pull --ff-only`. The
  harness records the commit it built and prints `*** NOT STOCK ***` if the tree
  is dirty or ahead of origin.
- Two boards are attached. The DISC1 is probe
  `0483:3752:0671FF484971754867174427`; scripts must select it explicitly.
- Concurrent board work must take the `tmp/.board.lock` (atomic `mkdir`) around
  flash+run. **In a linked `git worktree` a relative `tmp/` is the worktree's
  own**, so the lock must resolve to the PRIMARY checkout's `tmp/` or it
  mutexes nothing while looking exactly like it does — `scripts/board_lock.py`
  resolves this itself (`_primary_checkout`).

## 7. Open threads

| Issue | Why it matters here |
|---|---|
| ~~[#25](https://github.com/awtoau/vyr/issues/25)~~ | **closed.** The two parallel ledgers are now one: `docs/perf/history.jsonl` (`"schema": 3`), with the `matrix` section as the only source of an M4 instruction figure and first-class sections for §4 (`silicon`, `board_anim`). Rebuilt 2026-07-24 from a replay of the whole history by one instrument; the SYS_CLOCK-era numbers are deleted rather than relabelled. |
| [#27](https://github.com/awtoau/vyr/issues/27) | partly addressed: the `Fast` tier exists and matches Exact's edge quality, but at 4.4x Draft's cost; the LVGL harness's checker and gauge are content-matched, its theme colours and font are not |
| [#30](https://github.com/awtoau/vyr/issues/30) | LTDC+SDRAM — weakened by §4.3, not eliminated |
| [#29](https://github.com/awtoau/vyr/issues/29) | scaling: unresolved, and constrained by byte-exact band equivalence |
| [#31](https://github.com/awtoau/vyr/issues/31)–[#36](https://github.com/awtoau/vyr/issues/36) | **fixed on both sides.** The `insn/px` normaliser mismatch is gone (every row is per *delivered* pixel) and #44 removed the fold from the timed window in vyr AND in the LVGL harness, so every §3 row is render-only by construction — no subtraction, no mixed provenance. Full attribution in [`measurements/lvgl-gap.md`](measurements/lvgl-gap.md) |
| [#44](https://github.com/awtoau/vyr/issues/44) / [#45](https://github.com/awtoau/vyr/issues/45) | verification is a **build type**, not a field to subtract: #44 (closed) moved the fold to an untimed pass in both harnesses and added a second timed pass so `total`/`fold`/`render_only` are all measured per cell; #45 would remove the fold from the perf binary entirely. The ledger already carries `build_type` and `fold_provenance` per cell so the two eras stay comparable — and so a derived value can never be mistaken for a measured one. |
| [#33](https://github.com/awtoau/vyr/issues/33) | **`opt-level` is a permanent matrix DIMENSION, not a settled default.** All four levels are measured and recorded every run — see [`measurements/lvgl-gap.md`](measurements/lvgl-gap.md) §0.3 for the numbers. `release-mcu` ships `"z"` as the value that fits the smallest part the plan contemplates, but that is a **starting point, not a verdict**: the right level is a per-application choice made from the matrix at deployment time, and `--config profile.release-mcu.opt-level=…` reproduces any column in ~12 s. Pixels, heap peak and stack depth are opt-level-invariant across all 24 builds, so the M4 gate holds at every level. |

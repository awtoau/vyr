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
| **Single full framebuffer** (LTDC scanning SDRAM) | `buf` = the framebuffer, `area` = the dirty rects | scan-out is hardware DMA and free to the CPU; only dirty pixels are *written* |
| **Double buffered** (two FBs, pointer swap on vsync) | `buf` = the BACK buffer | **the trap** — see below |
| **Partial + flush** (small working buffer, per-band flush) | bands through a working buffer | the small-SRAM mode; what the DISC1 does today |

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
> loops. Since **#44** vyr's fold runs in an *untimed verification pass* that
> must reproduce the reference hash before the timed pass starts, so for vyr
> **total is render-only, structurally, with nothing subtracted**. The LVGL
> anchor still folds inside its `flush_cb`, so its render-only figure is
> obtained by rebuilding it with the fold folding nothing and differencing —
> in the same cell, at the same optimisation level. Every ledger cell records
> which of the two it was (`fold_provenance`).

`insn/px` is **instructions per DELIVERED pixel** (`insns / 129,600`) on every
row — the normaliser LVGL's row always used (`measurements/lvgl-gap.md` §0.2).

| Firmware (`opt-level="z"`, shipped) | total | harness fold | **render only** | insn/px | vs LVGL |
|---|--:|--:|--:|--:|--:|
| vyr **Exact** | 48,239,550 | 0 *(absent by build)* | **48,239,550** | 372.2 | **14.96x** |
| vyr **Fast** (#27) | 33,508,475 | 0 *(absent by build)* | **33,508,475** | 258.6 | 10.39x |
| vyr **Draft** | 5,511,165 | 0 *(absent by build)* | **5,511,165** | 42.5 | **1.71x** |
| LVGL 9.6.0-dev (`62f343b54`), content-corrected (`-Os`) | 7,112,541 | 3,888,238 *(measured)* | **3,224,303** | 24.9 | 1.00x |

Draft is **8.75x** cheaper than Exact; Fast recovers only **25 %** of the
Exact→Draft gap — see §3.1. **vyr Draft costs 71 % more than LVGL per frame
while drawing less** (no AA, square corners). The old "Draft is 4.6 % cheaper
than LVGL" compared two totals, i.e. substantially two FNV loops.

> **Mixed provenance, stated.** vyr's render-only is structural (no fold in the
> binary's timed path); LVGL's is a two-build difference. Both were taken by the
> same instrument in the same session, and the differential is validated three
> ways: it reproduces vyr's own pre-#44 numbers to **356 instructions in 48 M**
> when checked against #44's structural removal; it agrees with
> `scripts/m4-attribute.py`'s independent per-symbol attribution of LVGL's
> `flush_cb` (3,888,238 vs 3,888,561, i.e. 323 instructions in 7.1 M); and
> removing the fold moved all three vyr tiers by the same constant. It is still
> a subtraction, and it stays flagged until #44's LVGL half lands.

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
  flash+run.

## 7. Open threads

| Issue | Why it matters here |
|---|---|
| ~~[#25](https://github.com/awtoau/vyr/issues/25)~~ | **closed.** The two parallel ledgers are now one: `docs/perf/history.jsonl` (`"schema": 3`), with the `matrix` section as the only source of an M4 instruction figure and first-class sections for §4 (`silicon`, `board_anim`). Rebuilt 2026-07-24 from a replay of the whole history by one instrument; the SYS_CLOCK-era numbers are deleted rather than relabelled. |
| [#27](https://github.com/awtoau/vyr/issues/27) | partly addressed: the `Fast` tier exists and matches Exact's edge quality, but at 4.4x Draft's cost; the LVGL harness's checker and gauge are content-matched, its theme colours and font are not |
| [#30](https://github.com/awtoau/vyr/issues/30) | LTDC+SDRAM — weakened by §4.3, not eliminated |
| [#29](https://github.com/awtoau/vyr/issues/29) | scaling: unresolved, and constrained by byte-exact band equivalence |
| [#31](https://github.com/awtoau/vyr/issues/31)–[#36](https://github.com/awtoau/vyr/issues/36) | **fixed on vyr's side.** The `insn/px` normaliser mismatch is gone (every row is per *delivered* pixel) and #44 removed vyr's fold from the timed window, so §3's vyr rows are render-only by construction. **Outstanding: the LVGL harness still folds in its `flush_cb`**, so its render-only figure is a two-build difference and the cross-renderer row is mixed provenance until #44's LVGL half lands. Full attribution in [`measurements/lvgl-gap.md`](measurements/lvgl-gap.md) |
| [#44](https://github.com/awtoau/vyr/issues/44) / [#45](https://github.com/awtoau/vyr/issues/45) | verification is becoming a **build type**, not a field to subtract: #44 moved the fold to an untimed pass, #45 would remove it from the perf binary entirely. The ledger already carries `build_type` and `fold_provenance` per cell so the two eras stay comparable — and so a derived value can never be mistaken for a measured one. |
| [#33](https://github.com/awtoau/vyr/issues/33) | **`opt-level` is a permanent matrix DIMENSION, not a settled default.** All four levels are measured and recorded every run — see [`measurements/lvgl-gap.md`](measurements/lvgl-gap.md) §0.3 for the numbers. `release-mcu` ships `"z"` as the value that fits the smallest part the plan contemplates, but that is a **starting point, not a verdict**: the right level is a per-application choice made from the matrix at deployment time, and `--config profile.release-mcu.opt-level=…` reproduces any column in ~12 s. Pixels, heap peak and stack depth are opt-level-invariant across all 24 builds, so the M4 gate holds at every level. |

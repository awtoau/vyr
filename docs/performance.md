# vyr — performance model and measured numbers

**Status:** snapshot, 2026-07-23. **Every number here is perishable.** They were
taken at specific commits with specific tools, and the renderer changes under
them. Treat the *model* as durable and the *figures* as expiring — §6 gives the
exact command to regenerate each one. A number quoted from this file without
re-running its command is a claim about the past.

**Chronology:** [`measurements/perf-history.md`](measurements/perf-history.md) records how
every number here was arrived at — including the **four measurement errors**
found so far, each of which flattered vyr. Read it before trusting any figure
in this file.

This document exists because the project spent a long time quoting a
performance figure that was not measuring what it claimed (§5). The
countermeasure is not better numbers, it is written-down provenance.

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
| ~~**qemu SYS_CLOCK**~~ | **host wall time wearing an instruction-count costume** | **NO** | **nothing — see §5** |

---

## 3. Numbers — instruction counts (QEMU + `libinsn` plugin)

480x270 scene, `netduinoplus2` (STM32F405/M4F), warmed steady state.
Independently verified: bit-identical across 3 idle and 3 host-loaded runs;
doubling the frame count gives a marginal cost with **remainder exactly 0**;
fixed window overhead 7-11 instructions (the clock-read call itself).

**Re-measured 2026-07-23** — all four firmwares in one session, same tool, same
day (`python3 scripts/tier-insns.py` + one `qemu-insn.py` run for LVGL). The
previous table's figures are superseded and are kept in git history only; two
things moved under them, and both matter:

- the **`Fast` tier** landed (#27), so there are now three vyr rows;
- the **LVGL harness was corrected** (#27 Task B) — it had been drawing a
  tick-marked `lv_scale` + a value arc + a knob where vyr draws a plain ring,
  and its own grey checkerboard where vyr blits the real asset. Removing the
  content vyr never had made LVGL **23 % cheaper** (9,220,422 → 7,112,541).
  The old "vyr Draft beats LVGL by 8.05 %" was measured against an LVGL that
  was drawing more work than vyr.

| Firmware | insns/frame | insn/px | vs LVGL |
|---|--:|--:|--:|
| vyr **Exact** | 64,422,179 | 291.9 | 5.32x |
| vyr **Fast** (#27) | 49,585,035 | 230.9 | 4.21x |
| vyr **Draft** | **8,604,184** | 52.4 | **0.954x** |
| LVGL 9.6.0-dev (`62f343b54`), content-corrected | 7,112,541 | 54.9 | 1.00x |

vyr Draft costs **4.6 % fewer** instructions than LVGL on this scene (it was
8.05 % against the uncorrected harness). Draft is 5.57x cheaper than Exact;
Fast recovers only **25 %** of the Exact→Draft gap — see §3.1.

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
| M4 insns/frame | 64,422,179 | 49,585,035 | 8,604,184 |
| host ns, 480x270 panel | 243,596 | 237,973 | 58,059 |
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

## 5. The failure this document exists to prevent

For most of the project's life, per-frame cost was reported as "instructions"
derived from semihosting `SYS_CLOCK` under `-icount shift=0,sleep=off`,
multiplied by 1e7. **It was host wall time.** Fedora's stock `qemu-system-arm`
10.2.2 ships with TCG plugins disabled, so nothing was counting instructions.

Proof — identical unchanged workload:

| condition | SYS_CLOCK reading |
|---|--:|
| idle | 39-40 cs |
| host loaded (12 CPU burners) | **42, 58 cs** |

A 49 % swing from host load. An instruction count cannot do that. The LVGL
harness asserted `DETERMINISTIC (icount)`; it was wrong. vyr's own `dev.py`
warning that the same mechanism was "wall-influenced, NOT pure icount" was
right.

Consequences, all now corrected: `QEMU_M4_EXACT_INSNS` was 16.9 % high,
`QEMU_M4_LVGL_INSNS` 8.5 % high, and the headline "vyr beats LVGL by 12.8 %" was
an overstatement of ~60 % in relative terms (the true figure is 8.05 %). The
*direction* survived; every absolute number did not.

**Three lessons, in priority order:**

1. **A tool asserting determinism is not evidence of it.** Run the workload
   under host load. If the number moves, it is not counting instructions.
2. **Cross-validate the window.** Doubling the frame count must give a marginal
   cost with zero remainder. This catches a mis-attributed measurement window
   that repetition alone will not.
3. **Record provenance with the number.** An anchor whose source is not recorded
   is not an anchor — it is a rumour with a decimal point.

---

## 6. Regenerating every number here

**Do this before quoting anything above.** Each command writes JSON with full
provenance — tool version, source commit, ELF SHA-256, every run's raw values.

| Numbers | Command | Output |
|---|---|---|
| §3 instruction counts, ALL vyr tiers in one run | `python3 scripts/tier-insns.py --repeat 2` | `tmp/tier-insns.json` |
| §3 instruction counts, one ELF | `python3 scripts/qemu-insn.py --name <n> <elf> --repeat 3` | `tmp/qemu-insn-<n>.json` |
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
| ~~[#25](https://github.com/awtoau/vyr/issues/25)~~ | **closed.** The two parallel ledgers are now one: `docs/perf/history.jsonl` (`"schema": 2`), written only by `./dev.py track`, with first-class sections for §3 (`insns`) and §4 (`silicon`, `board_anim`). Every number here now has a home in a time series; re-run the §6 command, then `./dev.py track`. |
| [#27](https://github.com/awtoau/vyr/issues/27) | partly addressed: the `Fast` tier exists and matches Exact's edge quality, but at 4.4x Draft's cost; the LVGL harness's checker and gauge are content-matched, its theme colours and font are not |
| [#30](https://github.com/awtoau/vyr/issues/30) | LTDC+SDRAM — weakened by §4.3, not eliminated |
| [#29](https://github.com/awtoau/vyr/issues/29) | scaling: unresolved, and constrained by byte-exact band equivalence |
| [#31](https://github.com/awtoau/vyr/issues/31)–[#36](https://github.com/awtoau/vyr/issues/36) | **the §3 table above is measured wrong in two ways** — it counts the benchmark's own FNV fold (3.1 M insns/frame on our side, 3.9 M on LVGL's) as render cost, and its `insn/px` column divides vyr by *touched* pixels and LVGL by *delivered* ones. Render-only, LVGL is 24.9 insn/px, Draft 42.4, Exact 473.1. Full attribution, and the ranked fixes, in [`measurements/lvgl-gap.md`](measurements/lvgl-gap.md) |

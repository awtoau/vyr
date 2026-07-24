# vyr — performance history: the "why are we slow" investigation

**Chronological record.** [`performance.md`](../performance.md) holds the *model* and
the *current* numbers; [`lvgl-gap.md`](lvgl-gap.md) holds the *attribution*. This
file records **how we got here** — what was measured, when, at which commit, what
it said, and what turned out to be wrong.

It exists because the answer to "why are we slow" turned out to depend on four
measurement errors, each of which flattered vyr, and each found only by going
back and checking. **Every number in this project should be assumed provisional
until it has survived an attempt to disprove it.**

Convention: all times AEST. Every entry cites the commit it was taken at.

---

## The four measurement errors, in the order they were found

Read this section first. It is the single most useful thing in the file.

| # | Error | Effect | Found |
|---|---|---|---|
| 1 | `SYS_CLOCK` under qemu was **host wall time**, not instructions | every M4 "instruction count" before 2026-07-23 was fiction | 07-23 |
| 2 | `tier-insns.py` built `--release` (opt-level 3), not `--profile release-mcu` (opt-level z) | Exact appeared to be 37.8 M when it was 64.4 M — a fake 41 % "speedup" | 07-23 |
| 3 | The LVGL harness drew **content vyr never had** | LVGL scored 23 % dearer than it should have | 07-23 |
| 4 | Both harnesses **hash their own output inside the measured window** | LVGL's fold is 54.7 % of its frame; the comparison was mostly measuring FNV | 07-23 |

Errors 3 and 4 together **reversed the headline**: from "vyr Draft beats LVGL by
8.05 %" to "LVGL is cheaper, and by more than the raw numbers suggest".

**The pattern worth internalising:** every one of the four made vyr look better.
That is not coincidence — it is confirmation bias in what got checked. A number
that flatters you is the one to attack hardest.

---

## 2026-07-23 — the day the numbers broke

### `SYS_CLOCK` is a stopwatch, not a counter *(error 1)*

The M4 vehicle derived "instructions/frame" from semihosting `SYS_CLOCK` under
`-icount shift=0,sleep=off`, × 10⁷. Fedora's stock `qemu-system-arm` 10.2.2
ships **with TCG plugins disabled**, so nothing was counting instructions.

Proof — identical unchanged workload:

| condition | reading |
|---|--:|
| idle | 39–40 cs |
| host loaded (12 CPU burners) | **42, 58 cs** |

A 49 % swing from host load. An instruction count cannot do that. The LVGL
harness asserted `DETERMINISTIC (icount)`; vyr's own `dev.py` warning that the
same mechanism was "wall-influenced, NOT pure icount" was correct and had been
ignored.

**Everything M4-numeric before this point is discredited**, including the
`75 M` / `10 M` anchors in `dev.py` and the "Draft recovers 103 % of the gap"
claims in the milestone docs.

### Building a QEMU that can actually count

`scripts/qemu-plugins-build.py` — out of tree from the mirror, QEMU 11.0.50,
`--enable-plugins --enable-capstone`, plus `libinsn`.

Two traps, recorded because they each cost hours:

- **`libinsn.c` has moved** from `contrib/plugins/` to `tests/tcg/plugins/`.
  `contrib/plugins/` still builds and simply has no instruction counter.
- **`--enable-capstone` is mandatory, not cosmetic.** QEMU has no in-tree ARM
  disassembler, so without it `qemu_plugin_insn_disas()` returns nothing,
  `match=bkpt` silently matches **zero** instructions, and the plugin then
  segfaults on exit.

Validation that the counts are real: bit-identical across 3 idle + 3
host-loaded runs; doubling the frame count gives a marginal cost with
**remainder exactly 0**; fixed window overhead 7–11 instructions (the clock-read
call itself).

### First exact counts — and a headline that did not survive the day

| firmware | insns/frame |
|---|--:|
| vyr Exact | 64,178,227 |
| vyr Draft | 8,478,137 |
| LVGL 9.6.0-dev | 9,220,422 |

Published as **"vyr Draft beats LVGL by 8.05 %"**. Both of the numbers in that
comparison were wrong (errors 3 and 4), in opposite directions.

### F9 board half — real silicon *(commit `5e42dac`)*

STM32F429I-DISC1 at a debugger-verified 180 MHz (HSE **crystal** → PLL, 5 flash
wait states, ART on), DWT_CYCCNT.

| tier | cycles/frame | ms @180 MHz | heap peak | cycles/insn |
|---|--:|--:|--:|--:|
| Exact | 112,328,558 | 624.05 | 106,409 B | **1.750** |
| Draft | 12,609,945 | 70.06 | 82,881 B | **1.487** |

Zero spread across 5 freshly-flashed runs. Frame hash `0x24dcaff531c6eb01`
**identical on x86-64, emulated M4 and physical silicon** — three-ISA
determinism, evidenced rather than asserted.

The **cycles/insn** column is the whole value of owning a board: it is the cost
emulation cannot model (flash wait states, ART, bus contention). Exact pays more
per instruction than Draft because tiny-skia's float coverage pipeline is less
cache- and prefetch-friendly than integer span fills.

Diagnostic note: an earlier attempt hung silently because the clock init assumed
**HSE bypass** (ST-LINK MCO). Debugger evidence settled it — with `HSEBYP=0`
`HSERDY` asserts in 0.5–0.7 ms, reproduced 3×; with `HSEBYP=1` it never asserts
across 500 ms. **The board has a fitted crystal.** Also measured: the HSI→PLL
fallback runs at 182.2–182.7 MHz, i.e. **out of spec** for a 180 MHz part.

### Panel *(commits `2469624`, `153ae63`, `018b366`)*

- **SPI → ILI9341 GRAM**, no framebuffer anywhere: 218 ms full flush at
  5.625 MHz (ST's rate; the controller's write ceiling is 10 MHz).
- **Dirty-rect animation**: Draft readout 6.65 % dirty → **38.5 fps**; trace
  18.8 % → 16.8 fps. Flush measured **513–515 cycles per dirty pixel in every
  cell** — 2 bytes × 256 cycles/byte at PCLK2/16 is 512, so the wire runs at
  **~99.6 % efficiency** and scales linearly with dirty area.
- **LTDC + SDRAM**: SDRAM memory test clean over 8,388,608 B; refresh
  **measured 65.330 Hz** vs 65.331 nominal (15 ppm). Draft full-frame
  273 → 67 ms (**4.07×**); Exact only 1.35×, because render dominates flush
  45:1. Remaining 12.6 ms blit is **conversion-bound** (29.5 cycles/px
  RGB888→RGB565) — which is what DMA2D does in hardware.

Both display paths render the panel scene to the **same** hash
`0xc8a77478f7f9055a`.

### `Quality::Fast` *(commit `cb29f52`)*

Exact's anti-aliased curves, Draft's integer spans.

- **Quality: total success.** 567 blend pixels, 24 distinct values — *identical
  to Exact*; 0 differing pixels against Exact on both fixtures.
- **Cost: negative result.** Only 1.30× cheaper than Exact, 5.76× dearer than
  Draft. tiny-skia's AA fill costs ~1.3 k M4 instructions per curve pixel.
  **Anti-aliasing curves through tiny-skia cannot approach Draft's cost.**

Discovered here and load-bearing later: handing tiny-skia a whole rounded rect
was *slower than Exact*; decomposing into 4 AA corner squares + integer spans
took a 456×44 radius-8 frame from **20,064 AA pixels to 256**.

### Errors 2, 3 and 4 *(commits `56da347`, and the `lvgl-gap.md` work)*

**Error 2 — wrong build profile.** `tier-insns.py` used `cargo build --release`
(opt-level 3) while every committed anchor and the LVGL harness (`-Os`) were
size-optimised. Corrected:

| | reported | actual (`release-mcu`) |
|---|--:|--:|
| Exact | 37,832,925 | **64,422,179** |
| Fast | 29,922,023 | **49,585,035** |
| Draft | 6,787,919 | **8,604,184** |

**Error 3 — the LVGL scene drew more.** Its `lv_scale` had tick marks, 0/50/100
labels and a knob against vyr's plain ring; and it built its own grey checker
rather than blitting vyr's asset. Both sat inside the regions every number came
from. Fixed → LVGL **9,220,422 → 7,112,541 (23 % cheaper)**.

**Error 4 — the benchmark measured itself.** Both harnesses FNV-hash every
output byte *inside* the timed window:

| | fold/frame | share of frame |
|---|--:|--:|
| LVGL | 3,888,561 | **54.7 %** |
| vyr Draft | 3,110,434 | 36.2 % |
| vyr Exact | 3,110,434 | 4.8 % |

Being a fixed cost, it inflated LVGL's small number far more than vyr's large
one. Render-only:

| | published | render-only |
|---|--:|--:|
| Draft vs LVGL | 1.21× | **1.70×** |
| Exact vs LVGL | 9.06× | **19.0×** |

### The attribution *(`lvgl-gap.md`)*

**A hypothesis falsified.** "vyr pays coverage over solid interiors where LVGL
only anti-aliases edges" — disc scaling shows Exact's marginal cost has a
log-log slope of **0.91**, i.e. it doubles when the radius doubles. tiny-skia's
`SuperBlitter` already coalesces full-coverage interior runs.

**For scale: LVGL's entire AA bill is 175 k insns/frame — 1.35 insn/px.** An
integer 4×-upscaled Bresenham eighth-circle computed once per radius and cached
globally, coverage written only at the two arc crossings per row, interior as
one unmasked rect blend, no premultiplied round-trip.

Where vyr's instructions went:

| | insns/frame | note |
|---|--:|---|
| soft-`f64` trig | ~11.4 M | libm's f32 kernels evaluate in f64; M4F is single-precision |
| SIMD-shaped pipeline | ~21 M | `u16x16` shims + memcpy on a SIMD-less core |
| `opt-level="z"` vs `-Os` | ~19–27 M | we compare `-Oz` against `-Os` |
| IR string re-parse | ~1.27 M | 23 % of Draft's render cost |

Ruled out **with numbers**, so they are not re-litigated: premultiply round-trip
(1.42 M; `demultiply` is 10.0 insns/call — tiny-skia short-circuits
`alpha==255`), the gutter (believed 0.7 % — **later corrected, see below**),
per-band pixmap zeroing at Exact (1.3 %), and flattening *density* (the vertices
are not the cost, the trig per vertex is).

---

## 2026-07-24 — the first real wins

### #32 contour memo *(commit `f0a101a`)* — **the biggest win so far**

Curve flattening called `cosf`/`sinf` 4,976 times each per frame, and libm
computes f32 trig by promoting to f64 — software on an M4F, **~1,145
instructions per `cosf`** — recomputing identical values once per band, 17 times
a frame.

| tier | before | after | Δ |
|---|--:|--:|--:|
| Exact | 64,422,179 | **51,349,644** | **−20.3 %** |
| Fast | 49,585,035 | **36,618,969** | **−26.2 %** |
| Draft | 8,604,184 | 8,621,557 | +0.2 % (layout noise) |

**13.07 M recovered against 10.7 M predicted.** Mechanism confirmed per symbol:
the soft-`f64` group (`__muldf3` + `__adddf3` + `__aeabi_d*`) fell
**11,296,843 → 208,258 (−98.2 %)**.

**No pixel changed.** The memo keys on exact f32 **bit patterns** and
deliberately refuses the tempting "flatten at the origin then translate"
shortcut, because `q(cx + r·cosθ)` ≠ `cx + q(r·cosθ)` in f32. Taking it would
have forced a re-bless; refusing it got the whole win for free.

Cost: +6,064 B heap at Exact, +7,984 B at Fast (an 8 KiB budget).

**Why it hid for months:** invisible on the host, which has hardware f64. Host
bench shows only −8.2 %. That host/target divergence is the lesson — see #39.

### #38 gutter sweep *(commit `a9c8a4f`)* — **premise inverted, and a p0 found**

The issue asked whether `GUTTER = 8` was ~4× oversized, reasoning that `Fast`
measured the same rasteriser at a minimum of 2. **It is the opposite: 8 is
insufficient.**

Sweeping 6 fixtures × 120 band heights = 720 splits per row:

| overscan | failing splits | where |
|---|--:|---|
| 3 | 34 | CHART_IR only |
| 4 | 31 | CHART_IR |
| **8** | **25** | CHART_IR — **shipped** |
| 12 | 15 | CHART_IR |
| **16** | **0** | passes |

The old reasoning held on DEMO_IR + demo_scene + CLIP_IR, where the minimum
really is 3. **`CHART_IR` was not in the rig when 8 was chosen**, and it is the
binding scene.

**This is a shipped violation of band equivalence — a day-1 invariant** — filed
as **#42 (p0)**. It hid because `chart_band_equivalence` samples band heights 30
and 17, and neither is in the failing set. **Sampling cannot find this class of
defect; only sweeping can.**

And it cannot be fixed by raising the constant: largest that fits the 122,880 B
arena is 8, smallest that is correct is 16 — **no intersection**. Required
overscan tracks the vertical extent of the cut polygon, so it is unbounded in
scene geometry (a 109-row line still fails at overscan 88). The fix is
deterministic world-space polygon pre-clipping — **#40**.

**Correction to the earlier attribution:** `lvgl-gap.md` §6 priced the gutter at
~440 k insns/frame (0.7 %) by counting only the extra memset. End to end it is
**9.2 M (17.9 %)**. It was never just a RAM cost.

---

## Running scoreboard

Exact, M4, `release-mcu`, plugin-counted, **including** the benchmark's own hash
fold (see error 4 — render-only is lower):

| date | commit | Exact insns/frame | what changed |
|---|---|--:|---|
| 07-23 | pre-plugin | *(unmeasurable)* | `SYS_CLOCK` was wall time |
| 07-23 | `3e7c620` | 64,178,227 | first exact count |
| 07-23 | `56da347` | 64,422,179 | error 2 corrected (build profile) |
| 07-24 | `f0a101a` | **51,349,644** | #32 contour memo |
| 07-24 | `a9c8a4f` | 51,349,644 | gutter swept; no behaviour change |

LVGL anchor: `9,220,422` → **`7,112,541`** at `56da347` (error 3).

---

## Open opportunities, ranked *(see the issues for detail)*

| issue | worth | risk |
|---|--:|---|
| [#33](https://github.com/awtoau/vyr/issues/33) `opt-level` z→s | ~19–27 M | +198 KB flash — a product decision |
| [#40](https://github.com/awtoau/vyr/issues/40) polygon pre-clip | 9.2 M + 22.5 KB RAM | fixes the #42 p0 as well |
| [#37](https://github.com/awtoau/vyr/issues/37) narrow scalar pipeline | ~21 M | needs tiny-skia work; may be byte-identical |
| [#34](https://github.com/awtoau/vyr/issues/34) IR resolve-once | 1.27 M (23 % of Draft) | none |
| [#35](https://github.com/awtoau/vyr/issues/35) lazy Draft scratch | 0.4–0.7 M + 30 KB/band | none |

---

## Ledger discipline — how this stays true

**Every measurement must land in the ledger**, not only in a commit message or a
`tmp/*.json`. `docs/perf/history.jsonl` is the single measurement history
(schema 2, one append-only row per run, written **only** by `./dev.py track`);
`docs/perf/index.html` is regenerated from it.

The rule, after every landed change that could move a number:

```
./dev.py ci          # measures, then ends with `track`
# or, if the artifacts in ./tmp are already current:
./dev.py track
```

Three constraints that make the series trustworthy:

1. **Never record against a dirty tree.** A row's value is that it is pinned to
   a commit; a dirty-tree row is unattributable. `track` records the commit and
   a dirty flag — if the flag is set, treat the row as indicative only.
2. **A row records what was measured and nothing else.** Sections absent from
   `./tmp` are simply not recorded. **Sparse rows are honest; invented values
   are not.** No commit was ever measured by both pre-merge harnesses, so the
   early history is genuinely sparse and cannot be backfilled.
3. **A series is charted only where it has ≥ 2 observations.** Single
   observations stay in the tables. This is exactly the failure the retired
   `docs/metrics/` ledger had — seven "history" charts drawn from one row.

**Known gap:** rows exist for `3e7c620` and earlier, but `f0a101a` (the memo)
and `a9c8a4f` (the gutter sweep) are recorded here in prose and **not yet in the
ledger** — they were measured while the tree was mid-flight. They should be
backfilled by re-running the measurements at those commits, or accepted as
prose-only with the next row picking up the trend.

Provenance for every regeneration command: [`performance.md`](../performance.md) §6.

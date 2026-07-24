# The long animated scene — design, tiers, and what it found

**Added 2026-07-24.** A ~20 s continuously-changing scene (`vyr-scene`),
driven by the host rig and by the emulated-M4 vehicle from **one generator**,
so both legs animate the same pixels and their digests are directly
comparable.

The F18 rig scene it complements is a good determinism fixture and a poor
*cost* fixture: over its 600 frames the dirty fraction never leaves
**12.4 %–21.7 %**, its text is ten digits plus two fixed strings, and every
frame costs about the same as every other. Nothing in it can move the
dirty-rect cost model (`../performance.md` §4.2), pressure the glyph cache,
or churn the #32 contour memo.

---

## 1. Addressing it

The scene is versioned data, not a script: `vyr_scene::SUITE_VERSION` is
`suite-2` (#43 — adding a test is a suite bump, which obliges a replay of the
ledger's history against the new suite; the replay is not performed here).

| | |
|---|---|
| suite key | `vyr_scene::suite_key(detail, w, h, preset)` → `long-v1@480x270/full` |
| scene ids | `long-v1` (full), `long-lite-v1` (the MCU-fitting variant) |
| goldens | `vyr-rig/hashchain-long.json`, `vyr-rig/hashchain-long-lite.json` |
| host driver | `vyr-rig anim --scene long\|long-lite --preset smoke\|short\|full` |
| M4 vehicle | `vyr-size --features run-qemu,rig[,rig-lite][,rig-smoke\|rig-full]` |
| harness | `python3 scripts/rig-long.py --steps rig,host,arm,m4,series,video,report` |
| gate | `vyr-rig/tests/long_scene.rs` (smoke prefix vs the committed chains) + `./dev.py check-mcu` now covers `vyr-scene` |

```
./dev.py test                                     # 60-frame prefix, ~1.5 s
python3 scripts/rig-long.py --steps rig,report    # all presets, host, ~5 s
python3 scripts/rig-long.py --steps m4 --m4-matrix lite:full:draft   # ~140 s
```

## 2. The frame-count tiers

The frame count is a **preset**, not a constant, because 1200 frames is not
affordable everywhere and a test nobody can pay for is a test nobody runs.

| preset | frames | seconds @60 fps | what it is for |
|---|--:|--:|---|
| `smoke` | 60 | 1.00 | after every change; one whole theme block, so every animation class appears at least once |
| `short` | 300 | 5.00 | six blocks; the glyph cache visibly fills and the memo reaches steady state |
| `full` | 1200 | 20.00 | the endurance form — every driver's value set is fully visited well before the end |

The drivers' cycles are deliberately coprime (24, 25, 30, 50, 100 frames, and
279 for the ticker), so the scene does **not** repeat inside any preset and
the frame counter never repeats at all. What is asserted instead — and what
makes a long run mean something — is that every driver's **value set is
bounded** and fully visited early (13 gauge diameters, 3 panel slots, 93
ticker strings, all within 1200 frames). Past that point a longer run adds
endurance, not coverage.

### Measured wall-time cost

| leg | 60 | 300 | 1200 |
|---|--:|--:|--:|
| host x86-64, `vyr-rig` (full **+** incremental **+** byte-compare per frame) | 0.09 s | 0.40 s | **1.58 s** |
| host x86-64, `vyr-size` banded survey (per tier) | ~0.3 s | ~0.8 s | ~2.0 s |
| emulated M4, plugin-counted (`qemu-insn.py`; **2 passes** — untimed survey + timed) | 7.1 s | 34.6 s | **138.8 s** |
| STM32F429 @180 MHz, Draft, **projected** at 1.487 cycles/insn | 6.0 s | 29.8 s | **119 s** |

The board figure is a projection from the animated insns/frame and the
cycles/insn measured for the *static* Draft workload; it has not been run on
silicon.

**Exact and Fast have no M4 wall time at all — they do not run (§5).**

## 3. What the scene contains, and why

Geometry is authored in the 480×270 design space and scaled per ladder rung.
Every driver is a pure integer function of the frame index; circular motion
comes from a committed Q12 sine table (`scripts/gen-sintab.py`), never
`libm::sinf`, which costs ~1,145 M4 instructions a call and would price the
scene generator into the renderer's numbers.

| element | period | exercises |
|---|---|---|
| `vy_lcd` frame counter `F%05d` | every frame | glyph churn on ten digits; **near-static frames** (one digit ⇒ 3.5 % dirty) |
| rolling ASCII ticker (`vy_label`) | 1 char / 3 frames | **glyph-cache churn** — 93 distinct codepoints (39 in `lite`), walked completely within one period |
| root `background` cycling 4 themes | 50 frames | **full-frame repaints** (a root attr change dirties the whole screen) |
| idle tail, frames 41..49 of every block | 9 in 50 | frames where only the counter moves — the low end of the dirty distribution |
| translucent `vy_frame` panel over 3 slots | 25 frames | **translucent fills**, z-order restack, the ~50 % dirty band |
| `vy_chart` line trace (40 pts) + bar chart (12 bars) | every frame | polyline, marker discs, grid; the chart is the scene that binds the gutter work (#38/#40/#42) |
| `vy_gauge` breathing over 13 diameters | 24 frames | **contour-memo churn** (#32, 8 KiB budget) |
| 2 discs + a `fit: cover` blit orbiting a rounded `vy_container` | every frame | curves, **rounded clipping**, widgets crossing the clip edge, **scaled image blits** |
| roaming 24×24 checker at natural size | every frame | unscaled blits |
| `vy_button` + centred label, slider, progress, toggle | 100/50/30 | clip stack, ink inheritance, the cheap widget mix |

**No gradients.** `Canvas::fill_linear_gradient` exists and has Draft/Fast
fast paths, but **no IR attribute reaches it** — the only scene that uses it
is the hand-built `demo::demo_scene`. An IR-driven scene cannot exercise
gradients at all today. That is a coverage gap in the IR, not in this scene.

**No rotation or scale**, because the IR has neither (#17). Rotational motion
is the orbits, the scrolling trace, and the breathing ring.

### Detail levels

`Detail::Full` (`long-v1`) is the headline scene. `Detail::Lite`
(`long-lite-v1`) shrinks the two unbounded consumers — 39-glyph repertoire,
20-point trace, no bar chart — and is the variant that fits an F429-class
arena (§5). They are separate scenes with separate goldens, never a quality
knob.

## 4. Dirty-fraction distribution (host, 480×270, 1200 frames)

| | `long-v1` | `long-lite-v1` | *(F18 rig scene, 600 frames, for contrast)* |
|---|--:|--:|--:|
| min | 3.53 % | 3.53 % | 12.42 % |
| median | 30.40 % | 24.68 % | — |
| mean | 37.75 % | 32.69 % | 14.58 % |
| max | 100 % | 100 % | 21.70 % |

Buckets over the 1199 incremental steps:

| scene | 1–5 % | 10–25 % | 25–50 % | 50–75 % | 75–100 % | =100 % |
|---|--:|--:|--:|--:|--:|--:|
| `long-v1` | 216 | 0 | 624 | 328 | 8 | 23 |
| `long-lite-v1` | 216 | 450 | 174 | 328 | 8 | 23 |

The 216 low-end steps are the idle tails; the 23 hundred-percent steps are
the theme flips (one per block, minus the first). The whole F18 scene lives
inside a single one of these buckets.

## 5. Findings

### 5.1 Exact and Fast cannot animate this scene on an F429-class part

The 122,880 B arena is not big enough, and the failure is **fragmentation**,
not headroom.

| tier | detail | outcome |
|---|---|---|
| Exact | `lite` and `full` | frame 0 completes (heap peak **113,578 B**, live 40,339 B); frame 1's **63,488 B** gutter-pixmap allocation **fails** |
| Fast | `lite` | fails **during frame 0**, on a **5,120 B** allocation |
| Draft | `lite` | 1200 frames OK, heap peak **112,422 B** |
| Draft | `full` | dies between frames 106 and 120, on a **5,120 B** allocation, with heap peak 115,666 B and **live 49,617 B** — i.e. ~73 KB nominally free |

A 5,120 B request failing against ~73 KB of free space is fragmentation, and
per-frame IR churn is what fragments it: the static workload parses once and
holds one tree forever, so it never exercises this at all. Per-frame heap
churn against `linked_list_allocator` on a 120 KiB arena is an unmeasured
risk for any animating vyr application, not a property of this scene.

### 5.2 Heap does NOT leak — growth is bounded by the glyph cache filling

Host, `long-v1`, Exact, sampled every 60 frames:

| frame | 0 | 60 | 120 | 180 | 240 | 300 | 360 | 420 … 1199 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| heap peak (B) | 148,211 | 166,263 | 171,088 | 172,963 | 174,293 | 174,293 | 174,294 | **174,294** |
| glyph cache (B) | 5,158 | 9,235 | 11,181 | 12,513 | **13,039** | 13,039 | 13,039 | 13,039 |
| live after frame (B) | 73,244 | 80,012 | 84,836 | 86,842 | 88,037 | 88,035 | 88,041 | ~88,040 |

The peak tracks the glyph cache and stops when the ticker has walked its
whole repertoire — the last glyph is rasterized at **frame 201** for the
93-glyph `Full` and **frame 39** for `Lite`; after that the peak is **flat
for the remaining 960+ frames**. Live bytes are flat to ±6 B.
The M4 agrees: Draft/`lite` heap peak is **112,421 B at 60 and at 300 frames
and 112,422 B at 1200** — one byte over a twentyfold longer run.

So the unevicted glyph cache is bounded by the repertoire, not leaking. The
cost is that the bound is "every glyph the application ever draws", which for
a dynamic-text UI is the whole font.

### 5.3 The 8 KiB contour memo covers a static frame completely and an animated one barely

| workload | entries | bytes | hits | misses | overflow |
|---|--:|--:|--:|--:|--:|
| static fixture, Exact | 21 | 6,064 | 115 | 21 | **0** |
| `long-v1`, Exact, 1200 frames | — | 8,128 (full) | 86,050 | 44 | **579,181** |
| `long-v1`, Fast, 1200 frames | — | 8,184 (full) | 153,519 | 47 | **760,086** |
| `long-v1`, Draft, 1200 frames | — | 7,952 | 8,149 | 29 | 5,114 |

The memo fills on frame 0 and then **overflows ~483 times per frame** at
Exact: it serves the 44 contours it managed to cache and re-flattens
everything else, every band, every frame. The #32 win (−20.3 % Exact) was
measured on a scene whose contour set fits; it should not be assumed to carry
over to animation.

### 5.4 A centred `vy_label` with no declared rect is culled on a box that does not contain its own text

Found while building the scene: `vy_button` + child `{"text": …, "align":
"center"}` with no `x/y/width/height` (the shape `demo::TEXT_IR` uses) gives
the label a **0×0** paint rect at the button's top-left. Band culling
inflates that by `CULL_MARGIN = 32`, but `align: center` places the run
around the *parent's* centre — outside the cull box for any button wider than
~64 px. A dirty rect covering the caption but not the button's top-left
corner renders the button **without its caption**: `run_anim` caught it as
`INCREMENTAL != FULL` at frame 7, row 148 col 258.

The existing goldens miss it because their bands are full-width horizontal
strips, so the button's left edge is always inside the band — exactly the
"sampling cannot find this class of defect" pattern #38 recorded. The scene
declares the label's rect explicitly so it measures animation rather than
re-testing a bug; the bug itself is unfixed and worth its own issue.

## 6. Cost

### Per-frame instructions (M4, `release-mcu`, plugin-counted)

| scene | tier | frames | insns/frame |
|---|---|--:|--:|
| static fixture (HEAD) | Draft | 20 | 5,511,165 |
| `long-lite-v1` | Draft | 60 | 11,798,732 |
| `long-lite-v1` | Draft | 300 | 11,947,985 |
| `long-lite-v1` | Draft | 1200 | **12,030,298** |
| `long-v1` | Draft | 60 | 12,633,739 |

The animated frame is ~2.2× the static one. Two causes, both real: the scene
draws more, and the window **includes scene emit + `Request::parse` every
frame**, which an animating device genuinely pays (#34 puts the parse at
~1.27 M).

### Worst frame vs mean

Per-frame series, `long-lite-v1`, Draft, 1200 frames (`--features
rig-perframe`, every frame bracketed by its own clock read):

| mean | median | p95 | p99 | min | max |
|--:|--:|--:|--:|--:|--:|
| 12,030,302 | 12,245,696 | 13,156,732 | 13,750,702 | 10,214,968 (frame 100) | **13,924,580 (frame 877)** |

Worst / mean = **1.157×**; worst / min = 1.363×. The series' mean agrees with
the independently-measured aggregate window (12,030,298) to 3 parts in 10⁷,
which is the cross-check that the window was sliced correctly.

Note the M4 renders **full frames**, so this spread is content-driven only.
The dirty-rect spread that matters for a real runtime is the §4 distribution
— 3.5 % to 100 % of the screen, i.e. ~28× between the cheapest and dearest
repaint.

## 7. Determinism

One digest covers everything. The 1200-frame chains are:

| scene | run hash |
|---|---|
| `long-v1` | `0x6c2f38424d2959d9` |
| `long-lite-v1` | `0x060ff905e4448c56` |

and they are produced identically by:

- **x86-64, full-frame render, full `roboto.ttf`** (`vyr-rig`),
- **x86-64, 480×16 banded render, ASCII subset font** (`vyr-size` host leg),
- **armv7 musl under `qemu-arm-static`** (`vyr-rig`, all 1200 frame hashes
  and the run hash byte-identical),
- **emulated Cortex-M4, banded, `linked_list_allocator` arena**
  (`vyr-size --features run-qemu,rig`, Draft: `0x18687fc4d1b75314` for
  `lite`/Draft, matching the host's Draft chain exactly).

A divergence names its frame: `HashChain::diff` reports the first differing
frame index, and the M4 leg prints a per-sample frame hash so a chain
mismatch can be bisected without re-rendering on the host.

Existing goldens are untouched: static frame hashes are still Exact
`0x24dcaff531c6eb01`, Fast `0x930d03610b07ea6f`, Draft `0xf98cbbdddd6da1ba`,
and `./dev.py anim` still matches `vyr-rig/hashchain.json` over its 600
frames.

## 8. Artifacts

`./tmp` only (the committed regression form is the hash chain):

- `tmp/rig-long-stills/frame-*.png` — 24 stills, every 50th frame
- `tmp/rig-long-filmstrip.png` — 6-frame filmstrip
- `tmp/rig-long-full-480x270-rgb888-60fps-ffv1.mkv` — the 20 s lossless
  video (+ `.json` spec sidecar; frame-0 roundtrip verified)
- `tmp/rig-long.json`, `tmp/rig-long-series-*.json`,
  `tmp/rig-long-insn-series-*.json` — the series behind every table above
- `tmp/rig-long.log` — the run log

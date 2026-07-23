# vyr — plan of record

**Status:** plan of record (2026-06-11). Name **vyr**; license **GPL-3.0-only +
commercial** (see [`LICENSING.md`](../LICENSING.md)). Originated in
awto-vyvanse (`docs/ir-reference-renderer-plan.md` there is now a pointer
here). Each feature track F1–F14 maps 1:1 to a GitHub issue.

References to the conformance surface live in the **awto-vyvanse** repo:
`docs/widget-pixel-geometry.md` (the human-authored pixel spec),
`docs/tgx-lvgl-rendering.md` (how the existing backends render, incl. the
§3.2 non-1:1 divergence vyr resolves), `vyvanse/ir/attr_map_seed.json` (the
IR-attribute source of truth), `vyvanse/preview/cases.py` (the v1 widget
capability set).

---

## 1. Vision

vyr is vyvanse's own renderer, consuming the IR **directly** — no lowering to
any other framework's vocabulary. Three roles, in build order:

1. **Reference renderer (the oracle).** The executable form of the pixel
   spec: the four existing backends (LVGL / TGX / Qt / Flutter) become export
   targets *scored against vyr* with per-property tolerances.
2. **The vye editor canvas.** The editing surface renders IR truth (vyr
   pixels), not any backend's interpretation — WYSIWYG = the spec.
3. **The endgame: a universal embedded GUI toolkit.** "Port a TouchGFX
   project and it works; send it an LVGL project and it works; a Flutter
   design is near-identical." The IR's direction of travel flips: designs
   flow TGX/LVGL → IR → **run natively on vyr on-device**. Nobody else
   ingests cross-vendor (TouchGFX Designer, LVGL Editor, SquareLine are
   single-vendor lock-in). The Flutter claim is **"Flutter-fidelity
   rendering"** — Flutter has no standard serialized UI format, so literal
   import is limited to `rfw` (optional, experimental); the fidelity claim is
   structural because vyr's architecture is Flutter-concept.

   **Order matters, and it is TouchGFX first — LVGL last.** (Revised
   2026-07-23; the original wording led with LVGL, which inverted the real
   difficulty.) The two importers are not comparable in cost:

   | | TouchGFX | LVGL |
   |---|---|---|
   | Importer | `tgx_to_ir` **exists** in vyvanse | **does not exist** |
   | Positioning | **absolute** — maps to the IR directly | flex / grid / align |
   | Styling | per-widget | theme cascade across parts *and* states |
   | Corpus on hand | **154 real projects** (`tpa-projects` 27 SDK versions, `tpa-projects-from-wine` 127, plus `tgx-examples-sdk`) | none |

   The gap is not effort, it is **architectural**. vyr is absolute-rect with
   no layout pass and **no default chrome by construction (I5)**. There is
   nowhere in this architecture for `lv_obj_set_flex_flow` to land: it must
   be fully resolved to absolute geometry upstream, which means an LVGL
   importer needs a **layout *simulator*, not a translator** — it has to
   reimplement LVGL's flex/grid solver and its theme cascade faithfully
   enough that the resolved geometry matches what LVGL itself would compute.
   That is a substantial project in its own right and **nobody has scoped
   it**. TouchGFX being absolute-positioned means it has no equivalent
   problem.

   So the defensible near-term demo is **`.touchgfx` → IR → running on the
   DISC1 panel**, and the plan should say so rather than leading with the
   harder claim. LVGL remains the strategically louder one — it is the
   incumbent vyr is measured against (see F16/F9) — but it lands *after*
   TouchGFX, and it lands only once the layout-simulator problem has an
   owner and a design.

**Why not Flutter (or any backend) as the reference:** each renders a
*lowering* of the IR through its own opinions (Material defaults, its text
stack, its AA). Promoting one to oracle renames its quirks as truth.

**Why Rust:** an oracle needs determinism (pinned pure-Rust software raster,
byte-stable goldens), correctness culture, and the modern-2D lineage that now
lives in Rust (Linebender: kurbo/peniko/parley/vello; tiny-skia). Embedded is
**in play, sequenced second**: find the best renderer first, then prove it on
MCU with measured numbers (F9) — the core stays `no_std`-clean from day one so
that gate stays cheap. Slint (Rust core, STM32-class targets) is the existence
proof for the category.

**Architecture is Flutter-concept, not Flutter-source.** Constraints-down /
sizes-up layout, render-node-owns-paint, abstract Canvas, repaint boundaries —
written from Flutter's documentation as a spec. Never a Dart port: Flutter's
own rasterizer (Impeller) is GPU-only C++ and wrong for a deterministic
software oracle, and the Dart framework code drags Skia semantics and a heavy
text stack.

**Painter: tiny-skia** — a pure-Rust port of Skia's CPU rasterization
algorithms: Skia's pixels without Skia's build, size, or version churn.
`vello_cpu` is the watched alternative and the eventual second painter for
oracle-vs-oracle cross-checks (F8). ThorVG is noted as the C++-tier fallback
candidate only.

---

## 2. Architecture (the spine)

```
IR JSON (vy_ vocabulary, schema_version, verbatim — no lowering)
  └─ vyr-core (Rust, no_std + alloc)
       ├─ IR model (serde)
       ├─ render tree: one node type per vy_ widget
       │    layout():  BoxConstraints down / Size up  (v1: absolute pass-through)
       │    paint(canvas): node owns its painting
       ├─ Canvas trait  ◄── THE seam: painters and accel plug here
       │    fill_rrect / stroke_rrect / disc / ring / line /
       │    glyph_run / blit_image / linear_gradient
       ├─ painter: tiny-skia (first); vello_cpu (cross-check); DMA2D hybrid (later)
       └─ render(tree, area: Rect, buf: &mut [u8], stride)   ◄── THE entry point
  └─ vyr-cli (std shell): IR JSON file in → PNG out (farm contract)
  └─ vyr-bench: criterion, ns/px baselines, scaling-law assertion
```

### Day-1 invariants (non-negotiable, designed in not bolted on)

| # | Invariant | Why |
|---|---|---|
| I1 | **Partial framebuffer is the only code path**: `render(tree, area, buf, stride)`; a full frame is `area = screen`. Dirty-rect tracking on top: changed nodes → invalid rects → banded redraw. | Retrofitting banding into a full-frame renderer is a rewrite; designing it in is nearly free. The MCU configuration IS the banded one. |
| I2 | **Determinism**: pinned `Cargo.lock`, committed golden hashes, software raster only, no time/randomness in render. | An oracle that drifts is not an oracle. |
| I3 | **Performance measured from day 1**, same standing as goldens: counters always compiled (never a debug variant), benches land with the first primitive, **ns/px is the canonical metric** (portable across band sizes, projectable to MCU clocks). | A perf regression must be a decision, never drift. The metrics path is the shipped path. |
| I4 | **The scaling law is an asserted property**: render time ~linear in band area; per-pixel cost flat across band sizes. "I have x ms ⇒ I can afford an N-line band" is computed, not discovered on the board. Superlinear blowup = a per-band fixed cost crept in (alloc, tree re-walk, painter setup) — caught the week it lands. | Small bands are the embedded config; per-band overhead is invisible at desktop full-frame and fatal on target. |
| I5 | **IR-authoritative chrome by construction**: no default borders, radii, padding, or theme colours, ever. | The renderer defines truth; defaults are someone else's opinions. |
| I6 | **Honest failure**: unknown widget type = hard error before rendering; long-tail widgets = labelled placeholder (outline + caption); a blank render is a bug, never a fallback. | The emitter-honesty discipline, applied to pixels. |
| I7 | **`vyr-core` is `no_std + alloc` clean**: no filesystem, clock, thread, or std-only deps in core; render into caller-provided buffers. Std lives in `vyr-cli`. | Keeps the F9 embedded gate cheap. |
| I8 | **Standalone repo, versioned contract**: vyr never imports vyvanse; the interface is IR JSON with `schema_version`. Conformance fixtures are generated by vyvanse and **committed into vyr** so vyr CI runs without vyvanse. | Parallel development; the repo is a product, not a subdirectory. |

### Presentation modes (what I1 does and does NOT restrict)

I1 ("partial framebuffer is the only code path") is about renderer
INTERNALS — there is no separate full-frame renderer that could drift from
the banded one. It does **not** restrict display topology; the presentation
strategy is the CALLER's, and the one entry point serves all the standard
embedded layouts:

| Mode | How it calls vyr | Notes |
|---|---|---|
| **Single full framebuffer** (e.g. LTDC scanning SDRAM) | `buf` = the framebuffer itself, `area` = screen or the dirty rects | render lands directly in display memory — vyr-cli's full-frame render IS this mode |
| **Double buffered** (two full FBs, pointer swap on vsync) | `buf` = the BACK buffer | the back buffer is two frames stale, so dirty-rect mode must repaint the union of THIS and the PREVIOUS frame's dirty regions (or copy forward) — the TouchGFX SMOC lesson. This bookkeeping lives in the flush/presentation layer (F11 runtime; measured in F9), never in the painter |
| **Partial + flush** (small working buffer, per-band flush to panel) | bands through a working buffer | the small-SRAM mode the banding contract exists for |

The flush/display HAL itself (panel drivers, vsync, DMA, pixel-format
convert) is toolkit territory: F9 measures it, F11 owns it, F13 accelerates
it.

### Authority model (who defines truth)

awto-vyvanse `docs/widget-pixel-geometry.md` (+ `attr_map_seed.json`) is the
**human-authored spec**. Gate F8-1: vyr must match it with **zero deviation
entries** — if vyr needs a deviation, either the spec or the renderer is
wrong; a human fixes one of them. After that gate, **vyr renders are the
operational oracle** (backends score against pixels), and the spec doc remains
the appeals court. Spec-vs-vyr disagreement is always a bug report, never an
auto-rebaseline.

---

## 3. Feature tracks

Each track = one GitHub issue here (F7's vyvanse half = an issue in
awto-vyvanse). Format: goal / deliverables / acceptance / depends.

### F1 — Crate skeleton: workspace, Canvas trait, tiny-skia painter, goldens
- **Goal:** first deterministic pixels through the real architecture.
- **Deliverables:** `vyr-core` (`no_std + alloc`) + `vyr-cli` (std) workspace;
  `Canvas` trait (v1 primitive set: `fill_rrect`, `stroke_rrect`, `disc`,
  `ring`, `line`, `glyph_run`, `blit_image`, linear gradient; RGB888);
  `TinySkiaCanvas`; PNG export in cli; golden fixture suite with committed
  hashes; `render(tree, area, buf, stride)` end-to-end (I1).
- **Acceptance:** goldens byte-identical on two different machines;
  `vyr-core` builds `no_std` (**verify tiny-skia's no_std+alloc mode here** —
  if it std-leaks, the painter moves behind a cli-side impl and core keeps the
  trait; the seam insulates the decision); band render of a fixture ==
  full-frame crop, byte-identical.
- **Depends:** —

### F2 — Perf discipline: counters, benches, scaling law
- **Goal:** I3 + I4 made real before any widget exists.
- **Deliverables:** always-compiled core counters (per-band pixels touched,
  dirty-area %, band count, peak alloc, **pixels-per-op-class**: opaque fill /
  alpha blend / blit / glyph / AA edge — timing lives in the shell, core has
  no clock); `vyr-bench` (criterion) per-primitive benches; ns/px reporting;
  multi-band-size scaling assertion; committed baseline file; counters
  surfaced in `vyr-cli` output.
- **Acceptance:** baseline JSON committed; scaling assertion green (flat ns/px
  across ≥4 band sizes); every later PR adding a primitive/widget fails review
  if it lacks a bench.
- **Depends:** F1.

### F3 — IR ingestion, render tree, layout protocol
- **Goal:** vyr speaks `vy_` natively.
- **Deliverables:** serde model of the IR node tree (`{name, attrs, children}`,
  `schema_version` checked); render-node-per-`vy_`-type registry; layout =
  `BoxConstraints` down / `Size` up, **v1 absolute pass-through** (today's IR
  is x/y/w/h) with the protocol scaffolded for flex later; dirty-rect
  tracking; **clip stack** in the paint protocol (containers clip children,
  rounded-corner clip for radius boxes, scroll containers clip by
  construction — clipping composes with banding, the band is just the
  outermost clip); hard error on unknown types (I6); committed
  vyvanse-generated IR fixtures (I8).
- **Acceptance:** fixture IRs render; unknown-type fixture errors usefully;
  band-equivalence golden green across all fixtures.
- **Depends:** F1, F2.

### F4 — Widget vocabulary v1
- **Goal:** the vyvanse capability set (`cases._cap_widget`) renders real.
- **Deliverables:** gallery order, simple → complex: box/frame/container →
  circle/ellipse/line → label/lcd → button → slider/bar/progress →
  switch/toggle → arc/gauge → image → radio. Long tail (roller, list, chart,
  table, dropdown, canvas, video): labelled placeholders (I6). Per-widget
  golden + bench, every widget.
- **Acceptance:** every capability-set vy-type renders non-blank or
  placeholder-labelled; geometry matches the pixel spec within ±1px AA
  tolerance (pre-F8 spot check).
- **Depends:** F3 (text widgets also F5; image also F6).

### F5 — Text: FULL font support, one format, runtime-rasterized + glyph cache
- **Goal:** full vector font support as an EARLY target — **one format
  (TTF/OTF), pick the best, no second font pipeline**. One rasterizer serves
  desktop oracle and MCU alike; baking becomes an optimization (F15), not a
  separate text stack. Scope stays: single-style, single-direction runs (not
  a paragraph engine; no shaping — parley/HarfBuzz territory is explicitly
  out until the IR grows rich text).
- **Architecture:** `skrifa` (charmap, metrics/advances, **outlines**;
  `no_std`-friendly) → outline filled by the same tiny-skia painter →
  **glyph cache**: each (font, size, codepoint) rasterized ONCE to an A8
  alpha mask, then `glyph_run` is pure cached blits. Likely drops the swash
  dependency entirely (verify: kerning needs, embedded-bitmap strikes for
  Spleen). On MCU the same path runs at boot/first-use: TTF lives in flash
  (systems have the flash for it), cache lives in RAM — ~95 ASCII glyphs at
  14 px ≈ low-tens-of-KB, fine for F427-class; F15 moves the cache itself to
  flash at build time for tiny-RAM parts.
- **Deliverables:** glyph cache in core (`no_std`); vyvanse standard test
  fonts (Roboto vector + Spleen) rendered through it; cache-size counters
  wired into RenderStats; unicode beyond ASCII works by construction (cache
  keyed by codepoint) but coverage/fallback policy stays IR-driven.
- **Acceptance:** label/lcd/button text geometry within spec tolerance;
  cross-backend text comparison sane (same fonts); glyph rasterized exactly
  once per (font,size,cp) — proven by counters; `no_std` build includes the
  full text path.
- **Depends:** F3.

### F6 — Images
- **Goal:** `vy_image`/`src` renders the actual asset.
- **Deliverables:** PNG decode (`png` crate) in cli (decode stays out of
  `no_std` core; core blits caller-provided pixel buffers); `src` resolution
  rules matching the farm backends (the farm serializer strips
  backend-specific prefixes like LVGL's `A:`).
- **Acceptance:** `vy_image` golden with a real asset; missing asset = hard
  error, not blank.
- **Depends:** F3.

### F7 — Render-farm fifth backend (half lives in awto-vyvanse)
- **Goal:** `render(thing, "vyr", res)` works in the vyvanse farm; the widget
  gallery grows a vyr column.
- **Deliverables (vyvanse side, tracked there):** `farm_client._prepare`
  branch serialising IR **verbatim** (no lowering — the point);
  `vyvanse/backends/vyr_server.py` thin persistent wrapper (the
  "wrap, don't rewrite" pattern) forking one-shot `vyr-cli`; farm-registry
  endpoint; crash discipline (returncode/signal = hard FAIL, blank PNG = hard
  FAIL). **(vyr side):** `vyr-cli` env/arg contract (scene path, W/H, out
  path; counters on stdout). If process-spawn cost ever dominates farm
  throughput, add a persistent stdin loop mode to cli — core unchanged.
- **Acceptance:** parallel `render_many` fans across the pool; gallery emits
  `<label>-vyr.png` for the full capability set.
- **Depends:** F4 (partial vocab fine to start).

### F8 — Conformance gates, oracle flip, CI
- **Goal:** vyr becomes the operational oracle, provably.
- **Deliverables:** **Gate 1** — vyvanse `geometry_measure` + `colour_check`
  vs the pixel spec: zero deviation entries for vyr (authority model above).
  **Gate 2** — backends scored against vyr renders *in addition to* the spec,
  per-property tolerances, deviation-registry style (documented deltas, never
  byte-equality demands). **Band-equivalence golden**: every gallery scene
  full-frame vs N stitched bands, byte-identical. **Perf regression gate**:
  nightly bench vs committed baseline, fail like a broken golden; re-baseline
  only as an explicit reviewed change. Nightly CI (local-first by policy).
  Later: `vello_cpu` second painter, CI-diffed vs tiny-skia (oracle for the
  oracle).
- **Acceptance:** nightly green across goldens + conformance + band + perf;
  the flip documented in vyvanse docs.
- **Depends:** F4–F7.

### F9 — Embedded spike (measured verdict, not vibes)
- **Goal:** the "then see if we can make it work on embedded" gate.
- **Deliverables:** `vyr-core` compiled for `thumbv7em-none-eabihf`
  (STM32F4-class M4F; board choice open); flash + RAM measured:
  full-framebuffer (external SDRAM) AND banded into small SRAM working
  buffers (the path is CI-proven by F1/F8 — this *measures* band size vs
  frame time on target); ns/px on target (f32 AA on the single-precision
  FPU — tiny-skia is f32, the right float); **RGB565 output measured** (real
  MCU panels are 565, not the oracle's 888 — pixel-format conversion is a
  painter/flush concern and the convert cost belongs in the numbers; the
  format decision feeds F13's DMA2D path, which converts in hardware);
  boot-time glyph-cache fill measured (F5); hot-path identification;
  **verdict doc**: ship-it / add own fixed-function painter tier behind the
  trait / C++-tier fallback — decided on numbers.
- **Acceptance:** published numbers table + recorded go/no-go.
- **Depends:** F1–F4 (I7 enforced throughout makes this cheap).

### F10 — Debug overlay suite (Android-developer-options class)
- **Goal:** zero-code, hidden, always available; no config flags to know
  about, no debug build. Hooks are day-1 (F2 counters + an overlay pass
  through the same Canvas); modes land incrementally.
- **Deliverables:**

  | Mode | Android analogue | Shows |
  |---|---|---|
  | Perf HUD | Profile GPU rendering | frame time, ns/px, FPS, dirty-area %, band count, peak alloc; frame bars vs budget line |
  | Layout bounds + grid | Show layout bounds | node bboxes + padding, snap grid |
  | Touch indicators | Pointer location | touch point/trail + hit-tested node |
  | Partial-buffer visualiser | Show surface updates | tint each redrawn band/dirty rect |
  | Overdraw heatmap | GPU overdraw | per-pixel write count (1×/2×/3×+) |
  | Op-cost breakdown | — (ours) | est. CPU % per op class = per-class pixel counters × bench ns/px ("alpha blend ≈ x%") — estimation, zero on-device timing overhead |

  Runtime toggle (gesture/pin/command). Hidden = zero cost beyond counters.
- **Acceptance:** each mode demoable via `vyr-cli` (PNG with overlay) before
  any interactive target exists; modes double as vye editor inspection modes.
- **Depends:** F2 (hooks), F4 (something to inspect).

### F11 — Interaction & behaviour layer (the toolkit step) — design-first
- **Goal:** the gap between "renders the same" and "it works": input,
  hit-testing, widget interaction states (press/drag/scroll), animation tick,
  event callbacks into user code (C ABI + Rust). Composes with the vye
  bidirectional GUI/AI protocol.
- **Deliverables:** a design doc of its own first (state model, event model,
  callback ABI, IR extensions for behaviour — today's IR is
  visual-structural); then incremental implementation.
- **Acceptance (design phase):** reviewed design doc; IR extension proposal.
- **Depends:** F4; F9 verdict informs targets.

### F12 — Import-and-run demos (the endgame proof)
- **Goal:** the marketing claim, measured — a real vendor project running on
  vyr. **Staged: TouchGFX first, LVGL second** (see §1.3 for why they are not
  comparable in cost).
- **F12a — TouchGFX (the near-term deliverable).** `tgx_to_ir` already
  exists in vyvanse and TouchGFX is absolute-positioned, so no layout
  simulation is needed. A corpus of 154 real projects is already on disk.
  Target: `.touchgfx` → IR → rendering **on the DISC1 panel** (#28/#30) —
  the demo is the whole loop on real hardware, not a host screenshot.
- **F12b — LVGL (blocked on a design, not on effort).** Needs an LVGL XML →
  IR importer that **resolves flex/grid/align and the theme cascade to
  absolute geometry**, because vyr has no layout pass and I5 forbids default
  chrome. That is a layout *simulator*. **Do not start F12b until that has
  an owner and a written design** — it is the single largest unscoped risk
  in the plan.
- **F12c — `rfw`**, explicitly experimental, unchanged.
- **Deliverables:** demo captures + fidelity numbers from the F8 surface as
  evidence; the published fidelity table.
- **Acceptance:** F12a renders + (post-F11) interacts, on device, with a
  published fidelity table. F12b is separately accepted.
- **Depends:** F8 (+F11 for "works", not just "renders"); F12a additionally
  wants the panel path (#28 done over SPI, #30 for animation).
- **Blocked by a cheap fix:** I6 honest-failure is currently all-or-nothing —
  six IR types hard-error rather than painting the labelled placeholder F4
  and I6 both specify, so a single unsupported widget aborts the entire
  frame. Any imported project is one `vy_table` away from rendering nothing.
  Fix that before either import demo.

### F13 — Hardware-accelerated painters (later, never blocks v1)
- STM32 DMA2D/Chrom-ART (fills, blits, alpha blend, format convert — exactly
  the fixed-function subset), NeoChrom on newer STM32, NXP VGLite. The Canvas
  trait is the seam: accelerated painter offloads fills/blits, falls back to
  software for AA paths — the LVGL/TouchGFX hybrid pattern, zero changes above
  the trait. **Depends:** F9 verdict.

### F14 — Editor canvas embedding (later)
- vyr pixels inside the vye editor shell: `flutter_rust_bridge`/FFI →
  Flutter `Texture` on desktop; `wasm32` build for in-browser. (The
  editor-shell decision itself is a vye-side decision, tracked in vyvanse.)
  **Depends:** F3/F4.

### F15 — Bake-to-flash: build-time asset pre-render (the LAST optimization)
- **Goal:** the tiny-RAM/boot-time optimization, deliberately sequenced last
  because F5 makes it nearly free: a build step that runs **the same
  rasterizer/decoder as the runtime** to pre-render assets into
  flash-resident tables. Baked output is pixel-identical to the runtime path
  *by construction* (same code), and a golden asserts it.
- **Scope — fonts:** subset scan from the IR (static strings are known;
  dynamic text via declared ranges — the TouchGFX wildcard-range lesson, e.g.
  `0x20-0x7E`) → pre-rasterized glyph tables in flash; runtime glyph cache
  consults baked tables first, falls back to live rasterization if the TTF is
  also shipped, or hard-errors on a missing glyph in baked-only builds (I6 —
  never a tofu box the IR didn't ask for).
- **Scope — images:** PNG decoded + converted to the target pixel format at
  build time, blitted raw from flash (the same generalization: decode is a
  build step, the device blits).
- **Acceptance:** baked-vs-runtime byte-identical golden; flash/RAM deltas
  measured vs the F9 baseline; subset report (which glyphs, from which IR
  strings) emitted at build time — no silent coverage gaps.
- **Depends:** F5 (fonts), F6 (images), F9 (numbers to beat).

### F16 — Quality tiers: the speed-for-quality knobs (a product feature)
- **Goal:** expose quality/speed knobs as a FIRST-CLASS, measured feature —
  the thing TouchGFX never had, and the thing that becomes essential the
  moment frames animate or play video on a budgeted MCU. Dan's framing:
  byte-exact rendering is the oracle's job; the runtime wants to go fast,
  and dropping quality deliberately is a feature, not a failure.
- **The design rule that keeps the oracle intact:** quality is a SMALL
  DISCRETE ENUM (`Q::Exact | Q::Fast | Q::Draft`, names TBD), never a float —
  and **every tier is individually deterministic** (same input + same tier =
  same bytes, banding included). The oracle/conformance world pins
  `Q::Exact`; goldens exist per tier where behaviour differs; the
  I1/I2/I4 invariants hold WITHIN each tier. A knob that breaks per-tier
  determinism is rejected.
- **Knob candidates (each lands with its bench so the trade is PRICED):**
  - **AA off** (`Draft`): opaque non-AA fills become span writes — the
    single biggest ns/px lever; tiny-skia supports it per-paint already.
  - ~~**Flattening density**: halve the fixed-step segment counts (`Fast`)~~ —
    **superseded (#27).** `Fast` shipped with different semantics: Draft's
    integer span fills everywhere they apply, plus the Exact AA path for
    CURVED geometry only. Halving the flattening density was never built,
    because the measurement said the gap Draft leaves is entirely *edge
    blending*, not *segment count* — Draft has literally 0 blend pixels in the
    gauge region against Exact's 567. Fast recovers all 567. It costs 4.4x
    Draft on the M4 (docs/performance.md §3.1), so the knob is real but the
    price is not the one this line assumed.
  - **Gutter off** (`Fast`/`Draft`): the overscan exists for clip-adjacent
    AA cleanliness; without AA (or accepting LSB seams) the (w+16)(h+16)
    overscan cost — visible in the F2 scaling table — disappears.
  - **Bilevel glyphs**: 1-bpp masks instead of A8 (`Draft`) — classic
    embedded text, blits become masked writes, cache shrinks 8×.
  - **Cheaper gradients**: banded/stepped interpolation (`Draft`).
- **NOT knobs:** per-band op culling (always-on optimization, F2's recorded
  target); pixel format (a flush concern, F9/F13).
- **Runtime auto-tier (the video case, later, with F11):** a frame-budget
  governor — when measured frame time exceeds the budget, drop a tier;
  recover when headroom returns. The F10 perf HUD displays the ACTIVE tier
  (the knob must be visible to be trusted), and the per-tier ns/px baselines
  from this track are what the governor's predictions are made of.
- **Acceptance:** tier enum plumbed through Canvas/painter with `Exact` the
  default everywhere oracle-facing; per-tier determinism tests (golden per
  tier on the shared fixtures); per-tier ns/px rows in baseline.json — the
  knob's value is a measured number, not a vibe; gallery row showing
  Exact/Fast/Draft side-by-side (the honest "what you give up" picture).
- **Depends:** F2 (pricing), F4 (vocabulary to exercise); informs F9
  (measure tiers on target) and F11 (the governor).

---

## 4. Milestones

| Milestone | Means | Features |
|---|---|---|
| **M1 — first pixels** | deterministic goldens + perf harness green | F1, F2 |
| **M2 — oracle live** | full v1 vocab via the farm, conformance flipped, nightly CI | F3–F8 |
| **M3 — embedded verdict** | measured flash/RAM/ns-px on M4F target, go/no-go doc | F9 (+F10 hooks) |
| **M4 — toolkit alpha** | debug suite, interaction design, import-and-run demos | F10–F12 |
| later | accel painters, editor embedding, bake-to-flash, quality tiers | F13, F14, F15, F16 |

## 5. Open decisions

1. Embedded spike board (F9): F429-DISC1 class vs H735-DK — pick by what's on
   hand.
2. Long-tail widget order after the v1 set (which placeholder graduates
   first — roller? list?).
3. CLA mechanics (DCO accepted now; CLA bot for substantial contributions —
   see `LICENSING.md`).
4. F5 glyph source crate: skrifa outlines + tiny-skia fill (recommended) vs
   fontdue vs ab_glyph — decide inside F5 with a bake-off on the standard
   fonts (criteria: no_std, determinism, output quality at 10–16 px,
   Spleen/bitmap-strike handling, kerning).
5. crates.io: reserve `vyr-core` / `vyr-cli` names early (publish 0.0.1
   skeletons) — public repo means the names are now visible.

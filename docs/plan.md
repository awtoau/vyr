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
3. **The endgame: a universal embedded GUI toolkit.** "Send it an LVGL
   project and it works; port a TouchGFX project and it works; a Flutter
   design is near-identical." The IR's direction of travel flips: designs
   flow LVGL/TGX → IR → **run natively on vyr on-device**. Nobody else
   ingests cross-vendor (TouchGFX Designer, LVGL Editor, SquareLine are
   single-vendor lock-in); the importer half largely exists in vyvanse
   (`tgx_to_ir` done; LVGL XML import is the reverse of an emit path it
   owns). The Flutter claim is **"Flutter-fidelity rendering"** — Flutter has
   no standard serialized UI format, so literal import is limited to `rfw`
   (optional, experimental); the fidelity claim is structural because vyr's
   architecture is Flutter-concept.

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
  tracking; hard error on unknown types (I6); committed vyvanse-generated IR
  fixtures (I8).
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

### F5 — Text
- **Goal:** real glyphs, the same fonts every other backend uses, scoped to
  what the IR carries (single-style, single-direction runs — **not** a
  paragraph engine; parley only if the IR ever grows wrapping/rich text).
- **Deliverables:** desktop path: `skrifa` (parse) + `swash` (scale/raster)
  with the vyvanse standard test fonts (Roboto vector, Spleen bitmap);
  glyph_run through Canvas; **MCU path designed, not built**: feature-gated
  baked bitmap glyphs (swash/skrifa `no_std` availability to be verified — do
  not promise vector text on M4F in v1).
- **Acceptance:** label/lcd/button text geometry within spec tolerance;
  cross-backend text comparison sane (same fonts).
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
  FPU — tiny-skia is f32, the right float); hot-path identification;
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
- **Goal:** the marketing claim, measured: a real `.touchgfx` project and a
  real LVGL XML project running on vyr.
- **Deliverables:** LVGL XML → IR importer (vyvanse side); `.touchgfx` → IR
  already exists there (`tgx_to_ir`); demo captures + fidelity numbers from
  the F8 surface as evidence; `rfw` import as an explicitly experimental
  stretch.
- **Acceptance:** two demos render + (post-F11) interact; published fidelity
  table.
- **Depends:** F8 (+F11 for "works", not just "renders").

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

---

## 4. Milestones

| Milestone | Means | Features |
|---|---|---|
| **M1 — first pixels** | deterministic goldens + perf harness green | F1, F2 |
| **M2 — oracle live** | full v1 vocab via the farm, conformance flipped, nightly CI | F3–F8 |
| **M3 — embedded verdict** | measured flash/RAM/ns-px on M4F target, go/no-go doc | F9 (+F10 hooks) |
| **M4 — toolkit alpha** | debug suite, interaction design, import-and-run demos | F10–F12 |
| later | accel painters, editor embedding | F13, F14 |

## 5. Open decisions

1. Embedded spike board (F9): F429-DISC1 class vs H735-DK — pick by what's on
   hand.
2. Long-tail widget order after the v1 set (which placeholder graduates
   first — roller? list?).
3. CLA mechanics (DCO accepted now; CLA bot for substantial contributions —
   see `LICENSING.md`).

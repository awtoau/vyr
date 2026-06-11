#!/usr/bin/env bash
# One-shot M0 setup: milestones M1-M4, labels, issues F1-F14 for awtoau/vyr.
# Idempotence: label/milestone creation tolerates "already exists"; issues do
# NOT (re-running would duplicate them) — this script is run once at repo birth.
set -uo pipefail
REPO=awtoau/vyr
LOG="$(dirname "$0")/../tmp/file-issues.log"
mkdir -p "$(dirname "$LOG")"
exec > "$LOG" 2>&1
set -x

# --- milestones (gh has no native command; use the API) ---
gh api "repos/$REPO/milestones" -f title="M1 - first pixels" -f description="deterministic goldens + perf harness green (F1, F2)" || true
gh api "repos/$REPO/milestones" -f title="M2 - oracle live" -f description="full v1 vocab via the farm, conformance flipped, nightly CI (F3-F8)" || true
gh api "repos/$REPO/milestones" -f title="M3 - embedded verdict" -f description="measured flash/RAM/ns-px on M4F target, go/no-go doc (F9)" || true
gh api "repos/$REPO/milestones" -f title="M4 - toolkit alpha" -f description="debug suite, interaction design, import-and-run demos (F10-F12)" || true

# --- labels (p0-p4 = awto priority convention; copilot-ok = mechanical) ---
for L in p0 p1 p2 p3 p4; do gh label create "$L" --repo "$REPO" --color BFD4F2 || true; done
gh label create copilot-ok --repo "$REPO" --color 0E8A16 --description "mechanical + self-contained: suitable for the Copilot coding agent" || true
gh label create design --repo "$REPO" --color 5319E7 --description "design-first: needs a reviewed design doc before code" || true

issue() { # title milestone body
  gh issue create --repo "$REPO" --title "$1" --milestone "$2" --body "$3"
}
issue_nm() { # title body (no milestone)
  gh issue create --repo "$REPO" --title "$1" --body "$2"
}

issue "F1 — Crate skeleton: Canvas trait, tiny-skia painter, goldens" "M1 - first pixels" "Goal: first deterministic pixels through the real architecture.

Deliverables: Canvas trait (v1 primitive set: fill_rrect, stroke_rrect, disc, ring, line, glyph_run, blit_image, linear gradient; RGB888); TinySkiaCanvas; PNG export in cli; golden fixture suite with committed hashes; render(tree, area, buf, stride) end-to-end (invariant I1).

Acceptance:
- [ ] goldens byte-identical on two different machines
- [ ] vyr-core builds no_std — VERIFY tiny-skia no_std+alloc mode here; if it std-leaks, painter moves behind a cli-side impl and core keeps the trait
- [ ] band render of a fixture == full-frame crop, byte-identical

Plan: docs/plan.md §F1. Depends: —"

issue "F2 — Perf discipline: counters, benches, scaling-law assertion" "M1 - first pixels" "Goal: invariants I3 + I4 made real before any widget exists.

Deliverables: always-compiled core counters (pixels touched, dirty-area %, band count, peak alloc, pixels-per-op-class: opaque fill / alpha blend / blit / glyph / AA edge — timing lives in the shell, core has no clock); vyr-bench (criterion) per-primitive benches; ns/px reporting; multi-band-size scaling assertion; committed baseline file; counters in vyr-cli output.

Acceptance:
- [ ] baseline JSON committed
- [ ] scaling assertion green: flat ns/px across >=4 band sizes
- [ ] policy active: any PR adding a primitive/widget without its bench fails review

Plan: docs/plan.md §F2. Depends: F1"

issue "F3 — IR ingestion, render tree, layout protocol" "M2 - oracle live" "Goal: vyr speaks vy_ natively (IR JSON verbatim, no lowering).

Deliverables: serde IR node model ({name, attrs, children}, schema_version checked); render-node-per-vy_-type registry; layout = BoxConstraints down / Size up, v1 absolute pass-through, protocol scaffolded for flex later; dirty-rect tracking; hard error on unknown types (I6); committed vyvanse-generated IR fixtures (I8).

Acceptance:
- [ ] fixture IRs render
- [ ] unknown-type fixture errors usefully (before pixels)
- [ ] band-equivalence golden green across all fixtures

Plan: docs/plan.md §F3. Depends: F1, F2"

issue "F4 — Widget vocabulary v1 (capability set)" "M2 - oracle live" "Goal: the vyvanse capability set (cases._cap_widget) renders real.

Order (simple → complex): box/frame/container → circle/ellipse/line → label/lcd → button → slider/bar/progress → switch/toggle → arc/gauge → image → radio. Long tail (roller, list, chart, table, dropdown, canvas, video): labelled placeholders (I6). Per-widget golden + bench, every widget.

Acceptance:
- [ ] every capability-set vy-type renders non-blank or placeholder-labelled
- [ ] geometry within ±1px AA tolerance vs the pixel spec (pre-F8 spot check)

Plan: docs/plan.md §F4. Depends: F3 (+F5 text, +F6 image)"

issue "F5 — Text: skrifa+swash desktop; MCU bitmap path designed" "M2 - oracle live" "Goal: real glyphs, same fonts as every other backend; scoped to what the IR carries (single-style single-direction runs; NOT a paragraph engine — parley only if the IR grows wrapping/rich text).

Deliverables: skrifa (parse) + swash (scale/raster) with the vyvanse standard test fonts (Roboto vector, Spleen bitmap); glyph_run through Canvas; MCU path DESIGNED not built: feature-gated baked bitmap glyphs (verify swash/skrifa no_std availability; do not promise vector text on M4F in v1).

Acceptance:
- [ ] label/lcd/button text geometry within spec tolerance
- [ ] cross-backend text comparison sane (same fonts)

Plan: docs/plan.md §F5. Depends: F3"

issue "F6 — Images: PNG decode in cli, blit via core" "M2 - oracle live" "Goal: vy_image/src renders the actual asset.

Deliverables: PNG decode (png crate) in vyr-cli — decode stays OUT of no_std core; core blits caller-provided pixel buffers; src resolution rules matching the farm backends (farm serializer strips backend prefixes like LVGL A:).

Acceptance:
- [ ] vy_image golden with a real asset
- [ ] missing asset = hard error, not blank

Plan: docs/plan.md §F6. Depends: F3"

issue "F7 — Render-farm fifth backend (vyr-cli contract half)" "M2 - oracle live" "Goal: render(thing, \"vyr\", res) works in the vyvanse farm; gallery grows a vyr column.

vyr side (this issue): vyr-cli env/arg contract mirroring vyvanse-runner (scene path, W/H, out path; counters on stdout); exit non-zero on any failure. If process-spawn cost ever dominates farm throughput: add persistent stdin loop mode, core unchanged.
vyvanse side (tracked in awto-vyvanse): _prepare verbatim-IR serializer, vyr_server.py wrapper, farm-registry endpoint, crash discipline (returncode/signal = hard FAIL, blank PNG = hard FAIL).

Acceptance:
- [ ] parallel render_many fans across the pool
- [ ] gallery emits <label>-vyr.png for the full capability set

Plan: docs/plan.md §F7. Depends: F4 (partial vocab ok)"

issue "F8 — Conformance gates, oracle flip, CI" "M2 - oracle live" "Goal: vyr becomes the operational oracle, provably.

Gate 1: vyvanse geometry_measure + colour_check vs the pixel spec — ZERO deviation entries for vyr (authority model: spec-vs-vyr disagreement is a bug report, never an auto-rebaseline).
Gate 2: backends scored against vyr renders in addition to the spec; per-property tolerances; deviation-registry style (documented deltas, never byte-equality demands).
Plus: band-equivalence golden (every gallery scene full-frame vs N stitched bands, byte-identical); perf regression gate (nightly bench vs committed baseline, fail like a broken golden; re-baseline = explicit reviewed change); nightly CI (local-first by policy). Later: vello_cpu second painter CI-diffed vs tiny-skia (oracle for the oracle).

Acceptance:
- [ ] nightly green: goldens + conformance + band + perf
- [ ] the flip documented in vyvanse docs

Plan: docs/plan.md §F8. Depends: F4-F7"

issue "F9 — Embedded spike: thumbv7em, measured flash/RAM/ns-px, verdict" "M3 - embedded verdict" "Goal: the \"then see if we can make it work on embedded\" gate — numbers, not vibes.

Deliverables: vyr-core compiled for thumbv7em-none-eabihf (STM32F4-class M4F; board choice open: F429-DISC1 vs H735-DK); flash + RAM measured full-framebuffer (external SDRAM) AND banded into small SRAM buffers (path already CI-proven — this MEASURES band size vs frame time on target); ns/px on target (f32 AA on the single-precision FPU); hot-path identification; verdict doc: ship-it / own fixed-function painter tier behind the trait / C++-tier fallback.

Acceptance:
- [ ] published numbers table
- [ ] recorded go/no-go verdict doc

Plan: docs/plan.md §F9. Depends: F1-F4 (I7 enforced throughout keeps this cheap)"

issue "F10 — Debug overlay suite (Android-developer-options class)" "M4 - toolkit alpha" "Goal: zero-code, hidden, always available — no config flags to know about, no debug build. Hooks are day-1 (F2 counters + overlay pass through the same Canvas); modes land incrementally.

Modes: Perf HUD (frame time, ns/px, FPS, dirty-area %, band count, peak alloc, frame bars vs budget); Layout bounds + grid; Touch indicators (+ hit-tested node); Partial-buffer visualiser (tint redrawn bands/dirty rects); Overdraw heatmap (1x/2x/3x+); Op-cost breakdown (est. CPU % per op class = per-class pixel counters × bench ns/px — estimation, zero on-device timing overhead).

Acceptance:
- [ ] each mode demoable via vyr-cli (PNG with overlay) before any interactive target
- [ ] modes double as vye editor inspection modes

Plan: docs/plan.md §F10. Depends: F2 (hooks), F4 (something to inspect)"

issue "F11 — Interaction & behaviour layer (design-first)" "M4 - toolkit alpha" "Goal: the gap between \"renders the same\" and \"it works\": input, hit-testing, widget interaction states (press/drag/scroll), animation tick, event callbacks into user code (C ABI + Rust). Composes with the vye bidirectional GUI/AI protocol.

DESIGN-FIRST: a reviewed design doc (state model, event model, callback ABI, IR extensions for behaviour — today's IR is visual-structural) before any implementation.

Acceptance (design phase):
- [ ] reviewed design doc
- [ ] IR extension proposal

Plan: docs/plan.md §F11. Depends: F4; F9 verdict informs targets"

issue "F12 — Import-and-run demos (the endgame proof)" "M4 - toolkit alpha" "Goal: the marketing claim, measured — a real .touchgfx project and a real LVGL XML project running on vyr.

Deliverables: LVGL XML → IR importer (vyvanse side); .touchgfx → IR exists there (tgx_to_ir); demo captures + fidelity numbers from the F8 surface as evidence; rfw import as an explicitly EXPERIMENTAL stretch (Flutter has no standard serialized UI format — the Flutter claim is fidelity, not import).

Acceptance:
- [ ] two demos render (+ interact post-F11)
- [ ] published fidelity table

Plan: docs/plan.md §F12. Depends: F8 (+F11 for works-not-just-renders)"

issue_nm "F13 — Hardware-accelerated painters (DMA2D/Chrom-ART, NeoChrom, VGLite)" "Later — never blocks v1. STM32 DMA2D/Chrom-ART covers fills, blits, alpha blend, format convert — exactly the fixed-function subset. The Canvas trait is the seam: accelerated painter offloads fills/blits, falls back to software for AA paths (the LVGL/TouchGFX hybrid pattern), zero changes above the trait. NeoChrom (newer STM32) and NXP VGLite follow the same slot.

Plan: docs/plan.md §F13. Depends: F9 verdict."

issue_nm "F14 — Editor canvas embedding (FFI texture + wasm32)" "Later. vyr pixels inside the vye editor shell: flutter_rust_bridge/FFI → Flutter Texture on desktop; wasm32 build for in-browser. The editor-shell decision itself (Flutter chrome + vyr canvas vs Qt-WASM path) is a vye-side decision tracked in awto-vyvanse.

Plan: docs/plan.md §F14. Depends: F3/F4."

set +x
echo DONE

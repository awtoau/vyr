# vyr milestones — the renderer's own history, in its own pixels

Every feature track that changes what vyr can paint commits its evidence
render HERE, in the same commit (or the bump after). These are the **golden
pixels** where one exists (the PNG the committed hash pins), so this page is
the visual changelog of the executable spec. Posterity rule: rows are
APPENDED, never replaced — a later improvement gets a new row, the old pixels
stay as history.

Local gallery page: [`index.html`](index.html) (open via file:// or any
static server). On GitHub this README renders inline below.

| Stage | Date | Commit | Render | What it proves |
|---|---|---|---|---|
| **F1 — primitives** | 2026-06-11 | `b4f441a` | ![F1](f1-primitives.png) | The polygon-only tiny-skia painter: rounded fill, stroked outline, alpha-blended disc, ring, diagonal rule, linear gradient. The exact bytes of `GOLDEN_FNV1A`; byte-identical when rendered as 30-row or 17-row stitched bands. |
| **F3-lite — IR widgets** | 2026-06-11 | `83a7c09` | ![F3](f3-ir-widgets.png) | The `vy_` vocabulary rendered natively from IR JSON (no lowering): bordered frame, disc, 60% slider + knob, toggle ON, gauge ring, rule. `IR_GOLDEN_FNV1A`; band-exact. |
| **F5 — text** | 2026-06-11 | `834ed5e` | ![F5](f5-text.png) | skrifa-parsed Roboto through the rasterize-once A8 glyph cache: 14px label, button with centred ink-inherited label, 20px `vy_lcd`. `TEXT_GOLDEN_FNV1A`; band-exact (glyph curves never meet band clipping). Full path is `no_std` (thumbv7em). |
| **F7 — the farm speaks vyr** | 2026-06-11 | vyvanse `84a612d` / `2fac0e9` | ![frame](f7-farm-frame.png) ![slider](f7-farm-slider.png) ![label](f7-farm-label.png) | The same widgets rendered THROUGH the vyvanse render farm (`render(thing, "vyr")` → vyr_server → one-shot vyr-cli), ~2 ms each — vyr live as the fifth backend beside Qt/LVGL/TGX/Flutter. |

## Engineering gallery — the special bits

The places where vyr does something non-obvious, with the evidence.

### The tiny-skia clip discovery (what made the painter polygon-only)

The band-equivalence golden's FIRST run failed: rendering the same scene as
30-row bands produced pixels that differed from the full frame. The diff was
tiny — a handful of bytes — and all on the ring:

| full frame | stitched bands | differing pixels (dilated red) |
|---|---|---|
| ![full](eng-band-clip-full.png) | ![banded](eng-band-clip-banded.png) | ![diff](eng-band-clip-diff.png) |

Indistinguishable by eye; not byte-identical. Cause: tiny-skia clips path
geometry at the pixmap edge, and **clipping subdivides curves**, which
perturbs adaptive flattening along the whole remaining arc — an LSB of AA
drift up to 8 rows away from the clip line, so no overscan gutter can bound
it. The stroker makes it worse (curves expand into new curves). The fix is
architectural and permanent: **the painter never hands tiny-skia a curve or a
stroke**. All flattening happens in vyr (fixed-step, radius-derived), vertices
quantize to a 1/64-px grid in WORLD space (exact in f32), bands translate by
exact integers, strokes are built as outer+inner polygon contours. Bands
became byte-identical — for even and deliberately-awkward uneven splits —
and `tests/golden.rs` enforces it forever.

### What the polygon discipline looks like up close

6× nearest-neighbour zoom of the F1 ring — fixed-step flattening + 1/64-px
quantization, AA by tiny-skia's scanline coverage. No visible faceting at
~0.09 px max sagitta error:

![ring zoom](eng-ring-zoom.png)

### Glyphs live OUTSIDE the polygon rule, safely

Glyph outlines are real curves — allowed, because they only ever meet
tiny-skia in glyph-local, unclipped space during the one-time A8 mask
rasterization. Bands see nothing but cached masks blitted at INTEGER pen
positions through an exact-rounding integer source-over (`round(x/255)` in
pure integer math). 4× zoom of the F5 button:

![text zoom](eng-text-zoom.png)

### The other special bits (no picture, but load-bearing)

- **Honest failure**: an unrenderable widget exits with a NAMED error before
  any pixel (`Unimplemented("text-bearing widget pre-F5 …")` travelled
  cli → farm server → farm client verbatim in the smoke test). A blank frame
  is a bug by definition, never a fallback.
- **IR-authoritative chrome**: a plain box paints NOTHING the IR didn't
  declare — no default borders, radii, or theme colours. Real widgets carry
  documented widget-default chrome only.
- **Counters are the shipped path**: every render reports pixels-per-op-class
  (the `12:34` frame: 19,772 px written, 2,572 of them Glyph class, 19 masks
  rasterized, 2,209 B cache) — the same counters the benches read and the
  future on-device perf HUD will draw.

## The numbers that travelled with them

| Stage | ns/px (release, dev host) | Notes |
|---|---|---|
| F2 baseline | fills 1.3 · strokes 2.5 · discs 3.7 · rings 5.2 · gradients 6.7 · IR scene 5.9 | scaling table exposed the per-band fixed cost (1.0×→2.1× as bands shrink) — first recorded optimization target |
| F5 text | glyph blits 1.37 · text scene 1.86 | 19 cached masks = 2,209 B RAM; rasterize-once proven by counters |

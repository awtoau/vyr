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

## The numbers that travelled with them

| Stage | ns/px (release, dev host) | Notes |
|---|---|---|
| F2 baseline | fills 1.3 · strokes 2.5 · discs 3.7 · rings 5.2 · gradients 6.7 · IR scene 5.9 | scaling table exposed the per-band fixed cost (1.0×→2.1× as bands shrink) — first recorded optimization target |
| F5 text | glyph blits 1.37 · text scene 1.86 | 19 cached masks = 2,209 B RAM; rasterize-once proven by counters |

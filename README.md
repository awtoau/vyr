# vyr

**An IR-native UI renderer & embedded GUI toolkit, in Rust.**

vyr renders the [vyvanse](https://awto.au) IR — a canonical, backend-neutral
description of embedded UIs — *directly*: no lowering to any other framework's
widget vocabulary. It is being built in three stages:

1. **Reference renderer** — the executable spec. Existing backends
   (LVGL, TouchGFX, Qt, Flutter) are scored against vyr's pixels with
   per-property tolerances.
2. **Editor canvas** — the vye editor's live surface renders IR truth, not any
   one framework's interpretation of it.
3. **Embedded GUI toolkit** — the endgame: hand it an existing LVGL or
   TouchGFX project (imported to IR) and it runs, on-device, on one runtime.

**Status: animating.** F1 (polygon-only tiny-skia painter, byte-exact band
equivalence), F2 (ns/px perf gate + scaling-law assertion), F3 (IR rendered
natively from the `vy_` vocabulary; clip stack; dirty rects +
`render_incremental`), F5 (TTF text via skrifa + rasterize-once glyph cache —
the FULL text path is `no_std`), F6 (PNG assets: decode in the cli,
deterministic integer blits in core) are landed, vyr runs live as the fifth
backend in the awto-vyvanse render farm, and F18 — the rig — drives a
600-frame deterministic animation with incremental==full proven byte-exact
EVERY frame, a resolution ladder to 4K (incremental repaint runs 4K at 14×
the 60 fps budget), and cross-ISA goldens: the same run replayed on emulated
ARMv7 is hash-identical on all 600 frames. F9 phase 1: vyr **boots on an
emulated Cortex-M4** (`./dev.py qemu-m4` — real vector table + crt0,
128 KiB-SRAM budget) and renders a 480×270 banded frame byte-identical to
x86-64, heap peak 106 KB with an 8 KB ASCII-subset font
([measured numbers](docs/measurements/f9-static.md)). See the
**[milestone gallery](docs/milestones/README.md)** — the renderer's history
in its own golden pixels — and the **[perf history](docs/perf/index.html)**.
The full plan — architecture, day-1 invariants I1–I8, the feature tracks —
is [`docs/plan.md`](docs/plan.md). Work is tracked as GitHub issues, one per
feature track. Everything runs as ONE operation: `./dev.py ci`.

## Design pillars

- **Flutter-concept architecture, clean-room**: constraints-down/sizes-up
  layout, render-node-owns-paint, an abstract `Canvas` — the protocol, never a
  port.
- **Software rasterization for determinism**: [tiny-skia] first (Skia's CPU
  algorithms in pure Rust), pinned; goldens are byte-stable across machines.
- **Partial framebuffer is the only code path** — a full frame is just
  `area = screen`. Built for small-RAM targets from the first commit.
- **Performance is measured from day 1**: ns/px as the canonical metric,
  linear band-area scaling asserted in CI, nightly regression gate.
- **`vyr-core` is `no_std + alloc`** — the door to STM32-class MCUs stays
  open, and the embedded verdict will be made on measured numbers.

[tiny-skia]: https://github.com/linebender/tiny-skia

## Workspace

| Crate | What |
|---|---|
| `vyr-core` | `no_std + alloc`: IR model, render tree, layout, clip stack, dirty rects, `Canvas` trait, painter, text/asset registries, counters |
| `vyr-cli` | std shell: IR JSON in → PNG out (render-farm contract), text measure; decode/encode lives here |
| `vyr-bench` | deterministic ns/px benches, committed baseline, the scaling-law assertion (run\|record\|check) |
| `vyr-size` | the F9 measurement vehicle: real linked ELFs on the STM32F427 memory map (`./dev.py size-mcu`) |
| `vyr-rig` | the F18 rig: deterministic 60 fps animation (hash-chain golden), resolution ladder to 4K, cross-ISA replay (`./dev.py anim` / `ladder` / `perf-history`) |

## License

GPL-3.0-only, with commercial licenses available — see
[`LICENSING.md`](LICENSING.md). Contact: dan@awto.au.

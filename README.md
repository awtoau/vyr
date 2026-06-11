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

**Status: pre-F1 skeleton.** The crate layout and the two day-one contracts
are real (`render(tree, area, buf, stride)` partial-framebuffer entry point;
always-compiled counters); nothing paints yet. The full plan — architecture,
day-1 invariants I1–I8, feature tracks F1–F14, milestones — is
[`docs/plan.md`](docs/plan.md). Work is tracked as GitHub issues, one per
feature track.

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
| `vyr-core` | `no_std + alloc`: IR model, render tree, layout, `Canvas` trait, painter, counters |
| `vyr-cli` | std shell: IR JSON in → PNG out (render-farm contract); decode/encode lives here |
| `vyr-bench` | criterion benches, ns/px baselines, the scaling-law assertion |

## License

GPL-3.0-only, with commercial licenses available — see
[`LICENSING.md`](LICENSING.md). Contact: dan@awto.au.

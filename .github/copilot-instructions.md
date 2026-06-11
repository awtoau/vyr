# Copilot instructions for vyr

vyr is an IR-native UI renderer (reference oracle → editor canvas → embedded
GUI toolkit) in Rust. Read `docs/plan.md` before non-trivial work; the day-1
invariants I1–I8 there are hard rules. The ones you must never violate:

- `vyr-core` is `no_std + alloc`. Never add `std` imports, filesystem, clock,
  thread, or std-only dependencies to `vyr-core`. Decode/encode, I/O, and
  timing belong in `vyr-cli`.
- `render(tree, area, buf, stride)` is the ONLY render entry point. Never add
  a full-frame special case; never assume the buffer origin is the screen
  origin — `area` may be any band.
- Every new primitive or widget PR must include: its criterion bench
  (`vyr-bench`), its golden fixture, and no change to existing goldens or the
  committed perf baseline (baseline changes are separate, explicitly reviewed
  PRs).
- No default chrome: if the IR didn't specify a border/radius/padding/colour,
  do not paint one.
- Honest failure: unknown widget types return `RenderError::UnknownWidget`
  before any painting; never render a silent blank.
- Determinism: no `SystemTime`/`Instant`, no randomness, nothing
  machine-dependent anywhere in the render path.
- `#![forbid(unsafe_code)]` stays.

Only pick up issues labelled `copilot-ok`. Do not modify the `Canvas` trait,
the layout protocol, IR semantics, `docs/plan.md`, licensing files, or this
file.

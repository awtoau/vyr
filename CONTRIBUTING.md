# Contributing to vyr

Start with [`docs/plan.md`](docs/plan.md) — especially the **day-1 invariants
I1–I8**. PRs are reviewed against them, not just against "does it work".

## The short version of the invariants

- **I1** — `render(tree, area, buf, stride)` is the only render path. Never
  add a full-frame special case; never assume buffer origin == screen origin.
- **I2** — determinism: no clocks, no randomness, no float environment
  dependence in render; goldens are byte-stable.
- **I3/I4** — every new primitive or widget ships **with its bench** (ns/px)
  and its golden, in the same PR. The scaling-law assertion (flat ns/px across
  band sizes) must stay green; a perf-baseline change is its own reviewed
  commit, never a drive-by.
- **I5** — IR-authoritative chrome: no default borders/radii/padding/colours,
  ever. If the IR didn't say it, we don't paint it.
- **I6** — honest failure: unknown widget = hard error before pixels;
  not-yet-composable widget = labelled placeholder; a blank render is a bug.
- **I7** — `vyr-core` stays `no_std + alloc`: no filesystem/clock/thread/std
  deps in core. Decode, I/O, and timing live in `vyr-cli`.

## Workflow

- Work is tracked as issues; the feature tracks F1–F14 from the plan are the
  top-level ones, broken into sub-issues as they start.
- Issues labelled **`copilot-ok`** are mechanical/self-contained and suitable
  for the GitHub Copilot coding agent (assign the issue to Copilot). Anything
  touching the `Canvas` trait, the layout protocol, IR semantics, or the
  invariants is **not** `copilot-ok` — human (or at least non-mechanical)
  design work.
- **Every PR gets an AI review pass (Claude) before human merge.** Currently
  run from a maintainer's machine (`/code-review` on the PR); the repo also
  carries a ready-to-enable GitHub Action in `.github/workflows-disabled/`
  (move into `workflows/` and add `ANTHROPIC_API_KEY` to enable — kept
  disabled by default because project CI is local-only by policy).
- Commit style: conventional-ish (`feat(core): …`, `fix(cli): …`); sign off
  (`git commit -s`, see [`LICENSING.md`](LICENSING.md)).

## Build

```
cargo check --workspace
cargo test --workspace
```

`vyr-core` must also pass `cargo check -p vyr-core --target thumbv7em-none-eabihf`
once F1 lands (the no_std gate; CI will enforce it).

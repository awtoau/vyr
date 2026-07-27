---
name: run-vyr
description: Build, run, render, and drive the vyr IR renderer. Use when asked to run/build/test/smoke-test vyr, render an IR scene to PNG, screenshot vyr output, check a widget renders, or run the vyr gate (ci). vyr is a Rust IR→PNG renderer (no GUI/server) plus the ./dev.py driver and an M4 perf/size harness.
---

# Run vyr

vyr (`awtoau/vyr`) is a Rust **IR renderer**: IR JSON in → RGB888 **PNG** out. There is **no GUI and no server** — you "run" it by rendering a scene to a PNG and looking at the PNG. Two driver layers:

- **`.claude/skills/run-vyr/smoke.sh`** — builds `vyr-cli` and renders the built-in demo + a custom IR scene to PNG, then **verifies the pixels are real** (decodes each PNG, asserts >1 distinct colour — the project's "blank ≠ rendered" honesty invariant). Start here.
- **`./dev.py`** — the project's full driver (build, test, the `ci` gate, M4 size/perf, the metrics ledger). `./dev.py describe` prints the machine-readable command list.

All paths below are relative to the repo root (the unit). The driver lives at `.claude/skills/run-vyr/smoke.sh`.

## Prerequisites

Already present on this machine; install only if missing (Fedora shown — adapt to your distro):

- **Rust toolchain** (`cargo`) — the build.
- **python3** — the smoke PNG-verifier and `./dev.py`.
- **qemu-system-arm** — *optional*, only for the M4 perf/size gate (`./dev.py qemu-m4`, the `ci` M4 step). Absent ⇒ that step is **skipped, not failed**. (`dnf install qemu-system-arm` / `apt-get install qemu-system-arm`.)

No `apt-get` was needed in this container — the toolchain was already installed.

## Run (agent path) — the smoke driver

```bash
.claude/skills/run-vyr/smoke.sh          # build + render demo + custom IR + verify non-blank
.claude/skills/run-vyr/smoke.sh --gate   # also run ./dev.py ci --quick (the full gate)
```

Output PNGs land in `./tmp/`: `smoke-selftest.png` (the demo scene), `smoke-selftest-text.png` (the text fixture), `smoke-scene.png` (a custom frame+chart+gauge+slider+toggle scene). Open them to see the render. Expected tail:

```
  PASS selftest demo: 120x120, 208 distinct colours -> tmp/smoke-selftest.png
  PASS custom IR (frame+chart+gauge+slider+toggle): 240x160, 172 distinct colours -> tmp/smoke-scene.png
SMOKE OK — vyr builds and renders real pixels.
```

## Render your own scene

`vyr-cli` is the std shell (`IR JSON → PNG`). Build once, then:

```bash
cargo build --release -p vyr-cli
./target/release/vyr-cli selftest-png tmp/out.png        # built-in demo (no IR needed)
./target/release/vyr-cli render <scene.json> tmp/out.png # render an IR file
./target/release/vyr-cli measure roboto 14 "hello"       # text metrics as JSON on stdout
```

An IR file is `{"w":W,"h":H,"root":{"name":"view","children":[ ...widgets... ]}}`. A widget is `{"name":"vy_<kind>","attrs":{"x":..,"y":..,"width":..,"height":..,...}}`. Kinds incl. `vy_frame`, `vy_chart` (`points`=CSV series, `chart_type`=line|bar), `vy_gauge`, `vy_slider`, `vy_toggle`, `vy_label`, `vy_image`, `vy_circle`, … A bad/unknown widget or a missing image `src` is a **hard exit 2** (the farm's honest-FAIL contract), never a blank box.

## Drive via ./dev.py (build / test / gate / measure)

```bash
python3 dev.py describe        # JSON list of every command (agents: discover here)
python3 dev.py selftest        # render the demo PNG via vyr-cli
python3 dev.py test            # cargo test --workspace
python3 dev.py ci --quick      # THE gate: fmt+clippy+tests+check-mcu+M4+bench+size+ladder (~4s)
python3 dev.py qemu-m4 --draft # M4 instruction-count + heap (needs qemu-system-arm)
python3 dev.py track           # record one measurement-ledger row → docs/perf/ledger.db (SQLite) + page
```

`./dev.py ci --quick` is the run-after-every-change gate; the full `./dev.py ci` adds the cross-ISA ARM replay. Both end with `track`, which appends one row to the single measurement ledger.

## Direct invocation (internal code, no full app)

Most PRs touch `vyr-core` (the `no_std + alloc`, `forbid(unsafe_code)` renderer). Exercise it directly with the byte-exact golden tests — they render fixtures and assert FNV-1a hashes + band-equivalence:

```bash
cargo test --manifest-path vyr-core/Cargo.toml                 # all golden + conformance tests
cargo test --manifest-path vyr-core/Cargo.toml --test draft_golden --test chart_golden
VYR_TEST_DUMP=1 cargo test --manifest-path vyr-core/Cargo.toml --test chart_golden  # dumps PNGs to ./tmp/ to eyeball
```

Re-bless a golden only when a pixel change is intended: `VYR_BLESS=1 cargo test … <test>` prints the new hash to paste into the test.

## Gotchas

- **It's a CLI, not a GUI.** "Run vyr" means *render a PNG and open it*. There is no window. The smoke driver's verify step is your "did it actually draw something" check.
- **`blank ≠ rendered`** is a hard project rule. A crash/segfault must surface as a non-zero exit, never a blank PNG. The smoke verifier decodes the PNG and asserts >1 colour for exactly this reason.
- **The M4 `insns/frame` number is INDICATIVE, not deterministic.** It's derived from ARM semihosting `SYS_CLOCK`, which is **wall-clock-influenced** on a plugin-less qemu (jitters ±1 cs ≈ ±0.5 M/frame, and spikes under host build-load). The **deterministic** signals are the cross-ISA **frame hash** and the host **ns/px** bench (`./dev.py bench`/`ladder`). `./dev.py qemu-m4` hard-gates the hash + heap, warn-only on insns.
- **Fonts/images load in the shell, not the core** (invariant I7). `vyr-cli` reads `./fonts/*.ttf` (or `$VYR_FONTS`); text with no registered font is a **loud hard error**, not tofu boxes. Image `src` paths resolve under `$VYR_ASSETS` or CWD.
- **`./dev.py` logs to stderr + `./tmp/*.log`** (the awto convention) — numbers you grep for are on stderr, not stdout.
- **Perf builds pin `CARGO_INCREMENTAL=0`** (in `qemu-m4`/`bench`): incremental codegen-unit boundaries shift with build *order*, which perturbs the measured insns/ns. Don't "fix" a perf number by rebuilding — the binary is the variable.

## Troubleshooting

- **`qemu-system-arm not on PATH`** → install it for the M4 gate, or ignore: `ci` skips that step (logs "skipping the M4 insn-count gate"), it does not fail.
- **`render failed` / exit 2** → the IR is invalid (unknown `vy_` name, missing image `src`, zero-size widget, junk attr). The error line names the cause. This is by design (honest failure), not a crash.
- **Text renders nothing / `UnknownFont`** → no font registered. Confirm `./fonts/*.ttf` exists or set `$VYR_FONTS`; the shell WARNs the dir it tried.
- **A golden test "drifted"** → pixels changed. If unintended, that's a real regression — fix the code. If intended, re-bless with `VYR_BLESS=1` and commit the new hash.

# vyr — agent rules

Read `docs/plan.md` before non-trivial work; the **day-1 invariants I1–I8**
there are hard rules. Conventions detail: [`docs/conventions.md`](docs/conventions.md).
Global awto rules apply (canonical: `~/git/awto-dan/code/vscode/AGENT-RULES.md`).

## The load-bearing rules

- **`vyr-core` is `no_std + alloc`** — no std imports, filesystem, clock,
  thread, or std-only deps in core. Decode/encode, I/O, timing live in
  `vyr-cli`. Gate: `./dev.py check-mcu` (thumbv7em-none-eabihf) must pass.
- **`render(tree, area, buf, stride)` is the only render path** — never a
  full-frame special case; never assume buffer origin == screen origin.
- **Band equivalence is byte-exact** — the painter feeds tiny-skia ONLY
  polygons (own deterministic flattening, 1/64-px world quantization, exact
  integer translation). Never hand tiny-skia a curve or a stroke; never add
  an adaptive/transform-dependent flattening. `tests/golden.rs` enforces.
- **Every new primitive/widget ships with its bench + golden in the same PR.**
  Goldens/baselines change only as their own reviewed commit (re-bless:
  `./dev.py test --bless`, then commit the printed hash). Never invoke with
  env-var prefixes (`VYR_BLESS=1 cargo …`) — they break permission-allowlist
  token matching; dev.py flags set the env internally.
- **No default chrome** (IR-authoritative) and **honest failure** (unknown
  widget = hard error before pixels; a blank render is a bug).
- **Determinism**: no `SystemTime`/`Instant`/randomness in core; float math
  through `libm`; pinned `rust-toolchain.toml` + `Cargo.lock` (bumps are
  reviewed changes that may re-bless goldens).

## dev.py contract

- discover: `./dev.py describe` → JSON
- `./dev.py test` (workspace tests) · `check` · `check-mcu` (no_std gate) ·
  `clippy` · `fmt-check` · `selftest` (render demo PNG to tmp/)
- All output timestamped; script logs → `./tmp/<name>.log`.

## Repo specifics

- License GPL-3.0-only + commercial — do not add code copied from LVGL,
  TouchGFX, Qt, Flutter, or Skia (architecture-from-docs only); deps must be
  permissive (MIT/BSD/Apache).
- vyr never imports vyvanse; the contract is IR JSON + `schema_version`
  (fixtures committed here).
- Commit style: `feat(core): …` / `fix(cli): …`, sign-off (`git commit -s`).

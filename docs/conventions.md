# vyr conventions (awto baseline, adapted for Rust)

Distilled from the awto global rules (`~/git/awto-dan/code/vscode/AGENT-RULES.md`)
and the conventions proven in awto-vyvanse / awto-l8-app. Where this repo is
public and others may contribute, the rules are restated here rather than
referenced privately.

## Regular patterns — apply a pattern completely, never by ROI

When a pattern is introduced — a cache, a resolve-once, a validation approach, an
error shape, a naming scheme — apply it to **every** case it fits, in the same
pass. The value of code is in being **regular**: the next reader must be able to
trust "attributes are resolved once" without checking each one; the next change
must be able to follow the established shape without discovering exceptions.

- **ROI decides whether to introduce a pattern, never whether to finish it.**
  "Geometry was 13 % but colours only 1 %, so I stopped" leaves the code
  irregular — some attributes cached, some re-parsed — which is *worse* than
  either extreme: a trap the next person steps in. If a pattern is worth doing it
  is worth doing everywhere it applies; if it is not worth doing everywhere,
  reconsider doing it at all.
- **The cost of irregularity is not measured in the profiler.** It is the reader
  who can no longer trust an invariant, and the change that breaks because it
  followed the pattern into a place the pattern was never finished.
- Half-applying a pattern and calling the work done is the specific failure this
  rule exists to prevent (learned on #34: geometry + colours resolved-once, but
  `value`/`text`/`font` left re-parsing — regularity, not the extra 1 %, is why
  it had to be finished).

## Logging — heaps of timestamps, logs everywhere

The single most-load-bearing convention. Every tool in this repo logs **every
line with a timestamp**, mirrored to **both** the live stream (stderr) and a
**workspace-relative file** for retroactive review:

```
HH:MM:SS.ffffff UTC  LEVEL [origin] message
05:26:44.775190 UTC  INFO  [vyr-cli] rendered in 2.834 ms (196.8 ns/px), stats: …
```

- Levels: `FATAL`/`ERROR` (stderr-red family), `WARN`, `INFO`, `DEBUG`
  (opt-in), `ALERT` (success/done).
- Files: `./tmp/<name>.log`, append mode. `./tmp/` is gitignored. Never
  system `/tmp`, never silent stdout-only.
- Long operations log intermediate progress with timestamps — an agent (or a
  human three weeks later) must be able to reconstruct the timeline from the
  log alone.
- Rust core caveat: `vyr-core` has **no clock** (no_std, determinism) — core
  reports *counters*; the shell (`vyr-cli`, benches) owns *timing* and the
  timestamps.

## Time periods need reasons

Every timeout, delay, interval, or duration in code or scripts carries a
comment: what is being waited for, why that duration, what happens on expiry.
No bare `sleep`. (Agents have no concept of time; embedded operations are
µs/ms — unjustified durations are wrong by orders of magnitude.)

## Scripts, tmp, debris

- Multi-step logic → `./scripts/<name>` (or `./dev.py` subcommand), never
  inline shell one-liners. Script output → `./tmp/<name>.log`.
- `./tmp/` = scratch + logs + pidfiles (gitignored). `./debris/` = tracked
  archive for retiring NON-regenerable content only (move, don't delete);
  regenerable artifacts are just deleted — the rebuild is the migration.

## dev.py — the canonical entry point

One discoverable entry point per repo; AI agents enumerate it via
`./dev.py describe` (JSON). This repo's commands: `describe`, `test` (honours
`VYR_BLESS=1`), `check`, `check-mcu` (the no_std/thumbv7em gate), `clippy`,
`fmt-check`, `selftest` (demo PNG + ns/px to `./tmp/`), `track` (the one
measurement-ledger writer — see below). Return codes:
0 success, non-zero failure, 2 usage/unimplemented.

## The measurement ledger — ONE file, ONE writer

`docs/perf/history.jsonl` is **the** canonical measurement history: append-only,
committed, one row per run, `"schema": 3` (the matrix — platform × tier ×
opt-level cells, each with render-only, the benchmark's own fold and the total
as separate fields). `docs/perf/index.html` — one indexed line chart plus flat
sortable tables of every value in that file, and nothing else — is regenerated
from it and is a pure derived artifact. There is no second ledger
(`docs/metrics/` was retired in #25).

- **One writer: `./dev.py track`** (`scripts/ledger.py`). Nothing else appends.
  It *measures nothing* — it ingests the artifacts the measuring commands leave
  in `./tmp` (`ladder`, `anim`, `size-mcu`, `qemu-m4`, `scripts/qemu-insn.py`,
  `scripts/board-run.py`) plus the committed `vyr-bench/baseline.json`, so
  `./dev.py ci` measures each quantity exactly once and records it once.
- **Sections are independent and optional** — `matrix` (the instruction/heap/
  flash/coverage cells), `ladder`, `anim`, `arm`, `bench`, `size`, `m4_qemu`,
  `silicon`, `board_anim`, `derived`. A row that omits a section did not measure
  it. **Sparse rows are honest; a back-filled or interpolated value is not.**
  Nothing is ever written that was not measured, and old-instrument rows are
  regenerated by a replay rather than carried forward (the rebuild IS the
  migration).
- **Provenance travels with the number**: tool + version, ELF SHA-256, the
  upstream commit behind the LVGL anchor, the emulated/real distinction. A
  number whose source is not recorded is not an anchor.
- **Discredited numbers are relabelled, never silently carried.** SYS_CLOCK
  readings from a plugin-less qemu are host wall time; they live under
  `superseded` and are never charted (`docs/performance.md` §5).
- **The ledger has one format.** `ledger.py` refuses a row of any other schema
  rather than growing a read-old-format path; a schema change is a one-shot
  rewrite of the file, reviewed as a diff.
- **One suite per ledger; a test change is a REPROCESS, never an append (#43).**
  The ledger records one `suite_fingerprint` — a hash of *what is measured* (the
  vyr fixture, band height, frame counts, tiers, the LVGL scene + config +
  upstream commit; `./dev.py perf-suite`). Changing the tests changes that
  fingerprint, so `./dev.py track` **refuses to append** and you replay the
  whole history under the new suite instead (the rebuild IS the migration).
  Same reflex as an instrument change; there is never a mix of suites to
  reason about.

## Goldens & perf baselines

- Golden hashes and perf baselines are committed; CI compares nightly.
- Re-blessing is its own reviewed commit with the reason in the message —
  never a drive-by alongside a feature.
- `./dev.py test --bless` prints new hashes; `./dev.py test --dump` writes
  PNGs to `./tmp/` for eyeballing. (dev.py sets `VYR_BLESS`/`VYR_TEST_DUMP`
  internally — never put env-var prefixes on the command line; they break
  permission-allowlist token matching.)

## Commits

Conventional style: `feat(core): …`, `fix(cli): …`, `docs(plan): …`,
`test: …`, `chore: …`; reference issues `(#N)`; DCO sign-off
(`git commit -s`); imperative subjects under ~70 chars.

# vyr conventions (awto baseline, adapted for Rust)

Distilled from the awto global rules (`~/git/awto-dan/code/vscode/AGENT-RULES.md`)
and the conventions proven in awto-vyvanse / awto-l8-app. Where this repo is
public and others may contribute, the rules are restated here rather than
referenced privately.

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
`fmt-check`, `selftest` (demo PNG + ns/px to `./tmp/`). Return codes:
0 success, non-zero failure, 2 usage/unimplemented.

## Goldens & perf baselines

- Golden hashes and perf baselines are committed; CI compares nightly.
- Re-blessing is its own reviewed commit with the reason in the message —
  never a drive-by alongside a feature.
- `VYR_BLESS=1` prints new hashes; `VYR_TEST_DUMP=1` writes PNGs to `./tmp/`
  for eyeballing.

## Commits

Conventional style: `feat(core): …`, `fix(cli): …`, `docs(plan): …`,
`test: …`, `chore: …`; reference issues `(#N)`; DCO sign-off
(`git commit -s`); imperative subjects under ~70 chars.

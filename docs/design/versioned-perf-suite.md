# Design: the perf ledger is single-suite, reprocessed on change (#43)

**Status:** decided and implemented.
**Supersedes** an earlier draft of this doc that proposed per-row `suite_version`
labels and a chart that refused to join across versions. That was more machinery
than the repo's own rule needs.

---

## 1. The problem

The ledger is comparable end to end because history was replayed with one
instrument. That holds only while *what is measured* holds still. Improving the
test — a new scene, a fixed vyr↔LVGL divergence, a changed band height or frame
count — changes the **meaning** of the numbers, and rows before and after stop
being comparable. This already happened: the LVGL scene changed at `56da347`, a
**23 %** shift, and earlier rows silently became measurements of a different
thing.

## 2. The decision

**The ledger is single-suite by construction.** It is never incrementally grown
across a test change; it is **fully reprocessed** — replayed over git history
under the current suite — whenever the tests change. This is exactly the repo's
existing rule, *"derived artifacts have no legacy; the rebuild IS the
migration,"* extended from "the instrument changed" to "the tests changed." The
same reflex already used this session to drop old-instrument rows and replay.

Consequences, all simplifying:
- there is only ever **one** suite in a ledger, so there is nothing to version
  per row and nothing for the chart to refuse to join;
- no manual `v1`/`v2` labels, no "did you remember to bump";
- improving a test is cheap and expected: change it, reprocess (~11 min
  parallel replay; the board leg is a snapshot, run when wanted).

## 3. The one guard

A full reprocess is the only correct way to fold a test change into the ledger.
The path that could violate that is **`./dev.py track`, which *appends*** a row
for the current commit. If the suite changed and someone appends, the new row
measures a different thing than the rest.

So the ledger records **one** `suite_fingerprint` (in its schema-note, not per
row), and `append_row` **refuses to append when the current suite no longer
matches**, pointing at the reprocess command. That converts "reprocess when the
tests change" from a discipline into a gate — the same spirit as the f64 gate.

## 4. What the fingerprint covers

`scripts/perf-suite.py` hashes the inputs that change *what is measured*, as
**named spans and values, never whole files** (a renderer or harness edit that
does not touch the measured work must not move it — that false positive is what
would get the guard disabled):

- **vyr** — `FIXTURE_IR`, `FIXTURE_W`/`H`, `BAND_H`, `TIMED_FRAMES`, the tier set;
- **LVGL** — the `build_scene` body (delimited by `/* SUITE:lvgl_scene:start */`
  … `/* SUITE:lvgl_scene:end */` markers in `main.c`, so the boundary is visible
  in the source), its `TIMED_FRAMES`, `lv_conf.h`, and the **upstream LVGL
  commit** the mirror is at.

**LVGL is in the fingerprint** (the #43 decision): a large upstream change alters
the vyr↔LVGL ratio's meaning, so an LVGL bump is just another reprocess trigger.
This refines #26's "deliberately not pinned" — the anchor still tracks upstream,
but *moving* upstream now obliges a reprocess instead of silently drifting.

`opt-level` is **not** in the fingerprint: the same scene at `-Oz` vs `-O2` is the
same measured work, differently compiled — a matrix column (#33), not a suite
change.

## 5. Everything as data (kept from the earlier draft, optional but nice)

- `scripts/perf-commits.txt` — the replay set as reviewable data rather than a
  `--from/--to` range someone typed once. *(Not yet implemented; the replay still
  derives the range from git. A follow-up, low priority.)*

## 6. Rollout — done

1. ~~markers around `build_scene`~~ ✓
2. ~~`scripts/perf-suite.py` — `fingerprint()` over the named inputs; `--compare`~~ ✓
3. ~~ledger stamps `suite_fingerprint` on every reprocess~~ ✓
4. ~~`append_row` refuses on mismatch, pointing at the reprocess~~ ✓
5. ~~`./dev.py perf-suite [--compare]`~~ ✓
6. `docs/conventions.md`: a test change obliges a reprocess, never an append. ✓

The commit-list-as-data (§5) is the only open follow-up, and it is a convenience,
not a correctness requirement — the reprocess + fingerprint guard is what makes
the ledger permanently single-suite.

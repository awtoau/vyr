# Design: a versioned performance suite (#43)

**Status:** design — reviewed before code (the `design` label convention).
**Motivates:** every future improvement to *what* we measure.
**Builds on:** the harness rebuild (#31→#45), which made the ledger correct *once*;
this makes it stay trustworthy *as the tests evolve*.

---

## 1. The problem this exists to prevent

The ledger is now comparable end to end because history was replayed with **one
instrument**. That guarantee holds only while *what is measured* holds still. The
moment anyone improves the test, it breaks:

- add a video / animation scene, or a more complex one;
- fix a vyr↔LVGL content divergence (~9.6 % of the frame still differs —
  `scripts/lvgl-m4-bench/compare.md`) to make the comparison more equivalent;
- change the fixture, the band height, the frame count, the tier set.

Every one of those changes the **meaning** of the numbers. Rows measured before
and after stop being comparable, and nothing in the current design records that.
**This is not hypothetical — it already happened:** the LVGL harness scene
changed at `56da347` (it had been drawing a tick-marked scale, labels and a knob
vyr never drew), a **23 % difference**, and the earlier rows silently became
measurements of a different thing. The perf-history doc's error 4 and its
recurrences are the same disease in a different organ.

The fix is not "be careful." It is to make the meaning of every number
**machine-checkable**, so a suite change that would silently invalidate history
becomes a loud, mechanical operation instead.

---

## 2. Three separable things, and why the separation *is* the design

| piece | is | changes | who owns identity |
|---|---|---|---|
| **the harness** | *how* to measure — plugin QEMU + libinsn, DWT_CYCCNT, the counting allocator, the two-build #45 split | rarely | its own version + sha (already recorded: `harness.version`, `harness.sha256`) |
| **the suite** | *what* to measure — the vyr fixture IR, band height, frame count, tier set, and the LVGL-side scene | **often, as tests improve** | **`suite_version` — the new thing** |
| **the subject** | *which commit* is measured | every run | it **is** the commit |

The harness already carries its own identity into every row. The subject is the
commit. The missing axis is the **suite** — and it is missing precisely because
it was assumed constant, which is the assumption `56da347` violated.

Two rows are comparable **iff their `suite_version` matches** (given the same
harness). That single rule is the whole feature.

---

## 3. `suite_version`: a label bound to a fingerprint

The issue floats a manual label (`--suite v3`). A bare manual label has the same
failure mode as "be careful": someone changes the scene and forgets to bump. So
the design is a **label bound to a content fingerprint**, and the harness
**refuses to run** if they disagree.

- **`suite_version`** — a human-readable label, `v1`, `v2`, … Monotonic, never
  reused. This is what a ledger row records and what a human reasons about.
- **`suite_fingerprint`** — a hash over the *inputs that change the meaning of
  the numbers* (§4). Computed from the suite definition at run time.
- **the suite registry** — a committed data file mapping each label to the
  fingerprint it was blessed at.

At the start of every measurement the harness computes the fingerprint of the
current suite and checks it against the registry entry for the declared
`suite_version`. Three outcomes:

- **match** → run, stamp every row with `suite_version`.
- **mismatch** → **hard error**: "the suite content changed under `v2`; bump to
  `v3` (`./dev.py perf-suite --bless v3`) and re-replay, or revert the change."
- **unknown label** → the label is new and being blessed; record the fingerprint.

Forgetting to bump is now impossible to do silently: the fingerprint moved, the
label didn't, the harness stops. This is the load-bearing safety, and it is the
one thing a manual-label-only scheme cannot give.

---

## 4. What the fingerprint covers — and what it must NOT

The fingerprint hashes **the definition of the measured work**, not the code that
does the work. Getting this boundary right is the whole subtlety: the fixture
lives in `vyr-size/src/workload.rs`, which also contains renderer-adjacent code
that is *subject*, not *suite*.

**In the fingerprint (changes meaning):**

- the vyr fixture IR string (`FIXTURE_IR`), and the animated scene identity when
  `rig` is used (`vyr_scene::scene_id` + preset/detail);
- `BAND_H`, `FIXTURE_W`, `FIXTURE_H`;
- `TIMED_FRAMES` (and the rig frame count);
- the **tier set** measured (`exact`, `fast`, `draft`);
- the **LVGL-side scene** — `build_scene()` in `scripts/lvgl-m4-bench/main.c`,
  its `TIMED_FRAMES`, and its `lv_conf.h` feature set;
- the opt-level *axis* is **not** in the fingerprint — it is a recorded cell
  attribute (a dimension of the matrix), not a change in what is measured.

**NOT in the fingerprint (that is the subject or the instrument):**

- any renderer code — vyr-core, tiny-skia, the painter, the memo;
- the harness scripts, the plugin, the QEMU build (those are `harness.*`);
- the LVGL renderer version (recorded as provenance; the anchor deliberately
  tracks upstream — #26 — so it is not part of the suite identity, but see the
  open question in §8).

Because the fixture and the LVGL scene are embedded in files that also hold
non-suite content, the fingerprint must hash **extracted, named inputs**, never
whole files. Concretely: a small suite manifest (§5) *names* each input (a Rust
`const`, a C function body, a set of numeric parameters) and the fingerprint is
the hash of those extracted spans plus the parameter values. A whole-file hash
would make every renderer edit look like a suite change — the exact false
positive that would make people disable the check.

---

## 5. Everything as data: the suite manifest and the commit list

Two things move from code to reviewable data.

### 5.1 The suite manifest — `scripts/perf-suite.json`

```jsonc
{
  "suite_version": "v1",
  "scene": { "w": 480, "h": 270, "band_h": 16, "timed_frames": 20 },
  "tiers": ["exact", "fast", "draft"],
  "vyr_fixture": {          // where the measured IR lives, so it can be hashed
    "file": "vyr-size/src/workload.rs",
    "const": "FIXTURE_IR"
  },
  "lvgl_scene": {
    "file": "scripts/lvgl-m4-bench/main.c",
    "fn": "build_scene",
    "timed_frames": 40,
    "lv_conf": "scripts/lvgl-m4-bench/lv_conf.h"
  },
  "registry": {             // label -> the fingerprint it was blessed at
    "v1": "sha256:…"
  }
}
```

The manifest is *inspectable without reading Python*: what scene, what tiers,
what frame counts, and where the two fixtures live. `./dev.py perf-suite`
computes the current fingerprint and prints match/mismatch; `--bless <vN>`
records a new label. The manifest is committed; a suite change is a diff to it
plus a bless, reviewed like any other change.

### 5.2 The commit list — `scripts/perf-commits.txt`

Today `perf-replay.py` derives the replay set from a git range
(`--from e08aa63 --to HEAD`). That is code-ish and non-reproducible (HEAD moves).
Replace it with a committed file — one build-representative commit per line, with
a comment — so the replay set is **reviewable data**:

```
# The M4 vehicle history. One commit per distinct build state is enough;
# perf-replay dedupes by build_key and records the rest as `covers`.
e08aa63   # F9 dynamic half — vyr BOOTS
5da42a2   # F16 Draft tier
…
```

`--from/--to` stays as a convenience for ad-hoc ranges, but the canonical replay
reads the file, so "what history is charted" is a reviewable artifact, not a
flag someone typed once.

---

## 6. One entry point, three ways

The issue asks for one script usable three ways. Today it is two
(`perf-harness.py` + `perf-replay.py`). The design keeps that split — replay is a
genuinely different concern (worktrees, sharding, build-key grouping) and folding
it into the harness would bloat the single-ref path — but presents **one command
surface** via `./dev.py`:

```
./dev.py perf --ref HEAD                 # after every change: one ref, the matrix
./dev.py perf --replay                   # rebuild history from perf-commits.txt
./dev.py perf --replay --suite v3        # after changing the tests: bump + replay
./dev.py perf-suite                      # print current fingerprint vs registry
./dev.py perf-suite --bless v3           # bless a new suite version (reviewed)
```

`--replay --suite v3` is the mechanical "our numbers changed meaning" operation:
bless v3, regenerate the whole ledger under v3, every point shares one version.

---

## 7. The ledger and the chart

- **Every measurement row records `suite_version`** (beside `harness` and the
  commit). Schema 3 already has room — it is one more identity field.
- **The canonical ledger is single-version by construction.** A suite bump
  obliges a full replay (documented as a hard rule in `docs/conventions.md`), so
  every row in `history.jsonl` shares one `suite_version`. This keeps the chart
  simple and the invariant strong: there is never a mix to reason about.
- **The chart asserts it.** `ledger-verify.py` gains a check: all measurement
  rows share one `suite_version`; if not, the page **refuses to draw the trend**
  (tables still render — they are per-row facts) and says why. A belt to the
  replay's braces: if someone hand-appends a row under a new suite, the page
  fails loudly instead of drawing a false trend line.
- **The page header states the suite version and what it means** — one line, so a
  reader knows the scene, tiers and frame counts behind every number.

Why single-version rather than multi-version-with-breaks: a chart that draws
several suite eras with gaps invites exactly the cross-era eyeballing the whole
feature exists to prevent. Re-replay is cheap (the parallel replay does the full
history in ~11 min; the board leg is the only slow platform and is a snapshot
anyway). Cheap re-replay is what makes "improve the test freely" real.

---

## 8. Open questions (decide before code)

1. **LVGL upstream drift vs suite identity.** #26 makes the LVGL anchor track
   current upstream deliberately. But a big upstream LVGL change *does* change the
   meaning of the vyr↔LVGL ratio. Options: (a) leave LVGL version as provenance
   only (current) and accept the anchor can drift within a suite version; (b)
   pin the LVGL commit into the suite manifest so an LVGL bump is a suite bump.
   **Recommendation: (b)** — the anchor is part of "what is measured," and #26's
   "track upstream" becomes "bump the pin, which bumps the suite, which replays."
   That is the same cheap operation and it keeps the ratio honest. This reverses
   part of #26's "deliberately not pinned"; call it out in review.

2. **Fingerprint of a C function body.** Extracting `build_scene`'s body for
   hashing means a brittle brace-matched span or a marker-comment-delimited
   region (`/* SUITE:build_scene:start */ … /* SUITE:build_scene:end */`).
   **Recommendation:** marker comments — explicit, greppable, and they make the
   suite boundary visible in the source itself.

3. **Does `opt-level` really stay out of the fingerprint?** It is a matrix
   dimension (#33), and the same scene at `-Oz` vs `-O2` is the same *measured
   work*, differently compiled. Yes — out. Recorded per cell, not per suite.

4. **Retroactive `suite_version` for existing rows.** The current ledger predates
   this. On first rollout, the whole history is one suite (`v1`) by definition —
   nothing measured has changed since the rebuild. So: bless `v1` at today's
   fingerprint, stamp the existing rows `v1` in the next replay. No special
   migration path; the replay *is* the migration.

---

## 9. Rollout (once the questions above are settled)

1. Write `scripts/perf-suite.json` (manifest + `v1` fingerprint) and
   `scripts/perf-commits.txt` (the replay set as data).
2. Add `suite_fingerprint()` + the match/mismatch/bless logic; a `perf-suite`
   dev.py command.
3. Thread `suite_version` into every measurement row (harness + replay).
4. `ledger-verify.py`: assert single-version; the page header states it.
5. Document in `docs/conventions.md`: **a suite change obliges a bless + replay**;
   two rows compare only within a suite version.
6. Re-replay history under `v1`; commit the regenerated ledger.

Each step is small and independently reviewable. The design is the contract; the
code is mechanical once §8 is decided.

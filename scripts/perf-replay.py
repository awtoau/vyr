#!/usr/bin/env python3
"""perf-replay.py — replay the ONE canonical harness over git history.

The rule this script exists to enforce
--------------------------------------
**The instrument is constant; only the renderer varies.** Every commit is
measured with TODAY's `scripts/perf-harness.py`, TODAY's plugin QEMU and
TODAY's build profile — never with the tooling that shipped alongside it. The
previous sweep (`scripts/m4-history-sweep.py`) deliberately used each commit's
own `dev.py` "to be honest"; that is backwards, and it is why the old series
mixed a wall-clock era with a counted one and could not be compared to itself.

How it walks
------------
- A detached worktree (`/mnt/2tb/git_debris/vyr-perf-specimen`) so the primary
  checkout is never touched, with ONE shared cargo target dir wired in as a
  **symlink** at `<worktree>/target` — `dev.py` resolves binaries as
  `REPO/target/...` and ignores `CARGO_TARGET_DIR`, and the previous sweep
  learned that the hard way.
- Commits are grouped by `build_key` (the git trees of vyr-core, vyr-size,
  vyr-bench, Cargo.toml, Cargo.lock, rust-toolchain.toml). Every commit in a
  group compiles to byte-identical binaries, so the group is measured ONCE at
  the commit that introduced it and the rest are recorded as `covers` — not
  silently dropped, and not re-measured for show.
- A commit that cannot be built is recorded as a SKIP **with the reason**.

Output
------
  tmp/perf-replay.jsonl   one record per specimen (resumable: rerun skips
                          specimens already present)
  tmp/perf-replay.log

Usage
  python3 scripts/perf-replay.py                       # e08aa63..HEAD, canonical config
  python3 scripts/perf-replay.py --from cb29f52 --resume
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
OUT = TMP / "perf-replay.jsonl"
LOG = TMP / "perf-replay.log"
CACHE = TMP / "perf-elf-cache.json"

FIRST_M4 = "e08aa63"  # first commit with vyr-size/src/workload.rs (the M4 vehicle)

_lines: list[str] = []


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] [replay] {msg}"
    print(line, flush=True)
    _lines.append(line)
    TMP.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(_lines) + "\n")


def harness():
    spec = importlib.util.spec_from_file_location("perf_harness", REPO / "scripts" / "perf-harness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True,
                          check=False).stdout.strip()


def fan_out(a, groups: list[dict]) -> int:
    """Run N shards in parallel, then merge their records into one jsonl.

    Each shard gets its own worktree + cargo target dir + scratch tmp, because
    all three are shared mutable state: two shards checking out different
    commits into one worktree would race, and qemu-insn's output name is derived
    from a tag that repeats across specimens.

    The LVGL anchor is measured ONCE here, before the fan-out, and handed to
    every shard: it is built from the instrument's checkout so it cannot vary
    with the vyr ref, and its runner writes a fixed tmp/lvgl-m4.elf that
    concurrent builds would clobber.
    """
    n = a.workers
    scratch = Path(os.environ.get("VYR_REPLAY_SCRATCH", "/mnt/2tb/git_debris/vyr-perf-shards"))
    scratch.mkdir(parents=True, exist_ok=True)

    lvgl_cell = TMP / "perf-lvgl-cell.json"
    if a.lvgl and not lvgl_cell.exists():
        log("measuring the LVGL anchor ONCE for the whole replay "
            "(ref-independent: built from the instrument's checkout)")
        H = harness()
        cell = H.measure_lvgl(a.repeat, None)
        lvgl_cell.write_text(json.dumps(cell))
        log(f"  LVGL anchor: render_only={cell['metrics'].get('insns_per_frame_render_only')} "
            f"total={cell['metrics'].get('insns_per_frame_total')} "
            f"fold={cell['metrics'].get('harness_fold_insns')}")

    procs, outs = [], []
    for i in range(n):
        out = TMP / f"perf-replay-shard{i}.jsonl"
        out.unlink(missing_ok=True)
        outs.append(out)
        env = dict(os.environ)
        env["VYR_SPEC_WORKTREE"] = str(scratch / f"w{i}")
        env["VYR_SPEC_TARGET"] = str(scratch / f"t{i}")
        env["VYR_PERF_TMP"] = str(scratch / f"tmp{i}")
        env["VYR_LVGL_CELL"] = str(lvgl_cell)
        # Cap per-worker cargo parallelism. Cargo defaults to one job per
        # hardware thread, so N workers would ask for N x nproc threads and
        # thrash. max(2, nproc // N) keeps the total near nproc; qemu runs are
        # single-threaded and fill the gaps.
        env["CARGO_BUILD_JOBS"] = str(max(2, (os.cpu_count() or 8) // n))
        Path(env["VYR_PERF_TMP"]).mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, __file__, "--from", a.first, "--to", a.last,
               "--platforms", a.platforms, "--tiers", a.tiers,
               "--opt-levels", a.opt_levels, "--repeat", str(a.repeat),
               "--shard", f"{i}/{n}", "--out", str(out)]
        if a.lvgl:
            cmd.append("--lvgl")
        if a.stack:
            cmd.append("--stack")
        if a.no_host_bench:
            cmd.append("--no-host-bench")
        log(f"shard {i}/{n} -> {out}")
        procs.append(subprocess.Popen(cmd, cwd=REPO, env=env,
                                      stdout=open(TMP / f"perf-replay-shard{i}.log", "w"),
                                      stderr=subprocess.STDOUT))

    rcs = [p.wait() for p in procs]
    merged = []
    for out in outs:
        if out.exists():
            merged += [ln for ln in out.read_text().splitlines() if ln.strip()]
    # Sorted by the replay index each shard recorded, so the merged file is in
    # history order rather than completion order.
    merged.sort(key=lambda ln: json.loads(ln).get("replay", {}).get("index", 0))
    OUT.write_text("\n".join(merged) + "\n")
    log(f"merged {len(merged)} specimen record(s) from {n} shard(s) -> {OUT}")
    bad = [i for i, rc in enumerate(rcs) if rc != 0]
    if bad:
        log(f"ERROR: shard(s) {bad} exited non-zero — see tmp/perf-replay-shard*.log")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="first", default=FIRST_M4)
    ap.add_argument("--to", dest="last", default="HEAD")
    ap.add_argument("--platforms", default="host,qemu-m4")
    ap.add_argument("--tiers", default="exact,fast,draft")
    ap.add_argument("--opt-levels", default="z",
                    help="canonical replay is `z` (what release-mcu ships). The full "
                         "opt-level sweep is an on-demand single-ref mode of perf-harness.py "
                         "— same schema, so sweeps land in the same table.")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--no-host-bench", action="store_true")
    ap.add_argument("--lvgl", action="store_true",
                    help="also measure the LVGL anchor (current upstream) with the same "
                         "instrument — sensible only on the newest specimen")
    ap.add_argument("--stack", action="store_true",
                    help="also measure the stack high-water (#33 stack-probe)")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--workers", type=int, default=1,
                    help="shard the specimen list across N parallel workers, each with "
                         "its OWN git worktree, cargo target dir and scratch namespace. "
                         "Cargo already saturates cores WITHIN a build, but a replay's "
                         "wall time is dominated by many SMALL builds plus single-"
                         "threaded qemu runs, which leave most of the machine idle.")
    ap.add_argument("--shard", default=None,
                    help="internal: 'i/N' — run only groups where index %% N == i")
    ap.add_argument("--out", default=None, help="output jsonl (default tmp/perf-replay.jsonl)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    global OUT, LOG
    if a.out:
        OUT = Path(a.out)
        LOG = OUT.with_suffix(".log")

    H = harness()
    TMP.mkdir(parents=True, exist_ok=True)
    platforms = [p.strip() for p in a.platforms.split(",") if p.strip()]
    tiers = [t.strip() for t in a.tiers.split(",") if t.strip()]
    opts = [o.strip() for o in a.opt_levels.split(",") if o.strip()]

    raw = git("log", "--reverse", "--format=%h\x1f%aI\x1f%s", f"{a.first}^..{a.last}")
    commits = [tuple(ln.split("\x1f")) for ln in raw.splitlines() if ln]
    if not commits:
        log(f"ERROR: no commits in {a.first}^..{a.last}")
        return 1

    # group consecutive commits sharing a build key
    groups: list[dict] = []
    for sha, date, subj in commits:
        key = H.build_key_rev(sha)
        if groups and groups[-1]["build_key"] == key:
            groups[-1]["covers"].append({"commit": sha, "date": date, "subject": subj})
        else:
            groups.append({"build_key": key, "commit": sha, "date": date, "subject": subj,
                           "covers": []})
    log(f"{len(commits)} commit(s) {a.first}^..{a.last} → {len(groups)} distinct build "
        f"state(s); {len(commits) - len(groups)} commit(s) compile byte-identically to "
        f"their predecessor and are recorded as `covers`")
    if a.dry_run:
        for g in groups:
            log(f"  {g['commit']} {g['build_key']} covers={[c['commit'] for c in g['covers']]} "
                f"{g['subject'][:50]}")
        return 0

    if a.workers > 1 and not a.shard:
        return fan_out(a, groups)

    # The GLOBAL position is stamped before any sharding: a shard's own
    # enumerate() would number its groups 1..len(shard), and merging on that
    # only lands in history order by accident of a round-robin split.
    total_groups = len(groups)
    for k, g in enumerate(groups, 1):
        g["gindex"] = k
    if a.shard:
        i, n = (int(x) for x in a.shard.split("/"))
        groups = [g for k, g in enumerate(groups) if k % n == i]
        log(f"shard {i}/{n}: {len(groups)} specimen(s)")

    done: set[str] = set()
    if a.resume and OUT.exists():
        done = {json.loads(ln)["specimen"]["commit"]
                for ln in OUT.read_text().splitlines() if ln.strip()}
        log(f"resume: {len(done)} specimen(s) already recorded")
    elif OUT.exists():
        OUT.unlink()

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    t_start = time.monotonic()
    for i, g in enumerate(groups, 1):
        if g["commit"] in done:
            continue
        log(f"[{g['gindex']}/{total_groups}] {g['commit']} {g['subject'][:56]}")
        try:
            rec = H.run(g["commit"], platforms, tiers, opts, a.repeat, True, False,
                        not a.no_host_bench, cache, a.stack, a.lvgl)
            rec["replay"] = {"build_key": g["build_key"], "covers": g["covers"],
                            "index": g["gindex"], "of": total_groups}
        except Exception as e:  # a specimen that cannot even be checked out/probed
            log(f"    SKIPPED: {type(e).__name__}: {e}")
            rec = {"specimen": {"commit": g["commit"], "date": g["date"],
                                "subject": g["subject"]},
                   "status": "skipped",
                   "reason": f"{type(e).__name__}: {e}",
                   "replay": {"build_key": g["build_key"], "covers": g["covers"],
                              "index": g["gindex"], "of": total_groups}}
        with OUT.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        CACHE.write_text(json.dumps(cache))
        log(f"    elapsed {time.monotonic() - t_start:.0f}s total")

    log(f"done → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

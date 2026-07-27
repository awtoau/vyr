#!/usr/bin/env python3
"""microbench.py — the ~200-point painter micro-benchmark LANDSCAPE, in SQLite.

A single benchmark says vyr is behind; a matrix says WHERE. This builds the
parametric probe sweep (`vyr-size/src/probe.rs` `cases()` — primitive × size ×
alpha × radius, ×3 tiers), prices every point on the emulated Cortex-M4, and
writes one row per (tier, point) into a queryable SQLite database so you can:

    SELECT name, tier, insns, f64_share FROM points
    WHERE run_id = (SELECT max(run_id) FROM run)
    ORDER BY f64_share DESC;                       -- the determinism-tax leaders

Two speeds, because plugin QEMU is slow:

  * **Landscape** (default): one `libinsn` boot per tier prices every point
    exactly (bkpt-bracketed delta, the same rule as every published M4 number).
    Fills `insns` / `insns_per_px` / `pixels`. Fast — seconds per tier.
  * **Class split** (`--deep`): per point, an ISOLATED build
    (`VYR_PROBE_POINT=<name>`) run under hotblocks, reconstructed to the exact
    {int, mem, hardware-f32, soft-f64} instruction mix (`scripts/insn_static.py`,
    self-checking to 100.0000 %). The **soft-f64 share is the headline** — the
    determinism tax (#63): the part of a point's cost that exists only to round
    bit-identically to x86-64, that a hardware-f32 or fixed-point flattening
    would not pay. Boot is subtracted using a `null`-isolated run per tier.
    Slow — a build + hotblocks pass per point; `--deep` does a curated subset
    unless `--deep-full`.

The LVGL ratio column (`lvgl_insns`, `lvgl_ratio`) is wired but populated by a
later pass (`scripts/lvgl-m4-bench/`) only for the faithful-equivalent subset.

Store: SQLite at `--db` (default `tmp/microbench.db`, regenerable). The #25
JSONL ledger is untouched — folding this into it is a separate, deliberate
step (see docs/design/painter-simd-tax.md §6).

Output: the DB + tmp/microbench.log
Parallel: tiers and per-point isolated builds fan out across `--jobs` workers,
each with its OWN `CARGO_TARGET_DIR` (concurrent cargo builds cannot share one
— they lock it and emit the same ELF). Default jobs = min(12, cpu//2).

Usage:  python3 scripts/microbench.py [--tiers exact,fast,draft]
                                      [--deep [--deep-full]] [--jobs N]
                                      [--opt z|s|3] [--db tmp/microbench.db]
                                      [--keep-elf]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import contextlib
import multiprocessing
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import insn_static as S  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
WORK = TMP / "microbench"
LOG = TMP / "microbench.log"
QEMU_BUILD = Path(os.environ.get("VYR_QEMU_BUILD", "/mnt/2tb/git_debris/qemu-plugins-build"))
QEMU = QEMU_BUILD / "qemu-system-arm"
INSN_PLUGIN = QEMU_BUILD / "tests" / "tcg" / "plugins" / "libinsn.so"
HOTBLOCKS = QEMU_BUILD / "contrib" / "plugins" / "libhotblocks.so"
ELF = REPO / "target" / "thumbv7em-none-eabihf" / "release-mcu" / "vyr-size"
MACHINE = "netduinoplus2"
DEADLINE_S = 1200

FEATURES = {"exact": "run-qemu,probe", "fast": "run-qemu,probe,fast",
            "draft": "run-qemu,probe,draft"}

# The curated deep subset: the primitives at a mid and a large size, opaque and
# translucent — enough to see the f64 tax rise on curves without a full-matrix
# hotblocks sweep. `--deep-full` overrides and does every point.
DEEP_SUBSET = {
    "null", "rect48a255", "rect48a128", "rr48a255r8", "rr48a255r24",
    "disc48a255", "disc48a128", "disc192a255", "rect192a255", "w480", "b16", "b17",
}

_lines: list[str] = []
_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    # Worker threads call this; serialise so lines and the file don't interleave.
    with _log_lock:
        print(line, flush=True)
        _lines.append(line)
        WORK.mkdir(parents=True, exist_ok=True)
        LOG.write_text("\n".join(_lines) + "\n")


def git_commit() -> str:
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                       capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


# --- build + run -------------------------------------------------------------


class BuildError(RuntimeError):
    """A cargo build failed. Catchable so one task does not abort the pool."""


class Slots:
    """A pool of per-worker CARGO_TARGET_DIRs. Concurrent `cargo build`s MUST
    NOT share a target dir — cargo locks it (serialising the builds away) and
    they all emit the same `vyr-size` ELF (clobbering each other). Each worker
    acquires a private dir; builds within it stay incremental."""

    def __init__(self, n: int):
        self.q: queue.Queue[Path] = queue.Queue()
        for i in range(n):
            self.q.put(WORK / f"target-{i}")

    @contextlib.contextmanager
    def acquire(self):
        d = self.q.get()
        try:
            yield d
        finally:
            self.q.put(d)


def build(tier: str, opt: str | None, strip: bool, point: str | None, target_dir: Path) -> Path:
    cfg = ["--config", f"profile.release-mcu.strip={'true' if strip else 'false'}"]
    if opt:
        toml = f'"{opt}"' if opt in ("z", "s") else opt
        cfg += ["--config", f"profile.release-mcu.opt-level={toml}"]
    env = {**os.environ, "CARGO_INCREMENTAL": "0", "CARGO_TARGET_DIR": str(target_dir)}
    if point:
        env["VYR_PROBE_POINT"] = point
    cmd = ["cargo", "build", "--profile", "release-mcu", "-p", "vyr-size",
           "--target", "thumbv7em-none-eabihf", "--no-default-features",
           "--features", FEATURES[tier], *cfg]
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise BuildError(f"{tier}/{point or 'landscape'}: " + (r.stdout + r.stderr)[-1200:])
    src = target_dir / "thumbv7em-none-eabihf" / "release-mcu" / "vyr-size"
    tag = f"{tier}{'-O' + opt if opt else ''}{'-' + point if point else ''}{'' if strip else '-syms'}"
    dest = WORK / f"mb-{tag}.elf"
    dest.write_bytes(src.read_bytes())
    return dest


HEADER_RE = re.compile(r"probe \(#37\): (\d+) cases x (\d+) timed reps, "
                       r"(\d+)x(\d+) in \d+x(\d+) bands, quality=(\w+)")
CASE_RE = re.compile(
    r"case i=(\d+) name=(\S+) kind=(\S+) w=(\d+) count=(\d+) alpha=(\d+)"
    r"(?: radius=(\d+))? px=(\d+)")
# The perf build reports painted pixels per case AFTER the timed section. Used
# as the px denominator for insns/px — MEASURED, so it is right for the
# composite widgets (chart/text/…) whose analytic area is 0.
RESULT_RE = re.compile(r"result i=(\d+) name=\S+ pixels_written=(\d+)")


class GuestError(RuntimeError):
    """A tier's guest run failed (e.g. arena OOM). Catchable per-tier so one
    tier does not take the whole landscape down with it."""


def run_libinsn(elf: Path, tag: str) -> tuple[list[int], str]:
    plog = WORK / f"insn-{tag}.log"
    plog.unlink(missing_ok=True)
    args = [str(QEMU), "-machine", MACHINE, "-nographic",
            "-semihosting-config", "enable=on,target=native",
            "-icount", "shift=0,sleep=off",
            "-plugin", f"{INSN_PLUGIN},match=bkpt,trace=on",
            "-d", "plugin", "-D", str(plog), "-kernel", str(elf)]
    g = subprocess.run(args, capture_output=True, text=True, cwd=REPO, timeout=DEADLINE_S)
    gout = g.stdout + g.stderr
    if g.returncode != 0:
        # A panic ('memory allocation of N bytes failed') is the tell.
        reason = "arena OOM" if "allocation of" in gout else f"rc={g.returncode}"
        raise GuestError(f"{tag}: guest failed ({reason}): {gout[-400:]}")
    deltas = [int(d) for d in re.findall(r"Δ\+(\d+) since last match", plog.read_text())]
    return deltas, gout


def parse_cases(gout: str) -> tuple[dict, list[dict]]:
    h = HEADER_RE.search(gout)
    if not h:
        log("ERROR: no probe header — not a --features probe build?")
        raise SystemExit(1)
    pw = {int(m.group(1)): int(m.group(2)) for m in RESULT_RE.finditer(gout)}
    cases = []
    for m in CASE_RE.finditer(gout):
        i = int(m.group(1))
        cases.append({"i": i, "name": m.group(2), "kind": m.group(3),
                      "w": int(m.group(4)), "count": int(m.group(5)), "alpha": int(m.group(6)),
                      "radius": int(m.group(7)) if m.group(7) else 0,
                      "px": int(m.group(8)),        # analytic (rect/disc); 0 for widgets
                      "pw": pw.get(i)})             # measured WHOLE-frame painted px
    meta = {"n": int(h.group(1)), "reps": int(h.group(2)), "band_h": int(h.group(5)),
            "quality": h.group(6)}
    return meta, cases


def map_deltas(deltas: list[int], meta: dict, ncases: int) -> list[int] | None:
    """Per-case render insns (min over reps). Alignment is SEARCHED and
    shape-checked — the same rule painter-probe.py uses — so a slipped index
    fails loudly instead of reporting confident nonsense."""
    reps = meta["reps"]
    need = ncases * 2 * reps
    pre = 1 + ncases  # header + one case-table line each
    for start in range(pre, pre + 6):
        window = deltas[start:start + need]
        if len(window) != need:
            continue
        renders, ok = [], True
        for ci in range(ncases):
            base = ci * 2 * reps
            r = [window[base + 2 * k + 1] for k in range(reps)]
            gaps = [window[base + 2 * k] for k in range(reps)][1:]
            if gaps and max(gaps) > min(r) / 4:
                ok = False
                break
            renders.append(min(r))
        if ok:
            return renders
    return None


# --- SQLite ------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    run_id   INTEGER PRIMARY KEY,
    ts       TEXT, git_commit TEXT, opt TEXT, band_h INTEGER, machine TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS points (
    run_id INTEGER, tier TEXT, name TEXT, kind TEXT,
    w INTEGER, count INTEGER, alpha INTEGER, radius INTEGER, px INTEGER,
    insns INTEGER, insns_per_px REAL,
    soft_f64 INTEGER, hw_f32 INTEGER, mem INTEGER, total_deep INTEGER,
    f64_share REAL, f32_hw_share REAL, mem_share REAL,
    lvgl_insns INTEGER, lvgl_ratio REAL,
    PRIMARY KEY (run_id, tier, name)
);
"""


def open_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


# --- deep class split --------------------------------------------------------


def class_split(elf: Path, tag: str) -> dict:
    """One hotblocks pass over an ISOLATED build → its {soft_f64, hw_f32, mem}
    instruction totals (exact, self-checking)."""
    plog = WORK / f"hb-{tag}.log"
    plog.unlink(missing_ok=True)
    args = [str(QEMU), "-machine", MACHINE, "-nographic",
            "-semihosting-config", "enable=on,target=native", "-icount", "shift=0,sleep=off",
            "-plugin", f"{HOTBLOCKS},inline=true,limit=0",
            "-d", "plugin", "-D", str(plog), "-kernel", str(elf)]
    g = subprocess.run(args, capture_output=True, text=True, cwd=REPO, timeout=DEADLINE_S)
    if g.returncode != 0:
        log(f"  deep {tag}: guest rc={g.returncode}")
        return {}
    img = S.Image(elf)
    return S.float_weighting(img, S.blocks(plog))


# --- parallel tasks (run in worker threads; NO db access — main thread writes) ---


def landscape_task(tier: str, opt: str | None, slots: Slots, keep: bool) -> dict:
    with slots.acquire() as td:
        elf = build(tier, opt, strip=True, point=None, target_dir=td)
        try:
            deltas, gout = run_libinsn(elf, tier)
        finally:
            if not keep:
                elf.unlink(missing_ok=True)
    meta, cases = parse_cases(gout)
    renders = map_deltas(deltas, meta, len(cases))
    return {"tier": tier, "meta": meta, "cases": cases, "renders": renders}


def split_task(tier: str, point: str, opt: str | None, slots: Slots, keep: bool) -> dict:
    with slots.acquire() as td:
        elf = build(tier, opt, strip=False, point=point, target_dir=td)
        try:
            w = class_split(elf, f"{tier}-{point}")
        finally:
            if not keep:
                elf.unlink(missing_ok=True)
    return {"tier": tier, "point": point, "w": w}


def iso_landscape_task(tier: str, point: str, opt: str | None, slots: Slots, keep: bool) -> dict:
    """Price ONE point in its own boot (renders [null, point]). Used when a
    tier's single-boot landscape OOMs (Exact's 63,488 B pixmap can't survive
    the fragmentation of a 76-case run — the #46 wall); a fresh boot per point
    resets the heap. Returns the point's insns, its above-null delta, and the
    measured painted px for both, so the caller can price it identically."""
    with slots.acquire() as td:
        elf = build(tier, opt, strip=True, point=point, target_dir=td)
        try:
            deltas, gout = run_libinsn(elf, f"{tier}-iso-{point}")
        finally:
            if not keep:
                elf.unlink(missing_ok=True)
    meta, cases = parse_cases(gout)
    renders = map_deltas(deltas, meta, len(cases))
    if renders is None:
        return {"point": point, "ok": False}
    idx = {c["name"]: i for i, c in enumerate(cases)}
    if "null" not in idx or point not in idx:
        return {"point": point, "ok": False}
    ni, pi = idx["null"], idx[point]
    return {"point": point, "ok": True, "insns": renders[pi],
            "above": renders[pi] - renders[ni],
            "pw": cases[pi].get("pw"), "null_pw": cases[ni].get("pw")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="exact,fast,draft")
    ap.add_argument("--opt", default=None)
    ap.add_argument("--deep", action="store_true", help="fill the {int,mem,hw-f32,soft-f64} split")
    ap.add_argument("--deep-full", action="store_true", help="deep on EVERY point, not the subset")
    ap.add_argument("--jobs", type=int, default=0,
                    help="parallel workers (each gets its own CARGO_TARGET_DIR); "
                         "0 = auto (min(12, cpu//2))")
    ap.add_argument("--db", default=str(REPO / "docs" / "perf" / "microbench.db"))
    ap.add_argument("--note", default="")
    ap.add_argument("--keep-elf", action="store_true")
    a = ap.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    tiers = [t.strip() for t in a.tiers.split(",") if t.strip()]
    # Each worker holds a private target dir (~1 GB) and a cargo build that
    # itself uses several cores; cap so W builds do not thrash the box or disk.
    jobs = a.jobs or min(12, max(1, multiprocessing.cpu_count() // 2))
    slots = Slots(jobs)
    keep = a.keep_elf

    db = open_db(Path(a.db))
    cur = db.cursor()
    cur.execute("INSERT INTO run(ts, git_commit, opt, machine, note) VALUES (?,?,?,?,?)",
                (time.strftime("%Y-%m-%dT%H:%M:%S%z"), git_commit(), a.opt or "z",
                 MACHINE, a.note))
    run_id = cur.lastrowid
    log(f"run_id={run_id} commit={git_commit()} tiers={tiers} deep={a.deep} "
        f"jobs={jobs} db={a.db}")

    failed_tiers: list[str] = []
    ok_tiers: dict[str, list[dict]] = {}  # tier -> cases (for the deep phase)
    t0 = time.monotonic()

    # --- phase A: landscapes, one boot per tier, all tiers in parallel ---
    log(f"=== landscape: {len(tiers)} tiers in parallel ({jobs} workers) ===")
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(landscape_task, t, a.opt, slots, keep): t for t in tiers}
        for fut in cf.as_completed(futs):
            tier = futs[fut]
            try:
                r = fut.result()
            except (GuestError, BuildError) as e:
                log(f"  {tier}: SKIPPED — {e}")
                failed_tiers.append(tier)
                continue
            meta, cases, renders = r["meta"], r["cases"], r["renders"]
            if renders is None:
                log(f"  {tier}: delta stream did not align — skipping tier")
                failed_tiers.append(tier)
                continue
            cur.execute("UPDATE run SET band_h=? WHERE run_id=?", (meta["band_h"], run_id))
            names = [c["name"] for c in cases]
            null_insns = renders[names.index("null")]
            null_pw = cases[names.index("null")].get("pw")  # background-only px
            rows = []
            for ci, case in enumerate(cases):
                above = renders[ci] - null_insns
                # px: analytic for rect/disc; for the composite widgets the
                # painted-pixel delta over the background-only null frame.
                px = case["px"]
                if not px and case.get("pw") is not None and null_pw is not None:
                    px = max(0, case["pw"] - null_pw)
                ipp = above / px if px else None
                rows.append((run_id, tier, case["name"], case["kind"], case["w"], case["count"],
                             case["alpha"], case["radius"], px, renders[ci], ipp))
            cur.executemany(
                "INSERT OR REPLACE INTO points(run_id,tier,name,kind,w,count,alpha,radius,px,"
                "insns,insns_per_px) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            db.commit()
            ok_tiers[tier] = cases
            log(f"  {tier}: {len(rows)} points priced (null={null_insns:,} insns)")

    # --- phase A2: per-case isolated landscape for tiers whose full boot OOM'd ---
    if failed_tiers and ok_tiers:
        canon = next(iter(ok_tiers.values()))  # names/kinds/px are tier-independent
        canon_by_name = {c["name"]: c for c in canon}
        names_all = [c["name"] for c in canon if c["name"] != "null"]
        for tier in failed_tiers[:]:
            log(f"=== {tier}: full boot failed — per-case isolated landscape "
                f"({len(names_all)} boots, {jobs} workers) ===")
            got: dict[str, dict] = {}
            with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
                futs = {ex.submit(iso_landscape_task, tier, n, a.opt, slots, keep): n
                        for n in names_all}
                for fut in cf.as_completed(futs):
                    n = futs[fut]
                    try:
                        r = fut.result()
                    except (GuestError, BuildError) as e:
                        log(f"  {tier}/{n}: FAILED — {e}")
                        continue
                    if r.get("ok"):
                        got[n] = r
            rows = []
            for n in names_all:
                if n not in got or n not in canon_by_name:
                    continue
                c, g = canon_by_name[n], got[n]
                px = c["px"]
                if not px and g["pw"] is not None and g["null_pw"] is not None:
                    px = max(0, g["pw"] - g["null_pw"])
                ipp = g["above"] / px if px else None
                rows.append((run_id, tier, n, c["kind"], c["w"], c["count"],
                             c["alpha"], c["radius"], px, g["insns"], ipp))
            cur.executemany(
                "INSERT OR REPLACE INTO points(run_id,tier,name,kind,w,count,alpha,radius,px,"
                "insns,insns_per_px) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            db.commit()
            if rows:
                ok_tiers[tier] = canon  # now available to the deep phase too
                failed_tiers.remove(tier)
            log(f"  {tier}: {len(rows)} points priced (per-case isolated, #46 fallback)")

    # --- phase B: deep class split, every (tier, point) in parallel ---
    if a.deep and ok_tiers:
        # Boot floor per tier (the null-isolated split), computed in parallel.
        log("=== deep: boot floors (null-isolated, per tier) ===")
        floors: dict[str, dict] = {}
        with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = {ex.submit(split_task, t, "null", a.opt, slots, keep): t for t in ok_tiers}
            for fut in cf.as_completed(futs):
                try:
                    r = fut.result()
                except (GuestError, BuildError) as e:
                    log(f"  floor {futs[fut]}: FAILED — {e}")
                    continue
                floors[r["tier"]] = r["w"]
        # The points: subset unless --deep-full.
        tasks = []
        for tier, cases in ok_tiers.items():
            if tier not in floors:
                continue
            want = {c["name"] for c in cases} if a.deep_full else DEEP_SUBSET
            tasks += [(tier, c["name"]) for c in cases if c["name"] in want and c["name"] != "null"]
        log(f"=== deep: {len(tasks)} (tier,point) class splits in parallel ({jobs} workers) ===")
        done = 0
        with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = {ex.submit(split_task, t, p, a.opt, slots, keep): (t, p) for t, p in tasks}
            for fut in cf.as_completed(futs):
                tier, point = futs[fut]
                try:
                    w = fut.result()["w"]
                except (GuestError, BuildError) as e:
                    log(f"  deep {tier}/{point}: FAILED — {e}")
                    continue
                floor = floors[tier]
                boot = floor.get("total", 0)
                if not w or w.get("total", 0) <= boot:
                    log(f"  deep {tier}/{point}: no usable split")
                    continue
                tot = w["total"] - boot
                f64 = max(0, w["soft_f64_insns"] - floor.get("soft_f64_insns", 0))
                hw = max(0, w["hw_f32_insns"] - floor.get("hw_f32_insns", 0))
                mem = max(0, w["mem_insns"] - floor.get("mem_insns", 0))
                cur.execute(
                    "UPDATE points SET soft_f64=?,hw_f32=?,mem=?,total_deep=?,"
                    "f64_share=?,f32_hw_share=?,mem_share=? WHERE run_id=? AND tier=? AND name=?",
                    (f64, hw, mem, tot, f64 / tot, hw / tot, mem / tot, run_id, tier, point))
                db.commit()
                done += 1
                log(f"  {tier}/{point}: f64 {100 * f64 / tot:5.1f}%  hw-f32 {100 * hw / tot:5.1f}%  "
                    f"mem {100 * mem / tot:5.1f}%  ({tot:,} render insns)")
        log(f"deep: {done}/{len(tasks)} points split")

    log(f"wall: {time.monotonic() - t0:.0f}s")
    # Regenerate the viewer page (best-effort — a render failure must not lose
    # the measured DB). docs/perf/microbench.html, grid + graphs like index.html.
    try:
        r = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "microbench-html.py"), "--db", a.db],
            cwd=REPO, capture_output=True, text=True)
        log(r.stdout.strip() or f"html: rc={r.returncode} {r.stderr[-200:]}")
    except Exception as e:  # noqa: BLE001 — never fatal
        log(f"html regen skipped: {e}")
    # A short landscape summary from the DB itself.
    log("=== landscape (top per-px, latest run) ===")
    for tier in tiers:
        top = cur.execute(
            "SELECT name, insns, insns_per_px, f64_share FROM points "
            "WHERE run_id=? AND tier=? AND name!='null' "
            "ORDER BY insns_per_px DESC LIMIT 8", (run_id, tier)).fetchall()
        log(f"  {tier}: most expensive per px")
        for name, insns, ipp, f64 in top:
            fs = f"f64 {100 * f64:.0f}%" if f64 is not None else "f64 —"
            log(f"      {name:>14} {ipp or 0:8.1f} insn/px  {insns:>12,} insns  {fs}")
    db.close()
    if failed_tiers:
        log(f"NOTE: tiers skipped (guest failure): {', '.join(failed_tiers)}")
    log(f"wrote {a.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
Usage:  python3 scripts/microbench.py [--tiers exact,fast,draft]
                                      [--deep [--deep-full]] [--opt z|s|3]
                                      [--db tmp/microbench.db] [--keep-elf]
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
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


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line, flush=True)
    _lines.append(line)
    WORK.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(_lines) + "\n")


def git_commit() -> str:
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                       capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


# --- build + run -------------------------------------------------------------


def build(tier: str, opt: str | None, strip: bool, point: str | None) -> Path:
    cfg = ["--config", f"profile.release-mcu.strip={'true' if strip else 'false'}"]
    if opt:
        toml = f'"{opt}"' if opt in ("z", "s") else opt
        cfg += ["--config", f"profile.release-mcu.opt-level={toml}"]
    env = {**os.environ, "CARGO_INCREMENTAL": "0"}
    if point:
        env["VYR_PROBE_POINT"] = point
    cmd = ["cargo", "build", "--profile", "release-mcu", "-p", "vyr-size",
           "--target", "thumbv7em-none-eabihf", "--no-default-features",
           "--features", FEATURES[tier], *cfg]
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        log("BUILD FAILED: " + " ".join(cmd) + "\n" + (r.stdout + r.stderr)[-3000:])
        raise SystemExit(1)
    tag = f"{tier}{'-O' + opt if opt else ''}{'-' + point if point else ''}{'' if strip else '-syms'}"
    dest = WORK / f"mb-{tag}.elf"
    dest.write_bytes(ELF.read_bytes())
    return dest


HEADER_RE = re.compile(r"probe \(#37\): (\d+) cases x (\d+) timed reps, "
                       r"(\d+)x(\d+) in \d+x(\d+) bands, quality=(\w+)")
CASE_RE = re.compile(
    r"case i=(\d+) name=(\S+) kind=(\S+) w=(\d+) count=(\d+) alpha=(\d+)"
    r"(?: radius=(\d+))? px=(\d+)")


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
    cases = []
    for m in CASE_RE.finditer(gout):
        cases.append({"i": int(m.group(1)), "name": m.group(2), "kind": m.group(3),
                      "w": int(m.group(4)), "count": int(m.group(5)), "alpha": int(m.group(6)),
                      "radius": int(m.group(7)) if m.group(7) else 0, "px": int(m.group(8))})
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="exact,fast,draft")
    ap.add_argument("--opt", default=None)
    ap.add_argument("--deep", action="store_true", help="fill the {int,mem,hw-f32,soft-f64} split")
    ap.add_argument("--deep-full", action="store_true", help="deep on EVERY point, not the subset")
    ap.add_argument("--db", default=str(TMP / "microbench.db"))
    ap.add_argument("--note", default="")
    ap.add_argument("--keep-elf", action="store_true")
    a = ap.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    tiers = [t.strip() for t in a.tiers.split(",") if t.strip()]

    db = open_db(Path(a.db))
    cur = db.cursor()
    cur.execute("INSERT INTO run(ts, git_commit, opt, machine, note) VALUES (?,?,?,?,?)",
                (time.strftime("%Y-%m-%dT%H:%M:%S%z"), git_commit(), a.opt or "z",
                 MACHINE, a.note))
    run_id = cur.lastrowid
    log(f"run_id={run_id} commit={git_commit()} tiers={tiers} deep={a.deep} db={a.db}")

    failed_tiers = []
    for tier in tiers:
        log(f"=== {tier}: landscape (libinsn, all points, one boot) ===")
        elf = build(tier, a.opt, strip=True, point=None)
        try:
            deltas, gout = run_libinsn(elf, tier)
        except GuestError as e:
            log(f"  {tier}: SKIPPED — {e}")
            failed_tiers.append(tier)
            if not a.keep_elf:
                elf.unlink(missing_ok=True)
            continue
        meta, cases = parse_cases(gout)
        cur.execute("UPDATE run SET band_h=? WHERE run_id=?", (meta["band_h"], run_id))
        renders = map_deltas(deltas, meta, len(cases))
        if renders is None:
            log(f"  {tier}: delta stream did not align — skipping tier")
            continue
        null_insns = renders[[c["name"] for c in cases].index("null")]
        rows = []
        for ci, case in enumerate(cases):
            above = renders[ci] - null_insns
            ipp = above / case["px"] if case["px"] else None
            rows.append((run_id, tier, case["name"], case["kind"], case["w"], case["count"],
                         case["alpha"], case["radius"], case["px"], renders[ci], ipp))
        cur.executemany(
            "INSERT OR REPLACE INTO points(run_id,tier,name,kind,w,count,alpha,radius,px,"
            "insns,insns_per_px) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.commit()
        log(f"  {tier}: {len(rows)} points priced (null={null_insns:,} insns)")
        if not a.keep_elf:
            elf.unlink(missing_ok=True)

        if a.deep:
            want = {c["name"] for c in cases} if a.deep_full else DEEP_SUBSET
            names = [c["name"] for c in cases if c["name"] in want]
            log(f"=== {tier}: deep class split on {len(names)} points (isolated hotblocks) ===")
            # boot+floor once per tier, subtracted so the split is the RENDER's.
            nz = build(tier, a.opt, strip=False, point="null")
            floor = class_split(nz, f"{tier}-null")
            if not a.keep_elf:
                nz.unlink(missing_ok=True)
            boot = floor.get("total", 0)
            boot_f64 = floor.get("soft_f64_insns", 0)
            boot_hw = floor.get("hw_f32_insns", 0)
            boot_mem = floor.get("mem_insns", 0)
            for name in names:
                if name == "null":
                    continue
                pelf = build(tier, a.opt, strip=False, point=name)
                w = class_split(pelf, f"{tier}-{name}")
                if not a.keep_elf:
                    pelf.unlink(missing_ok=True)
                if not w or w.get("total", 0) <= boot:
                    log(f"  deep {tier}/{name}: no usable split")
                    continue
                # subtract the boot floor; the remainder is the render itself.
                tot = w["total"] - boot
                f64 = max(0, w["soft_f64_insns"] - boot_f64)
                hw = max(0, w["hw_f32_insns"] - boot_hw)
                mem = max(0, w["mem_insns"] - boot_mem)
                cur.execute(
                    "UPDATE points SET soft_f64=?,hw_f32=?,mem=?,total_deep=?,"
                    "f64_share=?,f32_hw_share=?,mem_share=? WHERE run_id=? AND tier=? AND name=?",
                    (f64, hw, mem, tot, f64 / tot, hw / tot, mem / tot, run_id, tier, name))
                db.commit()
                log(f"  {tier}/{name}: f64 {100 * f64 / tot:5.1f}%  hw-f32 {100 * hw / tot:5.1f}%  "
                    f"mem {100 * mem / tot:5.1f}%  ({tot:,} render insns)")

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

#!/usr/bin/env python3
"""ledger — THE vyr measurement ledger: one writer, one file, one page (#25).

Replaces the two parallel ledgers this repo used to keep (``docs/perf`` written
by ``perf-history.py`` and ``docs/metrics`` written by ``metrics-history.py``).
There is now exactly ONE canonical ledger:

    docs/perf/history.jsonl      append-only, committed, one row per run
    docs/perf/index.html         regenerated from it (+ SVG charts beside it)

Canonical entry point: ``./dev.py track``. Output timestamped → tmp/ledger.log.

ROW SCHEMA (``"schema": 3``) — a flat identity envelope plus independent,
OPTIONAL measurement sections. A row that lacks a section simply did not measure
it; sparse rows are honest, invented values are not. Nothing is ever
back-filled.

    ts, commit, dirty, host, cpu, arch      identity (dirty = unclean worktree)
    matrix    {cells[…], platforms_*}       THE performance matrix — every M4
                                            instruction figure, from
                                            scripts/perf-harness.py ONLY
    ladder    {measured_at, rungs[6]}       host resolution ladder 120→4K
    anim      {…, run_hash}                 host anim acceptance run
    arm       {…, cross_isa}                qemu-user cross-ISA verdict
    bench     {source, ns_px{…}}            committed vyr-bench medians
    size      {target, profile, flash_kib}  linked M4 ELF sizes
    m4_qemu   {machine, tiers{…}}           stock-qemu heap/hash/coverage ONLY
    silicon   {board, clock, tiers{…}}      real F429 cycles (DWT_CYCCNT)
    board_anim{…}                           dirty-rect animation cost model
    derived   {…}                           projections computed from the above

WHAT CHANGED IN SCHEMA 3, AND WHY (2026-07-24)

    · ``insns`` is GONE, replaced by ``matrix``. The old section recorded one
      number per firmware — a *total* that silently included the benchmark's own
      FNV hash fold (error 4 in docs/measurements/perf-history.md: 36.2 % of
      Draft's frame). The matrix records ``total`` / ``harness_fold_insns`` /
      ``render_only`` as three separate mandatory fields per cell, so the
      benchmark can never again measure itself into a headline.
    · ``opt_level`` is a first-class key on every cell, beside platform and
      tier. It is a dimension to be measured, not a decision to bake in (#33).
      The fold's own cost moves with it (3.11 M at ``z``, 2.24 M at ``2``), so a
      render-only figure is only valid for the cell it was measured in.
    · every cell carries ``fold_provenance`` and ``build_type``: HOW a
      render-only number was obtained is part of the number (#44/#45).
    · every cell carries ``word_bits`` and ``float`` — properties of the
      platform, not free axes. x86-64 has hardware f64 and the M4F does not,
      which is precisely why the soft-f64 trig bill (#32) hid for months in
      host-only measurement.
    · ``superseded`` is GONE, and with it every SYS_CLOCK-derived "insns/frame".
      That number was host wall time (error 1). Per this repo's
      derived-artifacts rule there is no migration path and no compatibility
      fallback: a fictional number is worse than a missing one, so it is
      deleted rather than carried with a warning label.
    · rows whose M4 figures came from any earlier tool were REBUILT by
      ``scripts/perf-replay.py`` — one instrument, replayed over history.
      Genuinely-measured host data (ladder, anim, arm, bench, size) is
      preserved untouched and labelled ``migrated_from``.

Every section carries its own provenance (tool + version, ELF SHA-256, upstream
commit for the LVGL anchor) because the sections are measured by different
tools, on different hardware, at different times. ``docs/performance.md`` §6
lists the command that produces each input.

Sources — all OPTIONAL, all ingested from artifacts already on disk (this
command MEASURES NOTHING itself, so it never double-runs a build or the board):

    tmp/rig-ladder.json         ./dev.py ladder
    tmp/rig-anim-stats.json     ./dev.py anim
    tmp/rig-arm.json            ./dev.py anim --arm
    vyr-bench/baseline.json     ./dev.py bench-record  (committed, blessed)
    tmp/size-mcu.json           ./dev.py size-mcu
    tmp/qemu-m4-{draft,exact}.json  ./dev.py qemu-m4 [--draft]
    tmp/perf-harness-HEAD.json  scripts/perf-harness.py   (THE matrix)
    tmp/lvgl-m4-result.json     scripts/lvgl-m4-bench/run.py (LVGL provenance)
    tmp/board-result.json       scripts/board-run.py     (real silicon)
    tmp/board-anim.json         scripts/board-anim.py    (SPI dirty-rect model)

Usage: scripts/ledger.py [--regen-only]
       scripts/ledger.py --rebuild-from-replay tmp/perf-replay.jsonl
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
LEDGER_DIR = REPO / "docs" / "perf"
HISTORY = LEDGER_DIR / "history.jsonl"
SCHEMA = 3

SCENE_PX = 480 * 270  # the 480x270 reference scene every M4 number is taken on

# The three flat scalars the retired docs/metrics ledger carried. They are
# strictly DERIVED from rungs[] — kept as a projection because they were the
# only quantity both retired harnesses recorded, i.e. the only bridge a
# continuous series can be drawn across.
BRIDGE = {
    "ladder480_full_ns_px": (480, "full_ns_px"),
    "ladder480_incr_ns_px": (480, "incr_ns_dirty_px"),
    "ladder4k_full_ns_px": (3840, "full_ns_px"),
}

# Categorical palette (validated: adjacent-pair CVD ΔE ≥ 8, normal-vision ≥ 15,
# both modes, against these surfaces). Fixed slot order, never cycled.
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"]


def log(msg: str) -> None:
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1e6):06d} UTC"
    line = f"{stamp}  INFO  [ledger] {msg}"
    print(line, file=sys.stderr)
    TMP.mkdir(exist_ok=True)
    with open(TMP / "ledger.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


# --- identity ----------------------------------------------------------------


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], capture_output=True, text=True, cwd=REPO, check=False)
    return out.stdout.strip()


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _mtime_iso(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(p: Path):
    """Read a JSON artifact, or None (with a note) when it is absent."""
    if not p.exists():
        log(f"absent: {p.relative_to(REPO)} — row carries no section from it")
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log(f"ERROR: {p.relative_to(REPO)} unreadable ({e}) — section skipped")
        return None


# --- section collectors ------------------------------------------------------


def sec_ladder() -> dict | None:
    p = TMP / "rig-ladder.json"
    d = _read(p)
    if not d or not d.get("rungs"):
        return None
    return {"measured_at": _mtime_iso(p), "rungs": d["rungs"]}


def sec_passthrough(name: str) -> dict | None:
    p = TMP / name
    d = _read(p)
    if not d:
        return None
    d = dict(d)
    d.setdefault("measured_at", _mtime_iso(p))
    return d


def sec_bench() -> dict | None:
    p = REPO / "vyr-bench" / "baseline.json"
    d = _read(p)
    if not d:
        return None
    keys = {
        "prim/fill_rrect_64": "fill_rrect",
        "prim/disc_r24": "disc",
        "prim/gradient_64": "gradient",
        "prim/line_diag_w3": "line",
        "scene/ir_full_exact": "scene_exact",
        "scene/ir_full_draft": "scene_draft",
    }
    ns = {v: round(d[k], 4) for k, v in keys.items() if k in d}
    if not ns:
        return None
    return {
        "source": "vyr-bench/baseline.json (committed, blessed medians)",
        "measured_at": _mtime_iso(p),
        "ns_px": ns,
    }


def sec_size() -> dict | None:
    p = TMP / "size-mcu.json"
    d = _read(p)
    if not d or not d.get("flash_kib"):
        return None
    d = dict(d)
    d.setdefault("measured_at", _mtime_iso(p))
    return d


def sec_m4_qemu() -> dict | None:
    tiers = {}
    newest = None
    for tier in ("draft", "exact"):
        p = TMP / f"qemu-m4-{tier}.json"
        d = _read(p)
        if not d:
            continue
        newest = max(newest or "", _mtime_iso(p))
        tiers[tier] = {
            k: d[k]
            for k in ("frame_hash", "heap_peak_b", "heap_live_end_b",
                      "fastpath_coverage_pct", "sys_clock_cs", "cross_isa")
            if d.get(k) is not None
        }
    if not tiers:
        return None
    return {
        "machine": "netduinoplus2 (STM32F405/M4F)",
        "measured_at": newest,
        "tool": "stock qemu-system-arm, -icount shift=0,sleep=off, no TCG plugin",
        "note": (
            "Deterministic quantities ONLY: frame hash, heap peak, Draft fast-path "
            "coverage. This vehicle's SYS_CLOCK reading is host wall time, not "
            "instructions, so schema 3 does not record it at all. Instruction counts "
            "come from the `matrix` section and nowhere else."
        ),
        "tiers": tiers,
    }


# --- the matrix (schema 3): the ONLY source of an M4 instruction figure ------

CELL_KEEP = ("platform", "firmware", "tier", "opt_level", "target", "profile", "isa",
             "word_bits", "float", "build_type", "fold_provenance", "status", "reason",
             "metrics", "metric_notes")


def strip_cell(cell: dict) -> dict:
    """A ledger cell: every metric and every reason, but not the raw guest
    console (that stays in tmp/, it is megabytes and it is not evidence anyone
    reads from a JSONL)."""
    out = {k: cell[k] for k in CELL_KEEP if k in cell}
    p = cell.get("provenance", {})
    keep_prov = {}
    for k in ("fold_share_of_total", "fastpath_coverage_pct", "instrument"):
        if p.get(k) is not None:
            keep_prov[k] = p[k]
    for name in ("total", "render_only"):
        d = p.get(name)
        if isinstance(d, dict):
            keep_prov[name] = {k: d.get(k) for k in
                               ("elf_sha256", "deterministic", "repeat", "timed_frames",
                                "timed_window_insns", "window_remainder", "cache_hit")}
    if p.get("raw"):
        keep_prov["board_raw"] = p["raw"]
    if keep_prov:
        out["provenance"] = keep_prov
    return out


def matrix_section(rec: dict) -> dict:
    """Turn one scripts/perf-harness.py record into the row's `matrix`."""
    return {
        "what": "the performance matrix — platform x tier x opt-level, one instrument",
        "harness": rec["harness"],
        "scene": rec.get("scene"),
        "axes": rec.get("axes"),
        "capabilities": {k: v for k, v in (rec.get("capabilities") or {}).items()
                         if k in ("tiers", "fold_patchable", "stack_probe",
                                  "timed_frames_declared", "vyr_size_features")},
        "platforms_attempted": rec.get("platforms_attempted"),
        "platforms_available": rec.get("platforms_available"),
        "platforms_unavailable": rec.get("platforms_unavailable"),
        "cells": [strip_cell(c) for c in rec.get("cells", [])],
    }


def sec_matrix() -> dict | None:
    p = TMP / "perf-harness-HEAD.json"
    d = _read(p)
    if not d or not d.get("cells"):
        return None
    return matrix_section(d)


def sec_silicon() -> dict | None:
    p = TMP / "board-result.json"
    d = _read(p)
    if not d or not d.get("tiers"):
        return None
    tiers = {}
    for tier, t in d["tiers"].items():
        cyc = t.get("cycles_per_frame") or {}
        tiers[tier.lower()] = {
            "cycles_per_frame": cyc.get("median"),
            "spread_ppm": cyc.get("spread_ppm"),
            "runs": t.get("n_ok"),
            "ms_per_frame": t.get("ms_per_frame"),
            "heap_peak_b": t.get("heap_peak"),
            "frame_hash": t.get("reference_hash") or (t.get("frame_hashes") or [None])[0],
            "timed_frames": t.get("timed_frames"),
            "elf_sha256": (t.get("runs") or [{}])[0].get("elf_sha256"),
        }
    clock = next(iter(d["tiers"].values())).get("clock", {})
    return {
        "board": d.get("board"),
        "chip": d.get("chip"),
        "probe": d.get("probe"),
        "timer": d.get("timer", "DWT_CYCCNT (real CPU cycles)"),
        "measured_at": d.get("when") or _mtime_iso(p),
        "clock": clock,
        "tiers": tiers,
    }


def sec_board_anim() -> dict | None:
    p = TMP / "board-anim.json"
    d = _read(p)
    if not d:
        return None
    return {
        "what": "dirty-rect animation cost model over SPI (render + flush per scene)",
        "measured_at": d.get("when") or _mtime_iso(p),
        "model": d,
    }


def m4_cell(row: dict, tier: str, opt: str = "z") -> dict | None:
    """The canonical qemu-M4 cell for a tier — the one and only place a chart or
    a table may take an instruction count from."""
    for c in (row.get("matrix") or {}).get("cells", []):
        if (c.get("platform") == "qemu-m4" and c.get("tier") == tier
                and c.get("opt_level") == opt and c.get("status") == "measured"):
            return c
    return None


def derived(row: dict) -> dict:
    """Projections computed from the sections present — never a new measurement."""
    out: dict = {}
    for key, (width, field) in BRIDGE.items():
        for rung in (row.get("ladder") or {}).get("rungs", []):
            if rung.get("w") == width and rung.get(field) is not None:
                out[key] = round(rung[field], 4)
                break
    sil = (row.get("silicon") or {}).get("tiers", {})
    cpi, ratio, foldshare = {}, {}, {}
    for tier in ("exact", "fast", "draft"):
        c = m4_cell(row, tier)
        if not c:
            continue
        m = c["metrics"]
        ro, tot, fold = (m.get("insns_per_frame_render_only"),
                         m.get("insns_per_frame_total"), m.get("harness_fold_insns"))
        if ro:
            ratio[tier] = round(ro / SCENE_PX, 2)
        if tot and fold is not None:
            foldshare[tier] = round(fold / tot, 4)
        cyc = sil.get(tier, {}).get("cycles_per_frame")
        # cycles/insn is measured against the TOTAL, because the silicon run
        # executes the fold too — comparing silicon cycles to render-only
        # instructions would be comparing two different workloads.
        if cyc and tot:
            cpi[tier] = round(cyc / tot, 3)
    if cpi:
        out["cycles_per_insn"] = cpi
    if ratio:
        out["render_only_insn_px"] = ratio
    if foldshare:
        out["harness_fold_share"] = foldshare
    return out


def build_row() -> dict:
    row: dict = {
        "schema": SCHEMA,
        "kind": "measurement",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "commit": _git("rev-parse", "--short", "HEAD") or "unknown",
        "commit_date": _git("log", "-1", "--format=%aI"),
        "subject": _git("log", "-1", "--format=%s"),
        "dirty": bool(_git("status", "--porcelain")),
        "host": platform.node(),
        "cpu": _cpu_model(),
        "arch": platform.machine(),
    }
    for key, fn in (
        ("matrix", sec_matrix),
        ("ladder", sec_ladder),
        ("anim", lambda: sec_passthrough("rig-anim-stats.json")),
        ("arm", lambda: sec_passthrough("rig-arm.json")),
        ("bench", sec_bench),
        ("size", sec_size),
        ("m4_qemu", sec_m4_qemu),
        ("silicon", sec_silicon),
        ("board_anim", sec_board_anim),
    ):
        v = fn()
        if v is not None:
            row[key] = v
    d = derived(row)
    if d:
        row["derived"] = d
    return row


def append_row() -> dict | None:
    row = build_row()
    sections = [k for k in row if k not in ("schema", "ts", "commit", "dirty", "host", "cpu", "arch")]
    if not sections:
        log("ERROR: nothing to record — no measurement artifact found in ./tmp "
            "(run ./dev.py ladder / anim / size-mcu / qemu-m4, or scripts/qemu-insn.py)")
        return None
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")
    log(f"appended row: commit {row['commit']}{'+dirty' if row['dirty'] else ''}, "
        f"sections [{', '.join(sections)}] → {HISTORY.relative_to(REPO)}")
    return row


def load_all(path: Path = HISTORY) -> list[dict]:
    if not path.exists():
        return []
    rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    bad = [r.get("commit", r.get("kind")) for r in rows if r.get("schema") != SCHEMA]
    if bad:
        raise SystemExit(
            f"ledger: rows {bad} are not schema {SCHEMA}. The ledger has ONE format; "
            "there is no read-old-format path. Rewrite the file "
            "(scripts/ledger.py --rebuild-from-replay) or start a new one."
        )
    return rows


def load_history() -> list[dict]:
    """Charted rows only: the schema note and the recorded SKIPS are carried in
    the file but are not measurements."""
    return [r for r in load_all() if r.get("kind", "measurement") == "measurement"]


# --- rebuild: one instrument, replayed over history --------------------------

SCHEMA_NOTE = {
    "schema": SCHEMA,
    "kind": "schema-note",
    "written": None,  # filled at write time
    "what_changed": [
        "schema 2 -> 3: the `insns` section is replaced by `matrix`, whose cells are "
        "keyed by platform x tier x opt-level and record insns_per_frame_total, "
        "harness_fold_insns and insns_per_frame_render_only as three separate "
        "mandatory fields.",
        "every M4 instruction figure in this file was produced by scripts/perf-harness.py "
        "replayed over history by scripts/perf-replay.py — old commits measured with "
        "TODAY's instrument, never with their own contemporaneous tooling.",
        "every cell records HOW its render-only figure was obtained — `fold_provenance` "
        "is one of `absent-from-window-by-build` (#44: the timed pass contains no fold, "
        "so total IS render-only), `measured-differential` (this firmware rebuilt with "
        "the fold folding an empty slice, differenced IN THIS CELL) or "
        "`derived-by-subtraction` (a fold figure carried in from elsewhere — never "
        "produced by this harness). A derived value must never look like a measured one.",
        "every cell records its `build_type`, because a perf build and a verifying build "
        "are now different binaries.",
        "a build whose frame hash does not match the host leg's has its timing "
        "SUPPRESSED, not annotated — a wrong-pixel build reports nothing.",
        "the SYS_CLOCK-derived `superseded` block is DELETED, not relabelled.",
        "host-measured sections (ladder, anim, arm, bench, size) from schema-2 rows are "
        "preserved verbatim and marked `migrated_from`.",
    ],
    "why": (
        "docs/measurements/perf-history.md records four measurement errors, every one of "
        "which flattered vyr. Errors 1 and 2 made the early M4 numbers fiction (host wall "
        "time; wrong build profile) and error 4 showed that the published totals were "
        "36-55 % the benchmark hashing its own output. A ledger that mixes those eras "
        "cannot be compared to itself, and per this repo's derived-artifacts rule a "
        "regenerable artifact has no legacy: the rebuild IS the migration."
    ),
    "instrument": {
        "harness": "scripts/perf-harness.py",
        "replay": "scripts/perf-replay.py",
        "counter": "plugin QEMU (--enable-plugins --enable-capstone) + tests/tcg/plugins/libinsn.so",
        "profile": "release-mcu (opt-level z canonical; other levels are a matrix axis, #33)",
    },
}


def _commit_order(commit: str) -> int:
    n = _git("rev-list", "--count", commit)
    return int(n) if n.isdigit() else 0


def migrate_schema2_row(r: dict) -> dict:
    """Keep what was genuinely measured on the host; delete every M4 instruction
    figure and the wall-clock fiction outright."""
    out = {"schema": SCHEMA, "kind": "measurement"}
    for k, v in r.items():
        if k in ("schema", "insns", "superseded"):
            continue
        out[k] = v
    mq = out.get("m4_qemu")
    if mq:
        for t in mq.get("tiers", {}).values():
            t.pop("sys_clock_cs", None)
        mq["note"] = (
            "Deterministic quantities ONLY: frame hash, heap peak, Draft fast-path "
            "coverage. The SYS_CLOCK reading this vehicle prints is host wall time and "
            "is not recorded in schema 3 at all."
        )
    out["migrated_from"] = r.get("migrated_from", "docs/perf/history.jsonl (schema 2)")
    out["migration_note"] = (
        "schema-2 row. Host-measured sections preserved verbatim; the `insns` section "
        "and the SYS_CLOCK-derived `superseded` block were deleted — every M4 "
        "instruction figure in this ledger now comes from scripts/perf-harness.py."
    )
    out["derived"] = derived(out) or {}
    return out


def replay_row(rec: dict) -> dict:
    sp = rec["specimen"]
    h = rec["harness"]
    rp = rec.get("replay", {})
    row = {
        "schema": SCHEMA,
        "kind": "measurement",
        "ts": h["timestamp"],
        "commit": sp["commit"],
        "commit_full": sp.get("commit_full"),
        "commit_date": sp.get("date"),
        "subject": sp.get("subject"),
        "dirty": sp.get("dirty", False),
        "host": h["host"],
        "cpu": h["cpu"],
        "arch": h["arch"],
        "source": f"{h['name']} {h['version']} via scripts/perf-replay.py",
        "build_key": rp.get("build_key"),
        "covers": [c["commit"] for c in rp.get("covers", [])],
        "matrix": matrix_section(rec),
    }
    row["derived"] = derived(row)
    return row


def rebuild_from_replay(replay_path: Path) -> list[dict]:
    if not replay_path.exists():
        raise SystemExit(f"ledger: no replay file at {replay_path}")
    # Deliberately NOT load_all(): the rebuild is the one path allowed to read a
    # previous schema, precisely so it can delete what that schema got wrong.
    old = [json.loads(ln) for ln in HISTORY.read_text().splitlines() if ln.strip()] \
        if HISTORY.exists() else []
    kept = []
    for r in old:
        if r.get("kind") in ("schema-note", "skip"):
            continue
        if r.get("schema") == SCHEMA and r.get("matrix"):
            continue  # a matrix row is a replay product; the replay is authoritative
        kept.append(migrate_schema2_row(r) if r.get("schema") != SCHEMA else r)
    log(f"preserved {len(kept)} host-measured row(s) from the previous ledger")

    rows, skips = [], []
    for ln in replay_path.read_text().splitlines():
        if not ln.strip():
            continue
        rec = json.loads(ln)
        if rec.get("status") == "skipped" or "harness" not in rec:
            skips.append({
                "schema": SCHEMA, "kind": "skip",
                "commit": rec.get("specimen", {}).get("commit"),
                "commit_date": rec.get("specimen", {}).get("date"),
                "subject": rec.get("specimen", {}).get("subject"),
                "reason": rec.get("reason", "unknown"),
                "note": "recorded, not omitted: a commit that could not be measured is "
                        "part of the history and an honest hole beats an interpolation.",
            })
            continue
        rows.append(replay_row(rec))
    log(f"{len(rows)} replayed matrix row(s), {len(skips)} recorded skip(s)")

    allrows = kept + rows
    allrows.sort(key=lambda r: (_commit_order(r.get("commit_full") or r["commit"]),
                                r.get("ts", "")))
    note = dict(SCHEMA_NOTE)
    note["written"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    note["rows"] = {"preserved_host_rows": len(kept), "replayed_matrix_rows": len(rows),
                    "recorded_skips": len(skips)}
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY, "w", encoding="utf-8") as f:
        f.write(json.dumps(note, separators=(",", ":")) + "\n")
        for r in allrows + skips:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    log(f"rebuilt {HISTORY.relative_to(REPO)}: {len(allrows)} row(s) + {len(skips)} skip(s)")
    return allrows


# --- charts (hand-rolled SVG, stdlib only) -----------------------------------


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


CHART_CSS = """
  .surface { fill: #fcfcfb; }
  .grid    { stroke: #e1e0d9; stroke-width: 1; }
  .axis    { stroke: #c3c2b7; stroke-width: 1; }
  .t-pri   { fill: #0b0b0b; }
  .t-sec   { fill: #52514e; }
  .t-mut   { fill: #898781; }
  text     { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  .tick    { font-variant-numeric: tabular-nums; }
__DARK__
"""


def svg_chart(title: str, ylabel: str, series, labels: list[str], out: Path,
              y_fmt: str = "{:.3g}") -> str | None:
    """One line chart. ``series`` = [(label, [(x_index, value), …]), …].

    Returns the SVG markup (also written to ``out``), or None when no series
    has two or more points — a single observation is not a trend and is shown
    as a table instead of a one-point "line".
    """
    series = [(lab, pts) for lab, pts in series if pts]
    if not any(len(pts) >= 2 for _, pts in series):
        return None
    W, H = 760, 330
    ml, mr, mt, mb = 74, 172, 38, 44
    pw, ph = W - ml - mr, H - mt - mb
    ys = [v for _, pts in series for _, v in pts]
    n = len(labels)
    y_max = (max(ys) * 1.15) or 1.0
    xs_max = max(1, n - 1)

    def px(i):
        return ml + (pw * (i / xs_max) if n > 1 else pw / 2)

    def py(v):
        return mt + ph * (1 - v / y_max)

    dark = []
    for i, (lc, dc) in enumerate(zip(SERIES_LIGHT, SERIES_DARK), start=1):
        dark.append(f"    .s{i} {{ fill: {dc}; stroke: {dc}; }}")
    dark_block = (
        "  @media (prefers-color-scheme: dark) {\n"
        "    .surface { fill: #1a1a19; }\n"
        "    .grid    { stroke: #2c2c2a; }\n"
        "    .axis    { stroke: #383835; }\n"
        "    .t-pri   { fill: #ffffff; }\n"
        "    .t-sec   { fill: #c3c2b7; }\n"
        "    .t-mut   { fill: #898781; }\n" + "\n".join(dark) + "\n  }"
    )
    css = CHART_CSS.replace("__DARK__", dark_block)
    for i, lc in enumerate(SERIES_LIGHT, start=1):
        css += f"\n  .s{i} {{ fill: {lc}; stroke: {lc}; }}"
    # LAST, and load-bearing: a CSS declaration beats a presentation attribute,
    # so `.sN { fill: … }` was silently overriding `fill="none"` on the
    # polylines and filling every line down to its closing edge. Invisible while
    # series had 3 near-horizontal points; obvious at 31 points across a decade
    # of values. Hand-rolled SVG fails silently — look at the rendered page.
    css += "\n  .ln { fill: none; }"

    p = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
        f'height="{H}" role="img" aria-label="{_esc(title)}" font-size="11">',
        f"<style>{css}</style>",
        f'<rect class="surface" width="{W}" height="{H}"/>',
        f'<text class="t-pri" x="{ml}" y="22" font-size="13" font-weight="600">{_esc(title)}</text>',
    ]
    for k in range(6):  # 5 divisions
        v = y_max * k / 5
        y = py(v)
        p.append(f'<line class="grid" x1="{ml}" y1="{y:.1f}" x2="{ml + pw}" y2="{y:.1f}"/>')
        p.append(f'<text class="t-mut tick" x="{ml - 8}" y="{y + 4:.1f}" '
                 f'text-anchor="end">{y_fmt.format(v)}</text>')
    p.append(f'<line class="axis" x1="{ml}" y1="{mt + ph:.1f}" x2="{ml + pw}" y2="{mt + ph:.1f}"/>')
    step = max(1, n // 8)
    for i in range(0, n, step):
        x = px(i)
        p.append(f'<text class="t-mut tick" x="{x:.1f}" y="{mt + ph + 16}" '
                 f'text-anchor="end" transform="rotate(-35 {x:.1f} {mt + ph + 16})">'
                 f'{_esc(labels[i])}</text>')
    p.append(f'<text class="t-sec" x="16" y="{mt + ph / 2:.1f}" text-anchor="middle" '
             f'transform="rotate(-90 16 {mt + ph / 2:.1f})">{_esc(ylabel)}</text>')
    for si, (label, pts) in enumerate(series):
        cls = f"s{si % len(SERIES_LIGHT) + 1}"
        if len(pts) > 1:
            coords = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in pts)
            p.append(f'<polyline class="{cls} ln" points="{coords}" stroke-width="2"/>')
        for i, v in pts:
            p.append(f'<circle class="{cls}" cx="{px(i):.1f}" cy="{py(v):.1f}" r="4" '
                     f'stroke="none"><title>{_esc(label)} @ {_esc(labels[i])}: '
                     f'{y_fmt.format(v)} {_esc(ylabel)}</title></circle>')
        ly = mt + 10 + si * 17
        p.append(f'<rect class="{cls}" x="{ml + pw + 16}" y="{ly - 8}" width="10" height="10" '
                 f'stroke="none"/>')
        p.append(f'<text class="t-sec" x="{ml + pw + 32}" y="{ly + 1}">{_esc(label)}</text>')
    p.append("</svg>")
    svg = "\n".join(p) + "\n"
    out.write_text(svg)
    log(f"chart → {out.relative_to(REPO)} ({len(series)} series, {len(labels)} runs)")
    return svg


# --- page --------------------------------------------------------------------

SECTIONS = ["matrix", "ladder", "anim", "arm", "bench", "size", "m4_qemu",
            "silicon", "board_anim"]


def _get(row: dict, *path):
    cur = row
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _rung_key(r: dict) -> str:
    return f"{r['w']}x{r['h']}"


def _heap_kib(row: dict, tier: str):
    """Emulated-M4 heap peak in KiB. Byte-exact where the run recorded bytes;
    the one row migrated from the retired docs/metrics ledger only ever stored
    rounded KiB, so that precision is not re-invented here."""
    b = _get(row, "m4_qemu", "tiers", tier, "heap_peak_b")
    if isinstance(b, (int, float)):
        return b / 1024
    k = _get(row, "m4_qemu", "tiers", tier, "heap_peak_kib")
    return k if isinstance(k, (int, float)) else None


def regen(history: list[dict]) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    labels = [f"{r['commit']}{'*' if r.get('dirty') else ''}" for r in history]
    written: set[str] = set()
    figures: list[str] = []

    def figure(title: str, ylabel: str, series, fname: str, y_fmt="{:.3g}",
               note: str = "") -> bool:
        svg = svg_chart(title, ylabel, series, labels, LEDGER_DIR / fname, y_fmt)
        if svg is None:
            return False
        written.add(fname)
        figures.append(
            f'<figure><a href="{fname}">{svg}</a>'
            + (f"<figcaption>{note}</figcaption>" if note else "")
            + "</figure>"
        )
        return True

    # ladder — the only sections with >1 observation today
    rung_keys: list[str] = []
    for r in history:
        for g in _get(r, "ladder", "rungs") or []:
            if _rung_key(g) not in rung_keys:
                rung_keys.append(_rung_key(g))
    rung_keys.sort(key=lambda k: int(k.split("x")[0]))

    def ladder_series(metric):
        out = []
        for k in rung_keys:
            pts = []
            for i, r in enumerate(history):
                for g in _get(r, "ladder", "rungs") or []:
                    if _rung_key(g) == k:
                        v = metric(g)
                        if v is not None:
                            pts.append((i, v))
            out.append((k, pts))
        return out

    figure("Full-frame render cost per rung (lower is better)", "ns / px",
           ladder_series(lambda g: g["full_ns_px"]), "ladder-full-ns-px.svg", "{:.2f}")
    figure("Incremental repaint cost per rung", "ns / dirty px",
           ladder_series(lambda g: g["incr_ns_dirty_px"]), "ladder-incr-ns-px.svg", "{:.2f}")
    figure("Incremental speedup vs full frame (higher is better)", "×",
           ladder_series(lambda g: g["full_ns"] / g["incr_ns"] if g.get("incr_ns") else None),
           "ladder-incr-speedup.svg", "{:.1f}")

    def rows_series(defs, *path):
        out = []
        for legend, key in defs:
            pts = []
            for i, r in enumerate(history):
                v = _get(r, *path, key)
                if isinstance(v, (int, float)):
                    pts.append((i, v))
            out.append((legend, pts))
        return out

    def m4_series(metric: str, tiers=("exact", "fast", "draft"), scale=1.0):
        out = []
        for t in tiers:
            pts = []
            for i, r in enumerate(history):
                c = m4_cell(r, t)
                v = c["metrics"].get(metric) if c else None
                if isinstance(v, (int, float)):
                    pts.append((i, v * scale))
            out.append((f"vyr {t.capitalize()}", pts))
        return out

    # THE headline. render_only, not total: the total includes the benchmark's
    # own FNV fold over every output byte (36-55 % of a small frame — error 4),
    # which is not rendering and must never be the published series again.
    figure("M4 instructions per frame — RENDER ONLY, 480×270 "
           "(plugin QEMU + libinsn, release-mcu opt-level z)", "insns / frame",
           m4_series("insns_per_frame_render_only"), "m4-insns-render-only.svg", "{:.3g}",
           note="Render only: the benchmark's own hash fold is measured separately and "
                "excluded. This is the series to quote.")
    figure("M4 instructions per frame — TOTAL, incl. the benchmark's own hash fold",
           "insns / frame", m4_series("insns_per_frame_total"), "m4-insns-total.svg",
           "{:.3g}",
           note="Kept only so the two can be compared. Every number published before "
                "2026-07-24 was this series, silently.")
    def fold_share_series():
        out = []
        for t in ("exact", "fast", "draft"):
            pts = []
            for i, r in enumerate(history):
                c = m4_cell(r, t)
                if not c:
                    continue
                fold = c["metrics"].get("harness_fold_insns")
                tot = c["metrics"].get("insns_per_frame_total")
                # `fold is not None`, never `if fold`: zero is the POINT — it is
                # what #44 achieved, and dropping it would hide the fix.
                if fold is not None and tot:
                    pts.append((i, 100.0 * fold / tot))
            out.append((f"vyr {t.capitalize()}", pts))
        return out

    figure("How much of the measured frame is the BENCHMARK hashing itself",
           "% of total", fold_share_series(), "m4-fold-share.svg", "{:.0f}",
           note="Error 4, made permanent and visible: the FNV fold is a fixed 3.11 M "
                "insns/frame, so it inflates a cheap tier far more than an expensive one.")
    figure("M4 workload heap peak (counting allocator, emulated)", "KiB",
           m4_series("heap_peak_b", scale=1 / 1024), "m4-heap.svg", "{:.0f}")
    figure("M4 flash — text+data of the measured ELF", "KiB",
           m4_series("flash_text_data_b", scale=1 / 1024), "m4-flash.svg", "{:.0f}",
           note="The run-qemu vehicle at each tier, not the size matrix — this is the "
                "binary the instruction count was taken on.")

    def host_series(metric: str):
        out = []
        for t in ("exact", "fast", "draft"):
            pts = []
            for i, r in enumerate(history):
                for c in (r.get("matrix") or {}).get("cells", []):
                    if c.get("platform") == "host" and c.get("tier") == t \
                            and isinstance(c["metrics"].get(metric), (int, float)):
                        pts.append((i, c["metrics"][metric]))
            out.append((f"host {t.capitalize()}", pts))
        return out

    figure("Host (x86-64) frame cost — the same scene, the other platform",
           "ns / frame", host_series("ns_per_frame"), "host-ns-frame.svg", "{:.3g}",
           note="Side by side with the M4 series above: a change that moves one and not "
                "the other is a platform pathology. x86-64 has hardware f64 and the M4F "
                "does not — that asymmetry hid #32's soft-float trig bill for months.")
    figure("Real silicon cycles per frame (F429 @180 MHz, DWT_CYCCNT)", "cycles / frame",
           [(lab, [(i, _get(r, "silicon", "tiers", k, "cycles_per_frame"))
                   for i, r in enumerate(history)
                   if _get(r, "silicon", "tiers", k, "cycles_per_frame")])
            for lab, k in (("Exact", "exact"), ("Draft", "draft"))],
           "silicon-cycles.svg", "{:.3g}")
    figure("Flash on the M4 (release-mcu, text+data)", "KiB",
           rows_series([("code-only", "code"), ("+font", "font"),
                        ("+font+image", "font_image")], "size", "flash_kib"),
           "size-flash.svg", "{:.0f}")
    # The bridge: the three DERIVED ladder scalars are the only quantity BOTH
    # retired harnesses recorded, so they are the only series that can span
    # every row in the union (#25).
    figure("Ladder cost — the bridge series (spans every run)", "ns / px",
           rows_series([("480×270 full", "ladder480_full_ns_px"),
                        ("480×270 incremental", "ladder480_incr_ns_px"),
                        ("4K full", "ladder4k_full_ns_px")], "derived"),
           "ladder-bridge.svg", "{:.2f}",
           note="Projected from rungs[] where present; recorded flat by the retired "
                "docs/metrics harness. The only continuous series across the union "
                "of commits (#25).")
    figure("Primitive cost (host, vyr-bench median)", "ns / px",
           rows_series([("fill_rrect", "fill_rrect"), ("disc", "disc"),
                        ("gradient", "gradient"), ("line", "line")], "bench", "ns_px"),
           "bench-prim.svg", "{:.2g}")
    figure("Scene cost (host, vyr-bench median)", "ns / px",
           rows_series([("ir Exact", "scene_exact"), ("ir Draft", "scene_draft")],
                       "bench", "ns_px"),
           "bench-scene.svg", "{:.2g}")
    figure("Draft fast-path coverage (emulated M4)", "%",
           [("coverage", [(i, _get(r, "m4_qemu", "tiers", "draft", "fastpath_coverage_pct"))
                          for i, r in enumerate(history)
                          if _get(r, "m4_qemu", "tiers", "draft", "fastpath_coverage_pct")])],
           "fidelity-coverage.svg", "{:.0f}")

    # prune SVGs this run did not write — derived artifacts have no legacy
    for stale in LEDGER_DIR.glob("*.svg"):
        if stale.name not in written:
            stale.unlink()
            log(f"pruned stale chart {stale.relative_to(REPO)}")

    html = page_html(history, figures)
    (LEDGER_DIR / "index.html").write_text(html)
    log(f"page → {(LEDGER_DIR / 'index.html').relative_to(REPO)} "
        f"({len(history)} row(s), {len(figures)} chart(s))")


def _tbl(headers: list[str], rows: list[list[str]]) -> str:
    h = "".join(f"<th>{_esc(x)}</th>" for x in headers)
    b = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>\n" for r in rows)
    return f"<table><tr>{h}</tr>\n{b}</table>"


def _latest_with(history: list[dict], section: str):
    for r in reversed(history):
        if r.get(section):
            return r
    return None


def page_html(history: list[dict], figures: list[str]) -> str:
    blocks: list[str] = []

    r = _latest_with(history, "ladder")
    if r:
        rows = [
            [_rung_key(g), f"{g['full_ns'] / 1e6:.3f}", f"{g['full_ns_px']:.2f}",
             f"{g['headroom_full_x']:.2f}×", f"{g['incr_ns'] / 1e6:.3f}",
             f"{g['incr_ns_dirty_px']:.2f}", f"{g['headroom_incr_x']:.2f}×",
             f"{g['dirty_pct']:.1f}%", f"{g['full_ns'] / g['incr_ns']:.1f}×"]
            for g in r["ladder"]["rungs"]
        ]
        blocks.append(
            f"<h2>Host resolution ladder — <code>{_esc(r['commit'])}</code> · {_esc(r['ts'])}</h2>"
            + _tbl(["rung", "full ms", "ns/px", "×60fps", "incr ms", "ns/dirty-px",
                    "×60fps", "dirty", "speedup"], rows)
            + '<p class="lede">×60fps = headroom vs the 16.67 ms budget (&gt;1 fits). '
              "The incremental path is where 60 fps @ 4K lives.</p>"
        )
        a = r.get("anim")
        if a:
            blocks.append(
                f"<p>Anim acceptance: <b>{a['frames']} frames @ {a['w']}×{a['h']}</b>, "
                f"run hash <code>{_esc(a['run_hash'])}</code>, dirty {a['dirty_pct']:.1f}%/step, "
                f"{a['ms_frame']:.2f} ms/frame (host wall clock).</p>"
            )
        m = r.get("arm")
        if m:
            verdict = m.get("cross_isa", "?")
            badge = "byte-identical ✅" if verdict == "identical" else f"❌ {verdict}"
            blocks.append(
                f"<p>Cross-ISA rung (qemu-arm-static, ARMv7): <b>{badge}</b> across "
                f"{m['frames']} frames. Emulated wall time is NON-target-indicative.</p>"
            )

    r = _latest_with(history, "matrix")
    if r:
        mx = r["matrix"]
        rows = []
        for c in mx["cells"]:
            m = c["metrics"]
            if c["status"] != "measured":
                continue

            def n(v, fmt="{:,}"):
                return fmt.format(v) if isinstance(v, (int, float)) else "—"

            share = (100.0 * m["harness_fold_insns"] / m["insns_per_frame_total"]) \
                if m.get("harness_fold_insns") and m.get("insns_per_frame_total") else None
            fp = c.get("fold_provenance") or "—"
            rows.append([
                f"{c['platform']} / {c.get('firmware', 'vyr')} {c['tier']} / O{c['opt_level']}",
                n(m.get("insns_per_frame_total")),
                n(m.get("harness_fold_insns")),
                f"<b>{n(m.get('insns_per_frame_render_only'))}</b>",
                f"{share:.1f}%" if share is not None else "—",
                _esc(fp.split(" (")[0]),
                n(m.get("cycles_per_frame")),
                n(m.get("ns_per_frame"), "{:,.0f}"),
                n(m.get("heap_peak_b")),
                n(m.get("stack_high_water_b")),
                n(m.get("flash_text_data_b")),
                f"<code>{_esc(m.get('frame_hash') or '—')}</code>",
            ])
        unavail = mx.get("platforms_unavailable") or {}
        why = " ".join(f"<code>{_esc(k)}</code>: {_esc(v)}." for k, v in unavail.items())
        inst = next((c.get("provenance", {}).get("instrument") for c in mx["cells"]
                     if c.get("platform") == "qemu-m4" and c.get("provenance", {}).get("instrument")),
                    {})
        q = (inst.get("qemu") or {})
        blocks.append(
            f"<h2>The matrix — <code>{_esc(r['commit'])}</code> "
            f"<span class=\"lede\">{_esc(r.get('subject', ''))}</span></h2>"
            + _tbl(["platform / firmware tier / opt", "insns total", "hash fold",
                    "insns RENDER ONLY", "fold share", "fold provenance", "cycles",
                    "ns/frame", "heap B", "stack B", "flash B", "frame hash"], rows)
            + '<p class="lede"><b>render-only is the number to quote</b>, and '
              "<b>how it was obtained is part of the number</b>: "
              "<code>absent-from-window-by-build</code> means the timed pass contains no "
              "fold at all (#44) so total IS render-only; "
              "<code>measured-differential</code> means the same firmware was rebuilt "
              "with the fold folding an empty slice and the two counts differenced, in "
              "this cell, at this opt-level; <code>derived-by-subtraction</code> would "
              "mean a fold figure carried in from elsewhere and is never produced here. "
              "The fold is not rendering, and being a fixed cost it inflates a cheap "
              "tier far more than an expensive one (error 4 in "
              '<a href="https://github.com/awtoau/vyr/blob/main/docs/measurements/perf-history.md">'
              "perf-history.md</a>). "
            + (f"Counter: {_esc(q.get('version'))} @ "
               f"<code>{_esc((q.get('source_commit') or '')[:12])}</code>, plugin "
               f"<code>{_esc(inst.get('plugin'))}</code> "
               f"<code>{_esc(inst.get('plugin_args'))}</code>, machine "
               f"<code>{_esc(inst.get('machine'))}</code>, "
               f"<code>{_esc(inst.get('icount'))}</code>. " if q else "")
            + (f"Platforms attempted but unavailable — {why}" if why else "")
            + "</p>"
        )

    r = _latest_with(history, "silicon")
    if r:
        s = r["silicon"]
        # cycles/insn needs BOTH legs of the same commit. The silicon row and the
        # matrix row are usually different rows (the board is measured on demand),
        # so pair them by commit — including the commits a matrix row `covers`,
        # which compile byte-identically to it.
        cpi = dict(_get(r, "derived", "cycles_per_insn") or {})
        if not cpi:
            mrow = next((h for h in history
                         if h.get("matrix") and (h["commit"] == r["commit"]
                                                 or r["commit"] in (h.get("covers") or []))),
                        None)
            if mrow:
                for tier, t in s["tiers"].items():
                    c = m4_cell(mrow, tier)
                    tot = c["metrics"].get("insns_per_frame_total") if c else None
                    if tot and t.get("cycles_per_frame"):
                        cpi[tier] = round(t["cycles_per_frame"] / tot, 3)
        rows = [
            [tier.capitalize(), f"{t['cycles_per_frame']:,}", f"{t['ms_per_frame']:.2f}",
             f"{t['heap_peak_b']:,} B", f"{cpi.get(tier, '—')}",
             f"{t['spread_ppm']} ppm", f"<code>{_esc(t['frame_hash'])}</code>"]
            for tier, t in s["tiers"].items() if t.get("cycles_per_frame")
        ]
        c = s.get("clock", {})
        blocks.append(
            f"<h2>Real silicon — <code>{_esc(r['commit'])}</code></h2>"
            + _tbl(["tier", "cycles/frame", "ms @180 MHz", "heap peak", "cycles/insn",
                    "spread", "frame hash"], rows)
            + f'<p class="lede">{_esc(s.get("board"))}, {_esc(s.get("timer"))}, '
              f"{c.get('sysclk_hz', 0) / 1e6:.0f} MHz ({_esc(c.get('source', ''))}), "
              f"{c.get('flash_wait_states', '?')} flash wait states, ART "
              f"prefetch/I/D {c.get('art_prefetch', '?')}/{c.get('art_icache', '?')}/"
              f"{c.get('art_dcache', '?')}. cycles/insn is the cost emulation cannot "
              "model — it comes from this row's <code>insns</code> section.</p>"
        )

    r = _latest_with(history, "m4_qemu")
    if r:
        rows = []
        for tier, t in r["m4_qemu"]["tiers"].items():
            kib = _heap_kib(r, tier)
            rows.append([
                tier.capitalize(),
                f"{kib:.1f} KiB" if kib is not None else "—",
                f"{t['fastpath_coverage_pct']:.1f}%" if t.get("fastpath_coverage_pct") else "—",
                f"<code>{_esc(t.get('frame_hash', '—'))}</code>",
            ])
        blocks.append(
            f"<h2>Emulated M4 (stock qemu) — <code>{_esc(r['commit'])}</code></h2>"
            + _tbl(["tier", "heap peak", "Draft fast-path", "frame hash"], rows)
            + '<p class="lede">Deterministic quantities only. This vehicle\'s '
              "<code>SYS_CLOCK</code> reading is host wall time, not instructions "
              "(docs/performance.md §5); it is recorded but never charted.</p>"
        )

    r = _latest_with(history, "size")
    if r and r["size"].get("flash_kib"):
        rows = [[k.replace("_", "+"), f"{v:.1f}"] for k, v in r["size"]["flash_kib"].items()]
        blocks.append(
            f"<h2>Static size — <code>{_esc(r['commit'])}</code></h2>"
            + _tbl(["config", "flash KiB"], rows)
        )

    r = _latest_with(history, "bench")
    if r:
        rows = [[k, f"{v:.4g}"] for k, v in r["bench"]["ns_px"].items()]
        blocks.append(
            f"<h2>Host bench medians — <code>{_esc(r['commit'])}</code></h2>"
            + _tbl(["case", "ns/px"], rows)
        )

    # commits that could not be measured — recorded, never omitted
    skips = [r for r in load_all() if r.get("kind") == "skip"]
    if skips:
        blocks.append(
            "<h2>Commits that could not be measured</h2>"
            + _tbl(["commit", "date", "subject", "why"],
                   [[f"<code>{_esc(s['commit'])}</code>", _esc((s.get('commit_date') or '')[:10]),
                     _esc(s.get("subject", "")), _esc(s.get("reason", ""))] for s in skips])
            + '<p class="lede">An honest hole beats a plausible interpolation.</p>'
        )

    # the coverage matrix: which row measured what — sparsity, stated
    hdr = ["run", "commit", "when"] + SECTIONS
    rows = []
    for i, r in enumerate(history):
        cells = [str(i), f"<code>{_esc(r['commit'])}{'*' if r.get('dirty') else ''}</code>",
                 f"<code>{_esc(r['ts'])}</code>"]
        cells += ["●" if r.get(s) else "·" for s in SECTIONS]
        rows.append(cells)
    matrix = _tbl(hdr, rows)

    charts = "\n".join(figures) or (
        '<p class="lede">No section yet has two observations — nothing is a trend, '
        "so nothing is charted.</p>"
    )
    css = """
  :root { color-scheme: light dark; --ink:#0b0b0b; --sec:#52514e; --line:#c3c2b7; }
  @media (prefers-color-scheme: dark) {
    :root { --ink:#ffffff; --sec:#c3c2b7; --line:#383835; }
  }
  body { font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
         max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: var(--ink); }
  h1 { font-size: 1.45rem; } h2 { font-size: 1.05rem; margin-top: 2.2rem; }
  p.lede { color: var(--sec); }
  table { border-collapse: collapse; font-size: .85rem; font-variant-numeric: tabular-nums; }
  th, td { border: 1px solid var(--line); padding: .25rem .55rem; text-align: right; }
  th:first-child, td:first-child { text-align: left; }
  figure { margin: 1.2rem 0; }
  figure svg { max-width: 100%; height: auto; border: 1px solid var(--line); border-radius: 4px; }
  figcaption { color: var(--sec); font-size: .85rem; }
  code { font-size: .9em; }
"""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>vyr measurement ledger</title>
<style>{css}</style>
</head>
<body>
<h1>vyr measurement ledger</h1>
<p class="lede">THE single measurement history for this project
(<a href="https://github.com/awtoau/vyr/issues/25">#25</a>): the performance
<b>matrix</b> (platform × tier × opt-level), the host resolution ladder and anim
acceptance, cross-ISA verdicts, static size, and real F429 silicon cycles — one
append-only row per run in
<a href="history.jsonl"><code>history.jsonl</code></a>, this page regenerated
from it by <code>./dev.py track</code>. Sections are independent and optional:
a run records what it measured and nothing else. <b>Rows are sparse where the
measurement was never taken</b> — see the coverage matrix. Provenance for every
number: <a href="https://github.com/awtoau/vyr/blob/main/docs/performance.md">docs/performance.md</a>.</p>
<p class="lede"><b>Schema 3 (2026-07-24) is a rebuild, not an append.</b> Every
M4 instruction figure here was produced by one instrument —
<code>scripts/perf-harness.py</code> — replayed over history by
<code>scripts/perf-replay.py</code>: old commits measured with today's tools,
never with their own. The SYS_CLOCK-derived rows of the previous ledger were
host wall time and are deleted rather than relabelled, and the benchmark's own
hash fold is now a separate recorded field instead of being silently inside the
headline. The four measurement errors that forced this are in
<a href="https://github.com/awtoau/vyr/blob/main/docs/measurements/perf-history.md">perf-history.md</a>.</p>

{"".join(blocks)}

<h2>Trends</h2>
<p class="lede">A series is charted only where it has two or more observations;
single observations stay in the tables above rather than being drawn as a
one-point line. <b>A tier that did not exist yet simply starts later</b> —
<code>Draft</code> from <code>5da42a2</code>, <code>Fast</code> from
<code>cb29f52</code> — rather than being interpolated backwards or drawn as a
gap in a line that pretends to span it. Where a platform could not be measured
at all, the cell carries a written reason instead of a value.</p>
{charts}

<h2>Coverage — what each run measured</h2>
{matrix}
<p class="lede">● recorded · nothing recorded. <code>*</code> = measured against
a dirty worktree. Host wall-clock numbers are desktop-host numbers; emulated
numbers are labelled where they are not target-indicative.</p>
</body>
</html>
"""


def main(argv: list[str]) -> int:
    regen_only = "--regen-only" in argv
    rest = [a for a in argv if a != "--regen-only"]
    rebuild = None
    if rest and rest[0] == "--rebuild-from-replay":
        if len(rest) != 2:
            log("ERROR: --rebuild-from-replay takes exactly one path")
            return 2
        rebuild = Path(rest[1])
        rest = []
    if rest:
        log(f"ERROR: unknown args: {rest}")
        return 2
    if rebuild is not None:
        rebuild_from_replay(rebuild)
    elif not regen_only and append_row() is None:
        return 1
    history = load_history()
    if not history:
        log(f"ERROR: {HISTORY.relative_to(REPO)} is empty — nothing to chart")
        return 1
    regen(history)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

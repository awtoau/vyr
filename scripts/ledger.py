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

THE PAGE IS A GRID, NOT A TABLE (2026-07-24)

    ``docs/perf/index.html`` draws the matrix AS a matrix: rows are platform
    legs, columns are quality tiers, each tile is one cell, and the opt-level
    axis lives inside the qemu-m4 tiles as a labelled bar strip. The 13-column
    table it replaces is still there, one ``<details>`` down, because a chart
    without its table view is a chart you cannot check. Every tile carries a
    ``fold_provenance`` chip and every ``null`` carries the written reason the
    harness recorded for it — the two things a sideways-scrolling table hid.

    After changing anything about the page, run ``scripts/ledger-verify.py``:
    it re-checks that no measured value moved, that the page is self-contained
    and theme-aware, and it SCREENSHOTS the page in both themes. Hand-rolled
    SVG fails silently; only looking catches it.

Usage: scripts/ledger.py [--regen-only]
       scripts/ledger.py --rebuild-from-replay tmp/perf-replay.jsonl
       scripts/ledger-verify.py            (check + render, after any change)
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


CHART_INK = {
    "light": {"surface": "#fcfcfb", "grid": "#e1e0d9", "axis": "#c3c2b7",
              "pri": "#0b0b0b", "sec": "#52514e", "mut": "#898781"},
    "dark": {"surface": "#1a1a19", "grid": "#2c2c2a", "axis": "#383835",
             "pri": "#ffffff", "sec": "#c3c2b7", "mut": "#898781"},
}

CHART_BASE_CSS = (
    '  text  { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }\n'
    "  .tick { font-variant-numeric: tabular-nums; }\n"
)


def _chart_rules(scope: str, mode: str) -> str:
    """Every themeable declaration, once, under one scope.

    STRUCTURAL, not cosmetic: ``.sN`` sets **stroke only** and ``.fN`` sets
    **fill only**. The old sheet set both on one class, and because a CSS
    declaration beats a presentation attribute that ``fill`` silently overrode
    ``fill="none"`` on every polyline — filling each line down to its closing
    edge. Invisible at 3 near-horizontal points, obvious at 31. Splitting the
    classes means no fill declaration can ever reach a line, whatever the
    scope's specificity or where it lands in the cascade.
    """
    k = CHART_INK[mode]
    series = SERIES_LIGHT if mode == "light" else SERIES_DARK
    s = f"{scope} " if scope else ""
    out = [
        f"  {s}.surface {{ fill: {k['surface']}; }}",
        f"  {s}.grid    {{ stroke: {k['grid']}; stroke-width: 1; }}",
        f"  {s}.axis    {{ stroke: {k['axis']}; stroke-width: 1; }}",
        f"  {s}.t-pri   {{ fill: {k['pri']}; }}",
        f"  {s}.t-sec   {{ fill: {k['sec']}; }}",
        f"  {s}.t-mut   {{ fill: {k['mut']}; }}",
    ]
    for i, c in enumerate(series, start=1):
        out.append(f"  {s}.s{i} {{ stroke: {c}; }}  {s}.f{i} {{ fill: {c}; }}")
    return "\n".join(out)


def chart_css() -> str:
    """Theme-aware three ways, exactly like the page that inlines this SVG: a
    light default, an OS media query, and explicit ``[data-theme]`` scopes so
    the page's own toggle beats the OS in BOTH directions. Inlined in the page
    ``:root`` is ``<html>`` and the toggle wins; opened as a standalone .svg
    ``:root`` is ``<svg>``, which carries no data-theme, so only the media
    query applies — correct for a file viewed on its own."""
    return "\n".join([
        CHART_BASE_CSS,
        _chart_rules("", "light"),
        "  @media (prefers-color-scheme: dark) {",
        _chart_rules(':root:not([data-theme="light"])', "dark"),
        "  }",
        _chart_rules(':root[data-theme="dark"]', "dark"),
        _chart_rules(':root[data-theme="light"]', "light"),
    ]) + "\n"


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

    css = chart_css()

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
        # Fixed slot order, never cycled: slot N is this series' identity for
        # the life of the chart. `sN` strokes, `fN` fills — see _chart_rules.
        slot = si % len(SERIES_LIGHT) + 1
        if len(pts) > 1:
            coords = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in pts)
            p.append(f'<polyline class="s{slot}" points="{coords}" fill="none" '
                     f'stroke-width="2"/>')
        for i, v in pts:
            p.append(f'<circle class="f{slot}" cx="{px(i):.1f}" cy="{py(v):.1f}" r="4" '
                     f'stroke="none"><title>{_esc(label)} @ {_esc(labels[i])}: '
                     f'{y_fmt.format(v)} {_esc(ylabel)}</title></circle>')
        ly = mt + 10 + si * 17
        p.append(f'<rect class="f{slot}" x="{ml + pw + 16}" y="{ly - 8}" width="10" '
                 f'height="10" stroke="none"/>')
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
            f'<figure class="card"><a href="{fname}">{svg}</a>'
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
    return f'<div class="tablewrap"><table><thead><tr>{h}</tr></thead>\n<tbody>{b}</tbody></table></div>'


def _latest_with(history: list[dict], section: str):
    for r in reversed(history):
        if r.get(section):
            return r
    return None


# --- the grid view -----------------------------------------------------------
#
# The ledger's subject is a MATRIX — platform x tier x opt-level — and for a
# year it was drawn as a 13-column table that scrolled sideways, which is the
# one shape that hides both of the things this project got wrong four times:
# which cells are missing, and how each render-only figure was obtained. The
# grid below is that matrix: rows are platform legs, columns are tiers, each
# tile is a cell, and provenance is a chip in a fixed position on every tile.

TIER_COLS = [
    ("exact", "Exact",
     "anti-aliases curves through tiny-skia — the full-fidelity path"),
    ("fast", "Fast",
     "Draft's integer spans for straight edges, tiny-skia for curves; "
     "byte-identical to Exact on this fixture"),
    ("draft", "Draft",
     "integer span fills, no anti-aliasing, radius&gt;0 drawn square"),
]

# (key, title, subtitle, headline-metric kind)
PLATFORM_ROWS = [
    ("qemu-m4", "Emulated Cortex-M4F", "qemu-m4",
     "STM32F405 under plugin QEMU (netduinoplus2) with libinsn. The only leg "
     "carrying an instruction counter, and therefore the only source of a "
     "published M4 figure.", "insns"),
    ("board", "Real silicon", "board",
     "STM32F429I-DISC1 @180 MHz, DWT_CYCCNT. The only leg that can bill a real "
     "cycle — the cost emulation cannot model.", "cycles"),
    ("host", "Host x86-64", "host",
     "The same run-qemu workload built natively. Wall time here is a desktop "
     "number; this leg's real job is the reference frame hash every other leg "
     "is checked against.", "ns"),
    ("arm32", "ARM32 user-mode", "arm32",
     "armv7-unknown-linux-musleabihf under qemu-arm-static. A pixel-equivalence "
     "leg: it proves the ARM build renders the same bytes, and deliberately "
     "measures nothing else.", "hash"),
]

# fold_provenance -> (chip class, short label, what it means)
FOLD_PROV = {
    "absent-from-window-by-build": (
        "good", "fold absent by build",
        "the timed pass contains no fold at all (#44), so total IS render-only"),
    "measured-differential": (
        "warn", "fold differenced in-cell",
        "this firmware was rebuilt with the fold folding an empty slice and the "
        "two counts differenced, in this cell, at this opt-level"),
    "derived-by-subtraction": (
        "crit", "fold carried in",
        "a fold figure taken from somewhere other than this cell — never "
        "produced by this harness, and never allowed to look measured"),
}

OPT_RANK = {"z": 0, "s": 1, "2": 2, "3": 3}


def _opt_short(o) -> str:
    return (str(o or "?").split(" ")[0]) or "?"


def _opt_rank(c: dict) -> int:
    return OPT_RANK.get(_opt_short(c.get("opt_level")), 9)


def _ins(v):
    """Instruction counts: M above a million, k above a thousand. Proportional
    figures — these are hero numbers, not a column to align."""
    if not isinstance(v, (int, float)):
        return None
    if v >= 1e6:
        return f"{v / 1e6:.2f} M"
    if v >= 1e3:
        return f"{v / 1e3:.1f} k"
    return f"{v:,.0f}"


def _kib(v):
    return f"{v / 1024:,.1f} KiB" if isinstance(v, (int, float)) else None


def _wall(v):
    if not isinstance(v, (int, float)):
        return None
    return f"{v / 1000:,.1f} µs" if v >= 1000 else f"{v:,.0f} ns"


def _hash_short(h):
    return f"{h[:8]}…{h[-4:]}" if isinstance(h, str) and len(h) > 14 else h


def _chip(cls: str, label: str, title: str = "") -> str:
    t = f' title="{_esc(title)}"' if title else ""
    return f'<span class="chip {cls}"{t}><i></i>{_esc(label)}</span>'


def _prov_chip(cell: dict) -> str:
    """Provenance, in a fixed position on every tile. Invisible provenance is
    precisely how this project published a wrong number four times."""
    fp = cell.get("fold_provenance")
    if fp:
        key = fp.split(" (")[0].strip()
        cls, label, meaning = FOLD_PROV.get(
            key, ("crit", key, "unrecognised provenance — treat as unverified"))
        return _chip(cls, label, f"fold_provenance: {fp} — {meaning}")
    if cell.get("status") != "measured":
        return _chip("warn", "not measured", cell.get("reason") or "")
    return _chip("mut", "no fold to account for",
                 "this leg has no instruction counter, so there is no total to "
                 "separate a hash fold out of")


def _common_notes(cells: list[dict]) -> tuple[dict, dict]:
    """Split the written reasons into leg-wide and cell-specific.

    A reason carried identically by every cell of a platform is a property of
    the PLATFORM, not of the cell — the M4 has no PMU whichever tier you build.
    Printing it once per tile is how a grid turns back into a wall of text, so
    it is hoisted to the band and the tiles keep only what is theirs. Returns
    ``(cannot, notes)``: metrics that are null everywhere with a reason, and
    metrics that have a value and a leg-wide annotation."""
    if not cells:
        return {}, {}
    cannot, notes = {}, {}
    keys = {k for c in cells for k in (c.get("metric_notes") or {})}
    for k in keys:
        texts = {(c.get("metric_notes") or {}).get(k) for c in cells}
        if len(texts) != 1 or None in texts:
            continue  # differs cell to cell — it belongs on the tile
        nulls = {(c.get("metrics") or {}).get(k) is None for c in cells}
        (cannot if nulls == {True} else notes)[k] = texts.pop()
    return cannot, notes


def _metric_rows(cell: dict, defs, skip: set) -> str:
    """One metric per row. A null is never blank and never zero: it renders as
    an em dash carrying the written reason the ledger recorded for it."""
    out = []
    m = cell.get("metrics") or {}
    notes = cell.get("metric_notes") or {}
    for key, label, fmt in defs:
        v = m.get(key)
        shown = fmt(v) if v is not None else None
        if shown is None:
            if key in skip or key not in notes:
                continue
            out.append(f'<div class="m null"><span class="k">{_esc(label)}</span>'
                       f'<span class="v">—</span>'
                       f'<span class="why">{_esc(notes[key])}</span></div>')
        else:
            # A note already hoisted to the band is not repeated per tile.
            note = None if key in skip else notes.get(key)
            extra = f'<span class="why">{_esc(note)}</span>' if note else ""
            out.append(f'<div class="m"><span class="k">{_esc(label)}</span>'
                       f'<span class="v">{_esc(shown)}</span>{extra}</div>')
    return f'<div class="ms">{"".join(out)}</div>' if out else ""


METRIC_DEFS = {
    "insns": [("insns_per_frame_total", "insns total", _ins),
              ("harness_fold_insns", "hash fold", _ins),
              ("heap_peak_b", "heap peak", _kib),
              ("stack_high_water_b", "stack high-water", _kib),
              ("flash_text_data_b", "flash text+data", _kib)],
    "ns": [("ns_per_px", "ns / px", lambda v: f"{v:,.4g}"),
           ("heap_peak_b", "heap peak", _kib)],
    "hash": [("heap_peak_b", "heap peak", _kib)],
    "cycles": [("cycles_per_frame", "cycles / frame", lambda v: f"{v:,.0f}"),
               ("heap_peak_b", "heap peak", _kib)],
}


def _headline(kind: str, cell: dict):
    """(value, unit, css-class). The headline series is render_only wherever a
    leg can produce one — never total, which is 36-55 % the benchmark hashing
    its own output on a cheap tier (error 4)."""
    m = cell.get("metrics") or {}
    if kind == "insns":
        v = _ins(m.get("insns_per_frame_render_only"))
        return (v, "insns / frame · render only", "") if v else None
    if kind == "ns":
        v = _wall(m.get("ns_per_frame"))
        return (v, "per frame · host wall clock", "") if v else None
    if kind == "cycles":
        v = m.get("cycles_per_frame")
        return (f"{v / 1e6:.1f} M", "cycles / frame · DWT_CYCCNT", "") if v else None
    if kind == "hash":
        v = m.get("frame_hash")
        return (_hash_short(v), "frame hash — this leg's whole output", "mono") if v else None
    return None


def _opt_strip(cells: list[dict], canonical: dict | None) -> str:
    """The opt-level axis, in the tile. One hue for every bar — they are all
    equally measured, so colour carries nothing here; the shipped level and the
    cheapest are called out by label, not by hue."""
    vals = [(c, (c.get("metrics") or {}).get("insns_per_frame_render_only"))
            for c in cells]
    vals = [(c, v) for c, v in vals if isinstance(v, (int, float))]
    if len(vals) < 2:
        return ""
    top = max(v for _, v in vals)
    low = min(v for _, v in vals)
    rows = []
    for c, v in vals:
        tags = ""
        if canonical is not None and c is canonical:
            tags += '<span class="tag ship">shipped</span>'
        if v == low:
            tags += '<span class="tag best">cheapest</span>'
        rows.append(
            f'<div class="orow" title="{_esc(c.get("opt_level"))}">'
            f'<span class="ol">O{_esc(_opt_short(c.get("opt_level")))}</span>'
            f'<span class="obar"><i style="width:{100.0 * v / top:.1f}%"></i></span>'
            f'<span class="ov">{_esc(_ins(v))}</span>{tags}</div>')
    return (
        '<div class="optstrip"><div class="opt-t">opt-level axis'
        '<span>lower is better</span></div>' + "".join(rows)
        + f'<p class="opt-n"><b>{top / low:.2f}×</b> spread across the four '
          "levels — measured, not assumed (#33).</p></div>")


def _tile(tier_key: str, tier_label: str, cells: list[dict], kind: str,
          common: dict, host_hash: dict, shared_reason: str | None = None) -> str:
    head = f'<span class="tier">{_esc(tier_label)}</span>'
    if not cells:
        return (f'<article class="tile absent"><div class="tile-h">{head}'
                + _chip("mut", "not attempted", "no cell for this platform × tier "
                                                "in the latest matrix row")
                + '</div><div class="hero none">no cell</div>'
                  '<p class="why">This run did not attempt this platform × tier. '
                  "A missing cell is not a zero.</p></article>")

    measured = [c for c in cells if c.get("status") == "measured"]
    if not measured:
        reasons = []
        for c in cells:
            r = c.get("reason") or "no reason recorded — which is itself a bug"
            if r not in reasons and r != shared_reason:
                reasons.append(r)
        return (f'<article class="tile null"><div class="tile-h">{head}'
                + _prov_chip(cells[0])
                + '</div><div class="hero none">not measured</div>'
                + ("".join(f'<p class="why"><b>Why:</b> {_esc(r)}</p>' for r in reasons)
                   or '<p class="why">Reason above — it is the same for every tier '
                      "on this leg.</p>")
                + "</article>")

    canonical = next((c for c in measured if _opt_short(c.get("opt_level")) == "z"),
                     measured[0])
    hl = _headline(kind, canonical)
    if hl is None:
        for c in measured:
            hl = _headline(kind, c)
            if hl:
                canonical = c
                break
    opt = _opt_short(canonical.get("opt_level"))
    body = []
    if hl:
        val, unit, cls = hl
        body.append(f'<div class="hero {cls}">{_esc(val)}</div>'
                    f'<div class="hero-u">{unit} · O<b>{_esc(opt)}</b></div>')
    else:
        body.append('<div class="hero none">no headline metric</div>')
    if kind == "insns":
        body.append(_opt_strip(sorted(measured, key=_opt_rank), canonical))
    ro = (canonical.get("metrics") or {}).get("insns_per_frame_render_only")
    if isinstance(ro, (int, float)):
        body.append(f'<p class="perpx">{ro / SCENE_PX:,.1f} insns per pixel '
                    f"over the {SCENE_PX:,}-px reference scene</p>")
    body.append(_metric_rows(canonical, METRIC_DEFS.get(kind, []), set(common)))

    fh = (canonical.get("metrics") or {}).get("frame_hash")
    foot = ""
    if fh and kind != "hash":
        ref = host_hash.get(tier_key)
        if ref and ref == fh:
            foot = ('<div class="tile-f ok"><code>' + _esc(_hash_short(fh))
                    + "</code> matches the host leg</div>")
        elif ref:
            foot = ('<div class="tile-f bad"><code>' + _esc(_hash_short(fh))
                    + "</code> DIFFERS from the host leg — a wrong-pixel build "
                      "reports no timing</div>")
        else:
            foot = ('<div class="tile-f"><code>' + _esc(_hash_short(fh))
                    + "</code> frame hash (no host leg to check against)</div>")
    elif kind == "hash" and fh:
        ref = host_hash.get(tier_key)
        foot = ('<div class="tile-f ok">byte-identical to the host leg</div>'
                if ref == fh else
                '<div class="tile-f bad">does NOT match the host leg</div>')
    return (f'<article class="tile"><div class="tile-h">{head}'
            + _prov_chip(canonical) + "</div>" + "".join(body) + foot + "</article>")


def matrix_grid(mx: dict, cross: dict | None = None) -> str:
    cross = cross or {}
    cells = [c for c in mx.get("cells", []) if c.get("firmware", "vyr") == "vyr"]
    host_hash = {c["tier"]: (c.get("metrics") or {}).get("frame_hash")
                 for c in cells if c.get("platform") == "host"}
    attempted = mx.get("platforms_attempted") or []
    unavail = mx.get("platforms_unavailable") or {}

    out = ['<div class="tiergrid head">'
           + "".join(f'<div class="tierhead"><span class="eyebrow">tier</span>'
                     f"<h4>{_esc(lab)}</h4><p>{desc}</p></div>"
                     for _, lab, desc in TIER_COLS)
           + "</div>"]

    for plat, title, mono, blurb, kind in PLATFORM_ROWS:
        leg = [c for c in cells if c.get("platform") == plat]
        if not leg and plat not in attempted:
            continue
        cannot_map, note_map = _common_notes(leg)
        common = dict(cannot_map)
        common.update(note_map)
        bits = ""
        if cannot_map:
            items = " · ".join(f"<b>{_esc(k.replace('_', ' '))}</b> — {_esc(v)}"
                               for k, v in sorted(cannot_map.items()))
            bits += (f'<p class="cannot"><span class="eyebrow">what this leg '
                     f"cannot measure</span>{items}</p>")
        if note_map:
            items = " · ".join(f"<b>{_esc(k.replace('_', ' '))}</b> — {_esc(v)}"
                               for k, v in sorted(note_map.items()))
            bits += (f'<p class="cannot"><span class="eyebrow">how this leg\'s '
                     f"numbers were obtained</span>{items}</p>")
        # A skip reason shared by every tier is a property of the leg, not of a
        # cell — say it once, loudly, instead of three times in three tiles.
        skipped = [c for c in leg if c.get("status") != "measured"]
        shared = None
        if skipped and len(skipped) == len(leg):
            rs = {c.get("reason") for c in skipped}
            if len(rs) == 1 and None not in rs:
                shared = rs.pop()
                bits += (f'<p class="cannot warn"><span class="eyebrow">not measured '
                         f"in this run — why</span>{_esc(shared)}</p>")
        if plat in unavail:
            bits += (f'<p class="cannot warn"><span class="eyebrow">platform '
                     f"unavailable</span>{_esc(unavail[plat])}</p>")
        if plat in cross:
            bits += (f'<p class="cannot"><span class="eyebrow">measured '
                     f"elsewhere in this ledger</span>{cross[plat]}</p>")
        cannot = bits
        tiles = "".join(
            _tile(tk, tl, sorted([c for c in leg if c.get("tier") == tk], key=_opt_rank),
                  kind, common, host_hash, shared)
            for tk, tl, _ in TIER_COLS)
        out.append(
            f'<section class="band"><div class="band-h"><h3>{_esc(title)}'
            f'<code>{_esc(mono)}</code></h3><p>{_esc(blurb)}</p></div>'
            f'{cannot}<div class="tiergrid">{tiles}</div></section>')
    return "".join(out)


def anchor_block(mx: dict) -> str:
    """The LVGL anchor is not a vyr tier and must never sit in the same grid row
    as one: it is a different codebase, built at its own opt-level, and (still)
    the one cell in this ledger whose fold is inside the timed window."""
    anchors = [c for c in mx.get("cells", []) if c.get("firmware", "vyr") != "vyr"]
    if not anchors:
        return ""
    tiles = []
    for c in anchors:
        m = c.get("metrics") or {}
        ro, tot, fold = (m.get("insns_per_frame_render_only"),
                         m.get("insns_per_frame_total"), m.get("harness_fold_insns"))
        share = (100.0 * fold / tot) if (fold and tot) else 0.0
        bt = c.get("build_type") or ""
        tiles.append(
            f'<article class="tile wide"><div class="tile-h">'
            f'<span class="tier">{_esc(str(c.get("firmware")).upper())}</span>'
            + _prov_chip(c) + "</div>"
            + f'<div class="hero">{_esc(_ins(ro))}</div>'
              f'<div class="hero-u">insns / frame · render only · '
              f"O<b>{_esc(_opt_short(c.get('opt_level')))}</b></div>"
            + '<div class="meter"><div class="meter-t">of the measured total, '
              f'<b>{share:.1f}%</b> is the benchmark hashing its own output</div>'
              f'<span class="mbar"><i class="warnfill" style="width:{share:.1f}%">'
              f"</i></span>"
              f'<div class="meter-l">{_esc(_ins(fold))} fold · '
              f"{_esc(_ins(tot))} total</div></div>"
            + (f'<p class="why"><b>Not like-for-like:</b> {_esc(bt)}</p>' if bt else "")
            + "</article>")
    return ('<section class="band"><div class="band-h"><h3>The anchor'
            '<code>third-party</code></h3><p>Measured by the same instrument, on '
            "the same emulated M4, at its own optimisation level — kept out of the "
            "vyr grid so a ratio can never be read off two tiles that were not built "
            "the same way.</p></div>"
            f'<div class="tiergrid">{"".join(tiles)}</div></section>')


def at_a_glance(mx: dict) -> str:
    """Four cross-cutting facts, none of which is legible in a per-cell table."""
    cells = mx.get("cells", [])
    vyr = [c for c in cells if c.get("firmware", "vyr") == "vyr"]
    measured = [c for c in cells if c.get("status") == "measured"]
    prov = {}
    for c in cells:
        key = (c.get("fold_provenance") or "").split(" (")[0].strip()
        prov[key] = prov.get(key, 0) + 1

    stats = []
    # 1. coverage
    stats.append((f"{len(measured)} / {len(cells)}", "cells measured in the latest run",
                  f"{len(cells) - len(measured)} carry a written reason instead of a "
                  "number — never a blank, never a zero."))
    # 2. fold share, vyr side
    shares = [100.0 * (c["metrics"]["harness_fold_insns"] / c["metrics"]["insns_per_frame_total"])
              for c in vyr
              if (c.get("metrics") or {}).get("harness_fold_insns") is not None
              and (c.get("metrics") or {}).get("insns_per_frame_total")]
    if shares:
        stats.append((f"{max(shares):.1f}%", "worst vyr cell that is the benchmark "
                                             "hashing itself",
                      "Error 4 was 36–55 % of a frame. #44 moved the fold out of the "
                      "timed window, so this is now zero by construction."))
    # 3. opt-level spread, worst tier
    spreads = {}
    for c in vyr:
        if c.get("platform") != "qemu-m4" or c.get("status") != "measured":
            continue
        v = (c.get("metrics") or {}).get("insns_per_frame_render_only")
        if isinstance(v, (int, float)):
            spreads.setdefault(c["tier"], []).append(v)
    worst = max(((t, max(v) / min(v)) for t, v in spreads.items() if len(v) > 1),
                key=lambda kv: kv[1], default=None)
    if worst:
        stats.append((f"{worst[1]:.2f}×", f"spread across opt-level on {worst[0].capitalize()}",
                      "One build flag, four numbers. The opt-level is a matrix axis, "
                      "not a decision to bake into a headline (#33)."))
    # 4. provenance rail
    by_build = prov.get("absent-from-window-by-build", 0)
    diff = prov.get("measured-differential", 0)
    sub = prov.get("derived-by-subtraction", 0)
    stats.append((f"{by_build} · {diff} · {sub}",
                  "fold provenance: by-build · differenced · carried-in",
                  "How a render-only figure was obtained is part of the figure. "
                  "Nothing on this page is derived by subtraction."))
    return ('<div class="stats">' + "".join(
        f'<div class="stat"><div class="n">{_esc(n)}</div>'
        f'<div class="l">{_esc(lab)}</div><p>{_esc(sub_)}</p></div>'
        for n, lab, sub_ in stats) + "</div>")


def grid_findings(mx: dict) -> str:
    """One computed observation the grid makes obvious and the table did not:
    whether the tier ranges OVERLAP once the opt-level axis is on screen."""
    rng = {}
    for c in mx.get("cells", []):
        if (c.get("platform") != "qemu-m4" or c.get("firmware", "vyr") != "vyr"
                or c.get("status") != "measured"):
            continue
        v = (c.get("metrics") or {}).get("insns_per_frame_render_only")
        if isinstance(v, (int, float)):
            rng.setdefault(c["tier"], []).append((v, _opt_short(c.get("opt_level"))))
    by_val = (lambda x: x[0])
    order = [(t, min(vs, key=by_val), max(vs, key=by_val))
             for t, vs in rng.items() if len(vs) > 1]
    order.sort(key=lambda x: -x[2][0])  # dearest tier first, by its dearest cell
    out = []
    for (ta, a_lo, _a_hi), (tb, _b_lo, b_hi) in zip(order, order[1:]):
        if a_lo[0] < b_hi[0]:
            out.append(
                f"<b>{ta.capitalize()} and {tb.capitalize()} overlap.</b> "
                f"{ta.capitalize()} at <code>O{_esc(a_lo[1])}</code> costs "
                f"{_esc(_ins(a_lo[0]))} insns/frame — <i>less</i> than "
                f"{tb.capitalize()} at <code>O{_esc(b_hi[1])}</code> "
                f"({_esc(_ins(b_hi[0]))}). Choosing the build flag moves this scene "
                f"further than choosing the quality tier does, so a tier-vs-tier "
                f"ratio quoted without its opt-level is not a fact.")
    if not out:
        return ""
    return ('<p class="finding"><span class="eyebrow">what the grid shows</span>'
            + " ".join(out) + "</p>")


PAGE_CSS_VARS_LIGHT = """
  color-scheme: light;
  --bg:#f9f9f7; --surface:#fcfcfb; --surface-2:#f3f2ef; --sunken:#eeede9;
  --ink:#0b0b0b; --ink-soft:#52514e; --ink-mut:#77756f;
  --line:#e1e0d9; --line-strong:#c3c2b7;
  --accent:#2a78d6; --accent-bg:#e8f0fc;
  --good:#0ca30c; --good-bg:#e7f4e7;
  --warn:#fab219; --warn-bg:#fbf0d8;
  --serious:#ec835a; --serious-bg:#fbeade;
  --crit:#d03b3b; --crit-bg:#f8e5e5;
  --shadow:0 1px 2px rgba(11,11,11,.05),0 8px 24px rgba(11,11,11,.045);
"""

PAGE_CSS_VARS_DARK = """
  color-scheme: dark;
  --bg:#0d0d0d; --surface:#1a1a19; --surface-2:#232322; --sunken:#111110;
  --ink:#ffffff; --ink-soft:#c3c2b7; --ink-mut:#898781;
  --line:#2c2c2a; --line-strong:#383835;
  --accent:#3987e5; --accent-bg:#152640;
  --good:#0ca30c; --good-bg:#132714;
  --warn:#fab219; --warn-bg:#2e2510;
  --serious:#ec835a; --serious-bg:#2e1e15;
  --crit:#d03b3b; --crit-bg:#2c1616;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35);
"""

PAGE_CSS_BODY = """
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--bg);color:var(--ink);overflow-x:hidden;
    font:16px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased;}
  .wrap{max-width:1180px;margin:0 auto;padding:clamp(24px,4vw,56px) clamp(16px,3vw,32px);}
  .eyebrow{display:block;font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;
    color:var(--ink-mut);font-weight:700;margin:0 0 8px;}
  h1{font-size:clamp(28px,5vw,42px);line-height:1.08;margin:0 0 14px;font-weight:700;
    letter-spacing:-.022em;text-wrap:balance;}
  h2{font-size:clamp(19px,2.6vw,24px);line-height:1.2;margin:0 0 6px;font-weight:700;
    letter-spacing:-.015em;}
  .lede{font-size:clamp(16px,2vw,18.5px);color:var(--ink-soft);margin:0 0 12px;
    max-width:72ch;text-wrap:pretty;}
  p{margin:0 0 12px} p:last-child{margin-bottom:0}
  a{color:var(--accent)} a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  code{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:.87em;
    background:color-mix(in srgb,var(--ink) 7%,transparent);padding:1px 5px;border-radius:5px;}
  section.chunk{margin:clamp(34px,5vw,56px) 0}
  section.chunk>h2+.lede{margin-bottom:18px}
  .rule{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-mut);
    font-weight:700;margin:0 0 14px;padding-bottom:8px;border-bottom:1px solid var(--line);}

  /* header */
  .top{display:flex;gap:16px;align-items:flex-start;justify-content:space-between}
  .meta{font-size:13px;color:var(--ink-soft);margin-top:12px}
  .meta b{color:var(--ink);font-variant-numeric:tabular-nums}
  .themetoggle{flex:none;background:var(--surface);color:var(--ink-soft);cursor:pointer;
    border:1px solid var(--line);border-radius:999px;padding:7px 14px;font:inherit;
    font-size:12.5px;font-weight:600;box-shadow:var(--shadow);}
  .themetoggle:hover{color:var(--ink);border-color:var(--line-strong)}
  .themetoggle:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

  /* stat row */
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:26px 0}
  .stat{background:var(--surface);border:1px solid var(--line);border-radius:12px;
    padding:16px 18px;box-shadow:var(--shadow)}
  .stat .n{font-size:clamp(22px,2.6vw,29px);font-weight:700;letter-spacing:-.02em;line-height:1.1}
  .stat .l{font-size:12.5px;color:var(--ink-soft);margin-top:4px;font-weight:600}
  .stat p{font-size:12.5px;color:var(--ink-mut);margin:8px 0 0;line-height:1.45}

  /* the grid */
  .tiergrid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;align-items:stretch}
  .tiergrid.head{margin:0 0 10px;gap:14px}
  .tierhead{padding:0 2px}
  .tierhead h4{margin:0 0 3px;font-size:15px;font-weight:700;letter-spacing:-.01em}
  .tierhead p{font-size:12.5px;color:var(--ink-mut);line-height:1.4;margin:0}
  .band{margin:0 0 26px}
  .band-h h3{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline;margin:0 0 4px;
    font-size:15px;font-weight:700;letter-spacing:.01em}
  .band-h h3 code{font-weight:600;color:var(--ink-mut)}
  .band-h p{font-size:13px;color:var(--ink-soft);margin:0 0 10px;max-width:80ch}
  .cannot{font-size:12.5px;color:var(--ink-mut);background:var(--surface-2);
    border:1px solid var(--line);border-radius:10px;padding:10px 13px;margin:0 0 12px;
    line-height:1.5}
  .cannot .eyebrow{margin-bottom:4px}
  .cannot b{color:var(--ink-soft)}
  .cannot.warn{background:var(--warn-bg);border-color:color-mix(in srgb,var(--warn) 35%,transparent)}

  .tile{background:var(--surface);border:1px solid var(--line);border-radius:14px;
    padding:15px 16px 13px;box-shadow:var(--shadow);display:flex;flex-direction:column;
    min-width:0}
  .tile.null{background:var(--surface-2);border-style:dashed;
    border-color:color-mix(in srgb,var(--warn) 45%,var(--line))}
  .tile.absent{background:transparent;border-style:dashed;box-shadow:none}
  .tile-h{display:flex;gap:8px;align-items:center;justify-content:space-between;
    margin-bottom:12px;min-height:22px}
  .tier{font-size:12px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
    color:var(--ink-soft)}
  .hero{font-size:clamp(26px,3.2vw,34px);font-weight:700;letter-spacing:-.025em;
    line-height:1.05}
  .hero.mono{font-size:clamp(15px,1.8vw,19px);font-family:ui-monospace,Menlo,monospace;
    letter-spacing:-.01em;word-break:break-all}
  .hero.none{font-size:clamp(17px,2vw,21px);color:var(--ink-mut);font-weight:600;
    letter-spacing:-.01em}
  .hero-u{font-size:12.5px;color:var(--ink-soft);margin-top:3px}
  .hero-u b{color:var(--ink);font-weight:700}
  .perpx{font-size:12.5px;color:var(--ink-mut);margin:10px 0 0}
  .why{font-size:12.5px;color:var(--ink-soft);line-height:1.5;margin:10px 0 0}
  .why b{color:var(--ink)}

  /* chips — the label is ink, the status is the dot. Colour never carries it alone. */
  .chip{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;font-weight:600;
    padding:3px 9px;border-radius:999px;white-space:nowrap;color:var(--ink);
    background:var(--surface-2);border:1px solid var(--line)}
  .chip i{width:7px;height:7px;border-radius:50%;background:var(--ink-mut);flex:none}
  .chip.good{background:var(--good-bg);border-color:color-mix(in srgb,var(--good) 35%,transparent)}
  .chip.good i{background:var(--good)}
  .chip.warn{background:var(--warn-bg);border-color:color-mix(in srgb,var(--warn) 40%,transparent)}
  .chip.warn i{background:var(--warn)}
  .chip.crit{background:var(--crit-bg);border-color:color-mix(in srgb,var(--crit) 40%,transparent)}
  .chip.crit i{background:var(--crit)}
  .chip.mut{color:var(--ink-soft)}

  /* opt-level strip */
  .optstrip{margin:13px 0 0;padding:11px 0 0;border-top:1px solid var(--line)}
  .opt-t{display:flex;justify-content:space-between;font-size:11px;font-weight:700;
    letter-spacing:.1em;text-transform:uppercase;color:var(--ink-mut);margin-bottom:8px}
  .opt-t span{letter-spacing:.02em;text-transform:none;font-weight:500}
  .orow{display:flex;align-items:center;gap:8px;margin:0 0 5px;font-size:12px}
  .ol{width:22px;flex:none;color:var(--ink-soft);font-weight:700;
    font-family:ui-monospace,Menlo,monospace}
  .obar{flex:1 1 auto;min-width:24px;height:8px;background:var(--sunken);border-radius:4px;
    overflow:hidden}
  .obar i{display:block;height:8px;border-radius:4px;background:var(--accent)}
  .ov{flex:none;font-variant-numeric:tabular-nums;color:var(--ink);font-weight:600;
    min-width:56px;text-align:right}
  .tag{flex:none;font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
    padding:2px 6px;border-radius:5px;background:var(--accent-bg);color:var(--ink)}
  .tag.best{background:var(--good-bg)}
  .opt-n{font-size:12px;color:var(--ink-mut);margin:9px 0 0;line-height:1.45}
  .opt-n b{color:var(--ink)}

  /* meter */
  .meter{margin:14px 0 0}
  .meter-t{font-size:12.5px;color:var(--ink-soft);margin-bottom:7px}
  .meter-t b{color:var(--ink);font-weight:700}
  .mbar{display:block;height:9px;background:var(--sunken);border-radius:5px;overflow:hidden}
  .mbar i{display:block;height:9px;border-radius:5px;background:var(--accent)}
  .mbar i.warnfill{background:var(--warn)}
  .meter-l{font-size:11.5px;color:var(--ink-mut);margin-top:6px;
    font-variant-numeric:tabular-nums}

  /* metric list */
  .ms{margin:13px 0 0;padding-top:11px;border-top:1px solid var(--line)}
  .m{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px;padding:3px 0;font-size:12.5px}
  .m .k{color:var(--ink-mut);flex:1 1 auto;min-width:0}
  .m .v{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums;text-align:right}
  .m.null .v{color:var(--ink-mut);font-weight:500}
  .m .why{flex:1 0 100%;font-size:11.5px;color:var(--ink-mut);margin:1px 0 4px;line-height:1.4}
  .tile-f{margin-top:auto;padding-top:11px;font-size:11.5px;color:var(--ink-mut)}
  .tile-f code{background:none;padding:0}
  .tile-f.ok::before,.tile-f.bad::before{content:"";display:inline-block;width:7px;height:7px;
    border-radius:50%;margin-right:6px;vertical-align:1px}
  .tile-f.ok::before{background:var(--good)}
  .tile-f.bad{color:var(--ink)}
  .tile-f.bad::before{background:var(--crit)}

  .finding{background:var(--accent-bg);border:1px solid color-mix(in srgb,var(--accent) 30%,transparent);
    border-radius:12px;padding:15px 17px;font-size:14px;line-height:1.55;
    color:var(--ink-soft);margin:6px 0 0;box-shadow:var(--shadow)}
  .finding b{color:var(--ink)}

  /* tables — kept, but never the page's own scrollbar */
  .tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px;
    box-shadow:var(--shadow);background:var(--surface);max-width:100%}
  table{border-collapse:collapse;width:100%;font-size:13px;
    font-variant-numeric:tabular-nums}
  th,td{text-align:right;padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
  th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-mut);
    font-weight:700;text-align:right;background:var(--surface-2);position:sticky;top:0}
  th:first-child,td:first-child{text-align:left}
  tbody tr:last-child td{border-bottom:none}
  td code{background:none;padding:0;color:var(--ink-soft)}
  details.tv{margin-top:14px}
  details.tv>summary{cursor:pointer;font-size:13px;font-weight:600;color:var(--ink-soft);
    padding:8px 0;list-style-position:inside}
  details.tv>summary:hover{color:var(--ink)}
  details.tv[open]>summary{margin-bottom:10px}

  /* charts */
  figure.card{margin:0;background:var(--surface);border:1px solid var(--line);
    border-radius:14px;padding:12px;box-shadow:var(--shadow);overflow:hidden}
  figure.card a{display:block;line-height:0}
  figure.card svg{width:100%;max-width:900px;height:auto;display:block;margin:0 auto}
  figcaption{color:var(--ink-soft);font-size:12.5px;line-height:1.5;margin-top:10px;
    padding:0 4px;max-width:80ch}
  .figs{display:grid;gap:16px}

  .foot{margin-top:52px;padding-top:18px;border-top:1px solid var(--line);
    font-size:12.5px;color:var(--ink-mut);line-height:1.6}

  @media (max-width:1000px){
    .stats{grid-template-columns:repeat(2,1fr)}
    .tiergrid{grid-template-columns:1fr}
    .tiergrid.head{display:none}
  }
  @media (max-width:560px){
    .stats{grid-template-columns:1fr}
  }
"""

THEME_JS = """
(function(){var r=document.documentElement,b=document.getElementById('themetoggle');
var s=null;try{s=localStorage.getItem('vyr-ledger-theme')}catch(e){}
function set(v){if(v){r.setAttribute('data-theme',v)}else{r.removeAttribute('data-theme')}
b.textContent=v==='dark'?'Theme: dark':v==='light'?'Theme: light':'Theme: auto';
try{v?localStorage.setItem('vyr-ledger-theme',v):localStorage.removeItem('vyr-ledger-theme')}
catch(e){}}
set(s==='dark'||s==='light'?s:null);
b.addEventListener('click',function(){var c=r.getAttribute('data-theme');
set(c===null?'light':c==='light'?'dark':null)});})();
"""


def page_css() -> str:
    return (":root{" + PAGE_CSS_VARS_LIGHT + "}\n"
            "@media (prefers-color-scheme:dark){:root:not([data-theme=\"light\"]){"
            + PAGE_CSS_VARS_DARK + "}}\n"
            ":root[data-theme=\"dark\"]{" + PAGE_CSS_VARS_DARK + "}\n"
            ":root[data-theme=\"light\"]{" + PAGE_CSS_VARS_LIGHT + "}\n"
            + PAGE_CSS_BODY)


def page_html(history: list[dict], figures: list[str]) -> str:
    blocks: list[str] = []

    mrow = _latest_with(history, "matrix")
    glance = grid = anchor = findings = mtable = ""
    if mrow:
        mx = mrow["matrix"]
        # A leg the CURRENT run could not take may still have a last-known
        # measurement further down this page. Say so on the tile band rather
        # than letting a reader conclude the board has never been run.
        cross = {}
        srow = _latest_with(history, "silicon")
        if srow:
            tiers = ", ".join(t.capitalize() for t, v in srow["silicon"]["tiers"].items()
                              if v.get("cycles_per_frame"))
            cross["board"] = (
                f'Last real-silicon run was <code>{_esc(srow["commit"])}</code> '
                f'({_esc((srow.get("ts") or "")[:10])}, {_esc(tiers)}) — see '
                '<a href="#silicon">Real silicon</a> below. It is a different row, '
                "not a back-fill of this one.")
        glance = at_a_glance(mx)
        grid = matrix_grid(mx, cross)
        anchor = anchor_block(mx)
        findings = grid_findings(mx)
        rows = []
        for c in mx["cells"]:
            m = c.get("metrics") or {}

            def n(v, fmt="{:,}"):
                return fmt.format(v) if isinstance(v, (int, float)) else "—"

            share = (100.0 * m["harness_fold_insns"] / m["insns_per_frame_total"]) \
                if m.get("harness_fold_insns") and m.get("insns_per_frame_total") else None
            rows.append([
                f"{c['platform']} / {c.get('firmware', 'vyr')} {c['tier']} / "
                f"O{_esc(_opt_short(c.get('opt_level')))}",
                _esc(c.get("status")),
                n(m.get("insns_per_frame_total")),
                n(m.get("harness_fold_insns")),
                f"<b>{n(m.get('insns_per_frame_render_only'))}</b>",
                f"{share:.1f}%" if share is not None else "—",
                _esc((c.get("fold_provenance") or "—").split(" (")[0]),
                n(m.get("cycles_per_frame")),
                n(m.get("ns_per_frame"), "{:,.0f}"),
                n(m.get("heap_peak_b")),
                n(m.get("stack_high_water_b")),
                n(m.get("flash_text_data_b")),
                f"<code>{_esc(m.get('frame_hash') or '—')}</code>",
            ])
        inst = next((c.get("provenance", {}).get("instrument") for c in mx["cells"]
                     if c.get("platform") == "qemu-m4"
                     and c.get("provenance", {}).get("instrument")), {})
        q = (inst.get("qemu") or {})
        counter = ""
        if q:
            counter = (f"Counter: {_esc(q.get('version'))} @ "
                       f"<code>{_esc((q.get('source_commit') or '')[:12])}</code>, plugin "
                       f"<code>{_esc(inst.get('plugin'))}</code> "
                       f"<code>{_esc(inst.get('plugin_args'))}</code>, machine "
                       f"<code>{_esc(inst.get('machine'))}</code>, "
                       f"<code>{_esc(inst.get('icount'))}</code>.")
        mtable = (
            '<details class="tv"><summary>Table view — every cell, every field, '
            "including the ones the tiles summarise</summary>"
            + _tbl(["platform / firmware tier / opt", "status", "insns total", "hash fold",
                    "insns RENDER ONLY", "fold share", "fold provenance", "cycles",
                    "ns/frame", "heap B", "stack B", "flash B", "frame hash"], rows)
            + f'<p class="lede" style="font-size:13px;margin-top:12px">{counter}</p>'
              "</details>")

    r = _latest_with(history, "ladder")
    if r:
        rows = [
            [_rung_key(g), f"{g['full_ns'] / 1e6:.3f}", f"{g['full_ns_px']:.2f}",
             f"{g['headroom_full_x']:.2f}×", f"{g['incr_ns'] / 1e6:.3f}",
             f"{g['incr_ns_dirty_px']:.2f}", f"{g['headroom_incr_x']:.2f}×",
             f"{g['dirty_pct']:.1f}%", f"{g['full_ns'] / g['incr_ns']:.1f}×"]
            for g in r["ladder"]["rungs"]
        ]
        extra = ""
        a = r.get("anim")
        if a:
            extra += (f"<p>Anim acceptance: <b>{a['frames']} frames @ {a['w']}×{a['h']}</b>, "
                      f"run hash <code>{_esc(a['run_hash'])}</code>, dirty "
                      f"{a['dirty_pct']:.1f}%/step, {a['ms_frame']:.2f} ms/frame "
                      "(host wall clock).</p>")
        m = r.get("arm")
        if m:
            verdict = m.get("cross_isa", "?")
            badge = "byte-identical" if verdict == "identical" else _esc(verdict)
            cls = "good" if verdict == "identical" else "crit"
            extra += (f"<p>Cross-ISA rung (qemu-arm-static, ARMv7): "
                      + _chip(cls, badge) + f" across {m['frames']} frames. "
                      "Emulated wall time is NON-target-indicative.</p>")
        blocks.append(
            '<section class="chunk"><h2>Host resolution ladder</h2>'
            f'<p class="lede"><code>{_esc(r["commit"])}</code> · {_esc(r["ts"])}. '
            "×60fps = headroom against the 16.67 ms budget (&gt;1 fits). The "
            "incremental path is where 60 fps @ 4K lives.</p>"
            + _tbl(["rung", "full ms", "ns/px", "×60fps", "incr ms", "ns/dirty-px",
                    "×60fps", "dirty", "speedup"], rows)
            + extra + "</section>")

    r = _latest_with(history, "silicon")
    if r:
        s = r["silicon"]
        # cycles/insn needs BOTH legs of the same commit. The silicon row and the
        # matrix row are usually different rows (the board is measured on demand),
        # so pair them by commit — including the commits a matrix row `covers`,
        # which compile byte-identically to it.
        cpi = dict(_get(r, "derived", "cycles_per_insn") or {})
        if not cpi:
            mrow2 = next((h for h in history
                          if h.get("matrix") and (h["commit"] == r["commit"]
                                                  or r["commit"] in (h.get("covers") or []))),
                         None)
            if mrow2:
                for tier, t in s["tiers"].items():
                    c = m4_cell(mrow2, tier)
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
            '<section class="chunk" id="silicon"><h2>Real silicon</h2>'
            f'<p class="lede"><code>{_esc(r["commit"])}</code> · '
            f'{_esc(s.get("board"))}, {_esc(s.get("timer"))}, '
            f"{c.get('sysclk_hz', 0) / 1e6:.0f} MHz ({_esc(c.get('source', ''))}), "
            f"{c.get('flash_wait_states', '?')} flash wait states, ART "
            f"prefetch/I/D {c.get('art_prefetch', '?')}/{c.get('art_icache', '?')}/"
            f"{c.get('art_dcache', '?')}. cycles/insn is the cost emulation cannot "
            "model — it needs both legs of one commit.</p>"
            + _tbl(["tier", "cycles/frame", "ms @180 MHz", "heap peak", "cycles/insn",
                    "spread", "frame hash"], rows) + "</section>")

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
            '<section class="chunk"><h2>Emulated M4 — stock QEMU</h2>'
            f'<p class="lede"><code>{_esc(r["commit"])}</code>. Deterministic '
            "quantities only. This vehicle's <code>SYS_CLOCK</code> reading is host "
            "wall time, not instructions (docs/performance.md §5); it is recorded but "
            "never charted.</p>"
            + _tbl(["tier", "heap peak", "Draft fast-path", "frame hash"], rows)
            + "</section>")

    size_row = _latest_with(history, "size")
    bench_row = _latest_with(history, "bench")
    if (size_row and size_row["size"].get("flash_kib")) or bench_row:
        pair = []
        if size_row and size_row["size"].get("flash_kib"):
            pair.append(
                f'<div><h2 class="rule">Static size · <code>'
                f'{_esc(size_row["commit"])}</code></h2>'
                + _tbl(["config", "flash KiB"],
                       [[k.replace("_", "+"), f"{v:.1f}"]
                        for k, v in size_row["size"]["flash_kib"].items()]) + "</div>")
        if bench_row:
            pair.append(
                f'<div><h2 class="rule">Host bench medians · <code>'
                f'{_esc(bench_row["commit"])}</code></h2>'
                + _tbl(["case", "ns/px"],
                       [[k, f"{v:.4g}"] for k, v in bench_row["bench"]["ns_px"].items()])
                + "</div>")
        blocks.append('<section class="chunk"><div class="tiergrid" '
                      'style="grid-template-columns:repeat(2,1fr)">'
                      + "".join(pair) + "</div></section>")

    # commits that could not be measured — recorded, never omitted
    skips = [x for x in load_all() if x.get("kind") == "skip"]
    if skips:
        blocks.append(
            '<section class="chunk"><h2>Commits that could not be measured</h2>'
            '<p class="lede">An honest hole beats a plausible interpolation.</p>'
            + _tbl(["commit", "date", "subject", "why"],
                   [[f"<code>{_esc(s['commit'])}</code>",
                     _esc((s.get('commit_date') or '')[:10]),
                     _esc(s.get("subject", "")), _esc(s.get("reason", ""))]
                    for s in skips]) + "</section>")

    # the coverage matrix: which row measured what — sparsity, stated
    hdr = ["run", "commit", "when"] + SECTIONS
    crows = []
    for i, row in enumerate(history):
        cells = [str(i), f"<code>{_esc(row['commit'])}{'*' if row.get('dirty') else ''}</code>",
                 f"<code>{_esc(row['ts'])}</code>"]
        cells += ["●" if row.get(s) else "·" for s in SECTIONS]
        crows.append(cells)
    coverage = _tbl(hdr, crows)

    charts = "\n".join(figures) or (
        '<p class="lede">No section yet has two observations — nothing is a trend, '
        "so nothing is charted.</p>"
    )

    n_rows = len(history)
    n_matrix = sum(1 for x in history if x.get("matrix"))
    n_cells = len((mrow or {}).get("matrix", {}).get("cells", []))
    latest = mrow or history[-1]

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>vyr measurement ledger</title>
<style>{page_css()}</style>
</head>
<body>
<div class="wrap">

<div class="top">
  <div>
    <span class="eyebrow">vyr · measurement ledger · schema {SCHEMA}</span>
    <h1>The matrix, drawn as a matrix</h1>
  </div>
  <button id="themetoggle" class="themetoggle" type="button">Theme: auto</button>
</div>
<p class="lede">One measurement history for this project
(<a href="https://github.com/awtoau/vyr/issues/25">#25</a>): the performance
<b>matrix</b> — platform × tier × opt-level — plus the host resolution ladder,
cross-ISA verdicts, static size and real F429 silicon cycles. One append-only
row per run in <a href="history.jsonl"><code>history.jsonl</code></a>; this page
is regenerated from it by <code>./dev.py track</code> and is never hand-edited.
Sections are independent and optional: a run records what it measured and
nothing else, so <b>rows are sparse where the measurement was never taken</b>.</p>
<p class="meta"><b>{n_rows}</b> rows · <b>{n_matrix}</b> carrying a matrix ·
latest <code>{_esc(latest['commit'])}</code>
{_esc(latest.get('subject', ''))} · <b>{n_cells}</b> cells in the current
matrix · provenance for every number in
<a href="https://github.com/awtoau/vyr/blob/main/docs/performance.md">docs/performance.md</a>.</p>

{glance}

<section class="chunk">
  <h2>The matrix</h2>
  <p class="lede">Rows are platform legs, columns are quality tiers, and each
  tile is one cell. <b><code>render_only</code> is the headline series</b> — the
  total includes the benchmark's own FNV hash fold over every output byte, which
  is not rendering and was 36–55 % of a cheap frame before
  <a href="https://github.com/awtoau/vyr/issues/44">#44</a>. Every tile carries a
  provenance chip saying <i>how</i> its render-only figure was obtained, because
  provenance being invisible is exactly how this project published a wrong number
  four times.</p>
  {findings}
</section>

{grid}
{anchor}
{mtable}

<section class="chunk">
  <h2>Why the ledger is a rebuild, not an append</h2>
  <p class="lede"><b>Schema 3 (2026-07-24).</b> Every M4 instruction figure here
  was produced by one instrument — <code>scripts/perf-harness.py</code> — replayed
  over history by <code>scripts/perf-replay.py</code>: old commits measured with
  today's tools, never with their own. The SYS_CLOCK-derived rows of the previous
  ledger were host wall time and are deleted rather than relabelled, and the
  benchmark's own hash fold is now a separate recorded field instead of being
  silently inside the headline. The four measurement errors that forced this are in
  <a href="https://github.com/awtoau/vyr/blob/main/docs/measurements/perf-history.md">perf-history.md</a>.</p>
</section>

{"".join(blocks)}

<section class="chunk">
  <h2>Trends</h2>
  <p class="lede">The grid above is the current state; these are the series. A
  series is charted only where it has two or more observations — a single
  observation stays in a table rather than being drawn as a one-point line.
  <b>A tier that did not exist yet simply starts later</b>
  (<code>Draft</code> from <code>5da42a2</code>, <code>Fast</code> from
  <code>cb29f52</code>) rather than being interpolated backwards or drawn as a gap
  in a line that pretends to span it.</p>
  <div class="figs">{charts}</div>
</section>

<section class="chunk">
  <h2>Coverage — what each run measured</h2>
  <p class="lede">A sparse matrix has to explain itself. ● recorded · nothing
  recorded. <code>*</code> = measured against a dirty worktree. Host wall-clock
  numbers are desktop-host numbers; emulated numbers are labelled where they are
  not target-indicative.</p>
  {coverage}
</section>

<p class="foot">Generated by <code>scripts/ledger.py</code> from
<code>docs/perf/history.jsonl</code> — one writer, one file, one page. Nothing on
this page is hand-written; to change a number, take a measurement.</p>

</div>
<script>{THEME_JS}</script>
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

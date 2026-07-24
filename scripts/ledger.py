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

THE PAGE IS THE DATA (2026-07-24)

    ``docs/perf/index.html`` is three flat, sortable tables and nothing else:
    every measurement cell one row per cell, every other recorded value one row
    per value, and one row per line of the file. The KPI tiles, the computed
    callouts, the platform/tier narrative bands, the status-coloured chips, the
    per-tile opt-level bars and the SVG trend charts are GONE — they were the
    page interpreting its own data, and interpretation belongs in an issue or a
    commit message. Numbers are stored as numbers and formatted on display, so
    they sort numerically; a ``null`` renders as an em dash carrying the written
    reason the harness recorded for it.

    Sorting comes from Tabulator, vendored (MIT) under ``docs/perf/vendor/``
    because the page is served from GitHub Pages AND opened as a local file and
    must fetch nothing at render time. Never a CDN.

    After changing anything about the page, run ``scripts/ledger-verify.py``:
    it re-checks that no measured value moved, that every value and every
    written reason is in the page, that the page is self-contained and
    theme-aware, and it SCREENSHOTS the page and counts the rendered rows. The
    tables are built in JavaScript, so a static read of the HTML cannot tell you
    whether anything actually rendered; only looking catches an empty div.

Usage: scripts/ledger.py [--regen-only]
       scripts/ledger.py --rebuild-from-replay tmp/perf-replay.jsonl
       scripts/ledger-verify.py            (check + render, after any change)
"""

from __future__ import annotations

import json
import platform
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


# --- the page: the data, flat and sortable ------------------------------------
#
# The page is THREE flat tables and nothing else. No KPI tiles, no computed
# callouts, no narrative bands, no status colour, no charts: those interpreted
# the data, and interpretation belongs in a commit message or an issue, not in
# the ledger's own view of itself. What is here is every field of every row of
# history.jsonl, sortable by any column.
#
# Sorting is Tabulator's (vendored, MIT — see docs/perf/vendor/README.md).
# Numbers are stored as numbers and formatted on display, so `9,220,422` sorts
# below `48,239,550` instead of above it, which is exactly what a lexical sort
# of pre-formatted strings would have got wrong.

VENDOR_DIR = LEDGER_DIR / "vendor"
TAB_VERSION = "6.5.2"
TAB_JS = "tabulator.min.js"
TAB_CSS = "tabulator.min.css"

SECTIONS = ["matrix", "ladder", "anim", "arm", "bench", "size", "m4_qemu",
            "silicon", "board_anim", "derived"]

# Row identity: carried on every ledger row, so it is the key of the `runs`
# table and is NOT repeated as a "section" in the long-form table.
IDENT_KEYS = ("schema", "kind", "ts", "commit", "commit_full", "commit_date",
              "subject", "dirty", "host", "cpu", "arch", "source", "build_key",
              "covers")

METRIC_FIELDS = ("insns_per_frame_total", "harness_fold_insns",
                 "insns_per_frame_render_only", "cycles_per_frame",
                 "ns_per_frame", "ns_per_px", "heap_peak_b",
                 "stack_high_water_b", "flash_text_data_b", "frame_hash")

PROV_SUB = ("elf_sha256", "deterministic", "repeat", "timed_frames",
            "timed_window_insns", "window_remainder", "cache_hit")

INST_KEYS = ("build", "runner", "cpu", "target", "qemu.binary", "qemu.version",
             "qemu.source", "qemu.source_commit", "qemu.source_commit_date",
             "qemu.built_by", "plugin", "plugin_args", "machine", "icount",
             "cargo_incremental")


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class Notes:
    """Written reasons, interned once. A null in this ledger always carries the
    reason the harness recorded for it; the page shows that reason on the cell
    rather than leaving the cell silently empty. Interning keeps the same eight
    sentences from being repeated two hundred times in the payload."""

    def __init__(self) -> None:
        self.seen: dict[str, int] = {}
        self.list: list[str] = []

    def idx(self, text):
        if not text:
            return None
        i = self.seen.get(text)
        if i is None:
            i = len(self.list)
            self.seen[text] = i
            self.list.append(text)
        return i


def _flatten(value, prefix: str = "") -> list[tuple[str, object]]:
    """Every leaf of a nested structure, as (dotted path, scalar).

    A list of scalars is joined into one leaf rather than exploded — the values
    are still there verbatim, and `axes.tier[0] = exact` in three rows says less
    than `axes.tier = exact, fast, draft` in one. A list of objects IS exploded,
    indexed, because those are separate records (ladder rungs, size configs)."""
    out: list[tuple[str, object]] = []
    if isinstance(value, dict):
        if not value:
            return [(prefix, "(empty)")]
        for k, v in value.items():
            out += _flatten(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(value, list):
        if not value:
            return [(prefix, "(empty)")]
        if all(not isinstance(x, (dict, list)) for x in value):
            out.append((prefix, ", ".join("null" if x is None else str(x) for x in value)))
        else:
            for i, v in enumerate(value):
                out += _flatten(v, f"{prefix}[{i}]")
    else:
        out.append((prefix, value))
    return out


def _ident(i: int, r: dict) -> dict:
    return {
        "run": i,
        "commit": r.get("commit"),
        "commit_full": r.get("commit_full"),
        "commit_date": r.get("commit_date"),
        "subject": r.get("subject"),
        "ts": r.get("ts"),
        "dirty": r.get("dirty"),
        "host": r.get("host"),
        "cpu": r.get("cpu"),
        "arch": r.get("arch"),
        "source": r.get("source"),
        "build_key": r.get("build_key"),
        "covers": ", ".join(r.get("covers") or []) or None,
    }


# --- table 1: one row per measurement cell -----------------------------------

def matrix_rows(history: list[dict], notes: Notes) -> list[dict]:
    rows = []
    for i, r in enumerate(history):
        mx = r.get("matrix")
        if not mx:
            continue
        ident = _ident(i, r)
        for c in mx.get("cells", []):
            d = dict(ident)
            seen = set(IDENT_KEYS)
            for k in ("platform", "firmware", "tier", "opt_level", "target", "profile",
                      "isa", "word_bits", "float", "build_type", "fold_provenance",
                      "status", "reason"):
                d[k] = c.get(k)
                seen.add(k)
            m = c.get("metrics") or {}
            mn = c.get("metric_notes") or {}
            for k in METRIC_FIELDS:
                d[k] = m.get(k)
                j = notes.idx(mn.get(k))
                if j is not None:
                    d[k + "_why"] = j
            ro = m.get("insns_per_frame_render_only")
            d["insns_per_px"] = round(ro / SCENE_PX, 4) if isinstance(ro, (int, float)) else None
            p = c.get("provenance") or {}
            d["prov.fold_share_of_total"] = p.get("fold_share_of_total")
            d["prov.fastpath_coverage_pct"] = p.get("fastpath_coverage_pct")
            for name in ("total", "render_only"):
                sub = p.get(name) or {}
                for k in PROV_SUB:
                    d[f"{name}.{k}"] = sub.get(k)
            inst = dict(_flatten(p["instrument"])) if p.get("instrument") else {}
            for k in INST_KEYS:
                d["inst." + k] = inst.get(k)
            # Anything this schema did not anticipate is still reachable: it is
            # dumped verbatim into `extra` rather than silently dropped.
            extra = {k: v for k, v in c.items()
                     if k not in seen and k not in ("metrics", "metric_notes", "provenance")}
            extra.update({k: v for k, v in p.items()
                          if k not in ("instrument", "total", "render_only",
                                       "fold_share_of_total", "fastpath_coverage_pct")})
            extra.update({k: v for k, v in inst.items() if k not in INST_KEYS})
            if extra:
                log(f"note: cell carries unmapped key(s) {sorted(extra)} — shown in `extra`")
            d["extra"] = json.dumps(extra, sort_keys=True, ensure_ascii=False) if extra else None
            rows.append(d)
    return rows


# --- table 2: everything that is not a matrix cell, one row per value --------

def other_rows(allrows: list[dict]) -> list[dict]:
    """Long form: (run, section, path, value). The ladder rungs, anim, arm,
    bench, size, m4_qemu, silicon, board_anim, derived and the matrix's own
    metadata all have different shapes, and forcing them into one wide table
    would invent columns. One row per recorded value invents nothing, and the
    `path` column is what you sort or filter on to get a series back."""
    out = []
    for i, r in enumerate(allrows):
        # Slim identity on purpose: one row per VALUE means 1500+ rows, and
        # restating the commit subject, cpu and build key on every one of them
        # would triple the page for nothing. `run` joins to the Runs table,
        # which carries the rest of the identity in full.
        ident = {
            "run": i,
            "kind": r.get("kind", "measurement"),
            "commit": r.get("commit"),
            "commit_date": r.get("commit_date"),
            "ts": r.get("ts"),
            "host": r.get("host"),
        }
        for key, val in r.items():
            if key in IDENT_KEYS:
                continue
            if key == "matrix":
                val = {k: v for k, v in val.items() if k != "cells"}
            for path, v in _flatten(val, key):
                sec, _, rest = path.partition(".")
                out.append({
                    **ident,
                    "section": sec,
                    "path": rest or sec,
                    "value": v,
                    "type": ("null" if v is None else
                             "boolean" if isinstance(v, bool) else
                             "number" if isinstance(v, (int, float)) else "string"),
                })
    return out


# --- table 3: one row per line of history.jsonl ------------------------------

def run_rows(allrows: list[dict]) -> list[dict]:
    rows = []
    for i, r in enumerate(allrows):
        d = _ident(i, r)
        d["kind"] = r.get("kind", "measurement")
        d["schema"] = r.get("schema")
        d["covers_n"] = len(r.get("covers") or [])
        d["cells"] = len((r.get("matrix") or {}).get("cells", []))
        d["measured_cells"] = sum(1 for c in (r.get("matrix") or {}).get("cells", [])
                                  if c.get("status") == "measured")
        for s in SECTIONS:
            d["has." + s] = 1 if r.get(s) else 0
        d["migrated_from"] = r.get("migrated_from")
        d["migration_note"] = r.get("migration_note")
        d["reason"] = r.get("reason")
        d["note"] = r.get("note")
        rows.append(d)
    return rows


# --- column specs -------------------------------------------------------------

def _c(f, t, k="text", d="", w=None, lst=False):
    c = {"f": f, "t": t, "k": k}
    if d:
        c["d"] = d
    if w:
        c["w"] = w
    if lst:
        c["list"] = True
    return c


IDENT_COLS = [
    _c("run", "run", "num", "position in history.jsonl — 0 is the oldest line", 100),
    _c("commit", "commit", "mono", "short commit the measurement was taken on", lst=True),
    _c("commit_full", "commit (full)", "mono"),
    _c("commit_date", "commit date", "mono", "author date of that commit"),
    _c("subject", "subject", "text", "commit subject"),
    _c("ts", "recorded", "mono", "timestamp on the ledger row"),
    _c("dirty", "dirty", "bool", "worktree was unclean when the row was written"),
    _c("host", "host", "text", "machine the harness ran on", lst=True),
    _c("cpu", "cpu"),
    _c("arch", "arch", "text", "", lst=True),
    _c("source", "row source", "text", "tool that produced this row"),
    _c("build_key", "build key", "mono", "identity of the build the replay measured"),
    _c("covers", "covers", "text",
       "other commits that compile byte-identically to this one, so this "
       "measurement stands for them too"),
]

# Column ORDER is not a ranking: the axes that identify a cell come first, then
# the measured numbers, then how they were obtained, then the rest of the run's
# identity. Drag any header to reorder, or hide columns from its ⋮ menu.
MATRIX_COLS = [
    IDENT_COLS[0],                                          # run
    IDENT_COLS[1],                                          # commit
    _c("platform", "platform", "text", "", lst=True),
    _c("firmware", "firmware", "text", "vyr, or the third-party anchor", lst=True),
    _c("tier", "tier", "text", "", lst=True),
    _c("opt_level", "opt level", "text", "", lst=True),
    _c("status", "status", "text", "", lst=True),

    _c("insns_per_frame_total", "insns total", "num",
       "instructions per frame including the benchmark's own hash fold"),
    _c("harness_fold_insns", "harness fold", "num",
       "instructions the benchmark spends hashing its own output"),
    _c("insns_per_frame_render_only", "insns render only", "num",
       "instructions per frame with the hash fold accounted out"),
    _c("insns_per_px", "insns/px", "num",
       "DERIVED on this page: insns render only / 129,600 px of the reference scene"),
    _c("cycles_per_frame", "cycles/frame", "num", "real CPU cycles, DWT_CYCCNT"),
    _c("ns_per_frame", "ns/frame", "num", "host wall clock"),
    _c("ns_per_px", "ns/px", "num", "host wall clock"),
    _c("heap_peak_b", "heap peak B", "num"),
    _c("stack_high_water_b", "stack high-water B", "num"),
    _c("flash_text_data_b", "flash text+data B", "num"),
    _c("frame_hash", "frame hash", "mono", "FNV-1a over the whole rendered frame"),

    _c("fold_provenance", "fold provenance", "text",
       "HOW the render-only figure was obtained", lst=True),
    _c("build_type", "build type", "text",
       "a perf build and a verifying build are different binaries"),
    _c("reason", "reason", "text", "why this cell was not measured"),
    _c("target", "target", "text", "", lst=True),
    _c("profile", "profile", "text", "cargo profile", lst=True),
    _c("isa", "isa"),
    _c("word_bits", "word bits", "num"),
    _c("float", "float", "text", "floating-point support of this platform"),
] + IDENT_COLS[2:] + [
    _c("prov.fold_share_of_total", "fold share of total", "num", "as recorded by the harness"),
    _c("prov.fastpath_coverage_pct", "fast-path coverage %", "num"),
    _c("total.elf_sha256", "total: ELF sha256", "mono"),
    _c("total.deterministic", "total: deterministic", "bool"),
    _c("total.repeat", "total: repeat", "num"),
    _c("total.timed_frames", "total: timed frames", "num"),
    _c("total.timed_window_insns", "total: window insns", "num"),
    _c("total.window_remainder", "total: window remainder", "num"),
    _c("total.cache_hit", "total: cache hit", "bool"),
    _c("render_only.elf_sha256", "render-only: ELF sha256", "mono"),
    _c("render_only.deterministic", "render-only: deterministic", "bool"),
    _c("render_only.repeat", "render-only: repeat", "num"),
    _c("render_only.timed_frames", "render-only: timed frames", "num"),
    _c("render_only.timed_window_insns", "render-only: window insns", "num"),
    _c("render_only.window_remainder", "render-only: window remainder", "num"),
    _c("render_only.cache_hit", "render-only: cache hit", "bool"),
] + [
    _c("inst." + k, "inst: " + k, "text") for k in INST_KEYS
] + [
    _c("extra", "extra", "mono", "any field this page has no column for, verbatim"),
]

OTHER_COLS = [
    _c("run", "run", "num", "position in history.jsonl", 100),
    _c("kind", "kind", "text", "", lst=True),
    _c("commit", "commit", "mono", "", lst=True),
    _c("commit_date", "commit date", "mono"),
    _c("ts", "recorded", "mono"),
    _c("host", "host", "text", "", lst=True),
    _c("section", "section", "text", "top-level key of the ledger row", lst=True),
    _c("path", "path", "mono", "dotted path to the value inside that key"),
    _c("value", "value", "mixed", "numbers sort numerically and before text"),
    _c("type", "type", "text", "", lst=True),
]

RUN_COLS = [
    _c("run", "run", "num", "position in history.jsonl", 82),
    _c("kind", "kind", "text", "", lst=True),
    _c("schema", "schema", "num"),
] + IDENT_COLS[1:] + [
    _c("covers_n", "covers n", "num"),
    _c("cells", "matrix cells", "num"),
    _c("measured_cells", "cells measured", "num"),
] + [
    _c("has." + s, "has " + s, "num", "1 = this run recorded that section")
    for s in SECTIONS
] + [
    _c("migrated_from", "migrated from", "text"),
    _c("migration_note", "migration note", "text"),
    _c("reason", "reason", "text", "why a commit could not be measured"),
    _c("note", "note", "text"),
]


# --- page ---------------------------------------------------------------------

PAGE_CSS_VARS_LIGHT = """
  color-scheme: light;
  --bg:#f9f9f7; --surface:#fcfcfb; --surface-2:#f3f2ef; --sunken:#eeede9;
  --ink:#0b0b0b; --ink-soft:#52514e; --ink-mut:#77756f;
  --line:#e1e0d9; --line-strong:#c3c2b7;
  --accent:#2a78d6;
  --shadow:0 1px 2px rgba(11,11,11,.05),0 8px 24px rgba(11,11,11,.045);
"""

PAGE_CSS_VARS_DARK = """
  color-scheme: dark;
  --bg:#0d0d0d; --surface:#1a1a19; --surface-2:#232322; --sunken:#111110;
  --ink:#ffffff; --ink-soft:#c3c2b7; --ink-mut:#898781;
  --line:#2c2c2a; --line-strong:#383835;
  --accent:#3987e5;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35);
"""

PAGE_CSS_BODY = """
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--bg);color:var(--ink);overflow-x:hidden;
    font:16px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased;}
  .wrap{max-width:1600px;margin:0 auto;padding:clamp(20px,3vw,40px) clamp(12px,2vw,24px);}
  .eyebrow{display:block;font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;
    color:var(--ink-mut);font-weight:700;margin:0 0 8px;}
  h1{font-size:clamp(24px,4vw,34px);line-height:1.1;margin:0 0 12px;font-weight:700;
    letter-spacing:-.02em;}
  h2{font-size:clamp(17px,2.2vw,21px);line-height:1.2;margin:0 0 4px;font-weight:700;
    letter-spacing:-.015em;}
  .lede{font-size:15px;color:var(--ink-soft);margin:0 0 10px;max-width:100ch;}
  .meta{font-size:13px;color:var(--ink-soft);margin:0 0 4px}
  .meta b{color:var(--ink);font-variant-numeric:tabular-nums}
  p{margin:0 0 10px} p:last-child{margin-bottom:0}
  a{color:var(--accent)} a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  code{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:.87em;
    background:color-mix(in srgb,var(--ink) 7%,transparent);padding:1px 5px;border-radius:5px;}
  section.chunk{margin:clamp(26px,3.5vw,40px) 0}
  .top{display:flex;gap:16px;align-items:flex-start;justify-content:space-between}
  .themetoggle{flex:none;background:var(--surface);color:var(--ink-soft);cursor:pointer;
    border:1px solid var(--line);border-radius:999px;padding:7px 14px;font:inherit;
    font-size:12.5px;font-weight:600;box-shadow:var(--shadow);}
  .themetoggle:hover{color:var(--ink);border-color:var(--line-strong)}
  .themetoggle:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

  /* table furniture */
  .tbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:10px 0}
  .tbar input{flex:0 1 340px;background:var(--surface);color:var(--ink);font:inherit;
    font-size:13px;border:1px solid var(--line-strong);border-radius:8px;padding:6px 10px}
  .tbar input:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
  .tbar button{background:var(--surface);color:var(--ink-soft);cursor:pointer;font:inherit;
    font-size:12.5px;font-weight:600;border:1px solid var(--line);border-radius:8px;
    padding:6px 12px}
  .tbar button:hover{color:var(--ink);border-color:var(--line-strong)}
  .tbar .count{font-size:12.5px;color:var(--ink-mut);font-variant-numeric:tabular-nums}
  .tablecard{overflow-x:auto;max-width:100%}
  .hint{font-size:12.5px;color:var(--ink-mut);margin:8px 0 0}
  .mono{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:12px}
  .nul{color:var(--ink-mut)}
  .why{border-bottom:1px dotted var(--line-strong);cursor:help}

  /* Tabulator (vendored, unmodified) recoloured to this page's own tokens.
     Selectors match the vendored sheet's specificity exactly — no !important. */
  .tabulator{background:var(--surface);border:1px solid var(--line);border-radius:10px;
    color:var(--ink);font:13px/1.4 ui-sans-serif,system-ui,-apple-system,sans-serif;
    font-variant-numeric:tabular-nums}
  .tabulator .tabulator-header{background:var(--surface-2);color:var(--ink-soft);
    border-bottom:1px solid var(--line-strong)}
  .tabulator .tabulator-header .tabulator-col{background:var(--surface-2);
    border-right:1px solid var(--line)}
  .tabulator .tabulator-header .tabulator-col .tabulator-col-content{padding:6px 8px}
  .tabulator .tabulator-header .tabulator-col .tabulator-col-content
    .tabulator-col-title{font-size:11.5px;letter-spacing:.03em}
  .tabulator .tabulator-header .tabulator-col .tabulator-col-content
    .tabulator-col-sorter .tabulator-arrow{border-bottom:6px solid var(--ink-mut)}
  .tabulator .tabulator-header .tabulator-col.tabulator-sortable[aria-sort=none]
    .tabulator-col-content .tabulator-col-sorter .tabulator-arrow{
    border-bottom:6px solid var(--ink-mut);border-top:none}
  .tabulator .tabulator-header .tabulator-col.tabulator-sortable[aria-sort=ascending]
    .tabulator-col-content .tabulator-col-sorter .tabulator-arrow{
    border-bottom:6px solid var(--ink);border-top:none}
  .tabulator .tabulator-header .tabulator-col.tabulator-sortable[aria-sort=descending]
    .tabulator-col-content .tabulator-col-sorter .tabulator-arrow{
    border-bottom:none;border-top:6px solid var(--ink)}
  .tabulator .tabulator-header .tabulator-col .tabulator-header-filter input,
  .tabulator .tabulator-header .tabulator-col .tabulator-header-filter select{
    background:var(--surface);color:var(--ink);border:1px solid var(--line-strong);
    border-radius:5px;font:inherit;font-size:12px;padding:2px 4px;width:100%}
  .tabulator .tabulator-tableholder{background:var(--surface)}
  .tabulator .tabulator-tableholder .tabulator-table{background:var(--surface);
    color:var(--ink)}
  .tabulator .tabulator-tableholder .tabulator-placeholder
    .tabulator-placeholder-contents{color:var(--ink-mut);font-size:14px}
  .tabulator .tabulator-footer{background:var(--surface-2);color:var(--ink-soft);
    border-top:1px solid var(--line)}
  .tabulator-row{background:var(--surface);border-bottom:1px solid var(--line)}
  .tabulator-row.tabulator-row-even{background:var(--surface-2)}
  .tabulator-row .tabulator-cell{border-right:1px solid var(--line);padding:4px 8px}
  .tabulator-popup-container{background:var(--surface);color:var(--ink);
    border:1px solid var(--line-strong);box-shadow:var(--shadow)}
  .tabulator-menu .tabulator-menu-item{color:var(--ink)}
  .tabulator-edit-list .tabulator-edit-list-item{color:var(--ink)}
  .tabulator-edit-list .tabulator-edit-list-placeholder{color:var(--ink-mut)}

  .foot{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
    font-size:12.5px;color:var(--ink-mut);line-height:1.6}
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

TABLE_JS = r"""
(function(){
  var D = JSON.parse(document.getElementById('ledger-data').textContent);
  var NOTES = D.notes || [];
  window.vyrLedgerTables = {};

  function esc(s){
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function isNull(v){ return v === null || v === undefined || v === ''; }
  function noteFor(cell){
    var i = cell.getData()[cell.getColumn().getField() + '_why'];
    return (i === null || i === undefined) ? null : NOTES[i];
  }
  /* A null is never blank: it renders as an em dash carrying the reason the
     ledger recorded for it, and says so when no reason was recorded. */
  function span(cell, text, cls, title){
    var why = noteFor(cell);
    var tip = why || title || '';
    var c = (cls || '') + (why ? ' why' : '');
    return '<span' + (c.trim() ? ' class="' + c.trim() + '"' : '')
      + (tip ? ' title="' + esc(tip) + '"' : '') + '>' + text + '</span>';
  }
  function nul(cell){
    return span(cell, '—', 'nul',
      'null — no reason recorded for this field in this row');
  }
  function num(v){ return v.toLocaleString('en-AU', {maximumFractionDigits: 6}); }

  function fmtNum(cell){
    var v = cell.getValue();
    if (isNull(v)) return nul(cell);
    return typeof v === 'number' ? span(cell, esc(num(v)))
                                 : span(cell, esc(v), '', String(v));
  }
  function fmtText(cell){
    var v = cell.getValue();
    return isNull(v) ? nul(cell) : span(cell, esc(v), '', String(v));
  }
  function fmtMono(cell){
    var v = cell.getValue();
    return isNull(v) ? nul(cell) : span(cell, esc(v), 'mono', String(v));
  }
  function fmtBool(cell){
    var v = cell.getValue();
    return isNull(v) ? nul(cell) : span(cell, v ? 'yes' : 'no');
  }
  function fmtMixed(cell){
    var v = cell.getValue();
    if (isNull(v)) return nul(cell);
    if (typeof v === 'number') return span(cell, esc(num(v)), 'mono', String(v));
    if (typeof v === 'boolean') return span(cell, v ? 'true' : 'false', 'mono');
    return span(cell, esc(v), 'mono', String(v));
  }
  /* One column holds every shape of recorded value, so numbers still sort as
     numbers (and ahead of text) instead of falling back to a lexical compare. */
  function mixedSort(a, b){
    var an = typeof a === 'number', bn = typeof b === 'number';
    if (an && bn) { return a - b; }
    if (an) { return -1; }
    if (bn) { return 1; }
    return String(isNull(a) ? '' : a).localeCompare(String(isNull(b) ? '' : b));
  }

  var KIND = {
    num:   {sorter: 'number',  align: 'right',  fmt: fmtNum},
    bool:  {sorter: 'boolean', align: 'center', fmt: fmtBool},
    mono:  {sorter: 'string',  align: 'left',   fmt: fmtMono},
    text:  {sorter: 'string',  align: 'left',   fmt: fmtText},
    mixed: {sorter: mixedSort, align: 'right',  fmt: fmtMixed}
  };

  function headerMenu(){
    var menu = [];
    this.getColumns().forEach(function(column){
      var label = document.createElement('span');
      label.textContent = (column.isVisible() ? '☑' : '☐') + '  '
        + column.getDefinition().title;
      menu.push({label: label, action: function(e){
        e.stopPropagation();
        column.toggle();
      }});
    });
    return menu;
  }

  function build(t){
    var cols = t.cols.map(function(c){
      var k = KIND[c.k] || KIND.text;
      var col = {title: c.t, field: c.f, sorter: k.sorter, formatter: k.fmt,
        hozAlign: k.align, headerHozAlign: k.align, headerTooltip: c.d || c.t,
        minWidth: 60, maxWidth: 360, headerMenu: headerMenu,
        /* a null is not a zero and must never sort as one */
        sorterParams: {alignEmptyValues: 'bottom'}};
      if (c.w) { col.width = c.w; }
      if (c.list) {
        col.headerFilter = 'list';
        col.headerFilterParams = {valuesLookup: 'active', clearable: true, sort: 'asc'};
        col.headerFilterFunc = '=';
      }
      return col;
    });
    var table = new Tabulator('#' + t.id, {
      data: t.rows,
      columns: cols,
      layout: 'fitDataFill',
      height: t.height,
      renderHorizontal: 'virtual',
      movableColumns: true,
      placeholder: 'no rows',
      headerSortElement: '<span class="tabulator-arrow"></span>'
    });
    /* scripts/ledger-verify.py drives these to prove, in a real browser, that
       rows rendered and that a numeric column sorts numerically. The tables are
       built in JS: a static read of the HTML cannot tell you either. */
    window.vyrLedgerTables[t.id] = table;
    var count = document.getElementById(t.id + '-count');
    function report(n){
      if (count) { count.textContent = num(n) + ' of ' + num(t.rows.length) + ' rows'; }
    }
    table.on('tableBuilt', function(){ report(t.rows.length); });
    table.on('dataFiltered', function(filters, rows){ report(rows.length); });
    var search = document.getElementById(t.id + '-search');
    if (search) {
      search.addEventListener('input', function(){
        var q = search.value.trim().toLowerCase();
        if (!q) { table.clearFilter(); return; }
        table.setFilter(function(row){
          for (var k in row) {
            if (k.slice(-4) === '_why') { continue; }
            var v = row[k];
            if (v !== null && v !== undefined
                && String(v).toLowerCase().indexOf(q) !== -1) { return true; }
          }
          return false;
        });
      });
    }
    var dl = document.getElementById(t.id + '-csv');
    if (dl) {
      dl.addEventListener('click', function(){ table.download('csv', t.id + '.csv'); });
    }
  }

  if (typeof Tabulator === 'undefined') {
    /* honest failure: a blank div is a bug, so say what is missing */
    Array.prototype.forEach.call(document.querySelectorAll('.tablecard'), function(n){
      n.textContent = 'vendor/tabulator.min.js did not load, so no table was built. '
        + 'The data itself is in docs/perf/history.jsonl.';
    });
    return;
  }
  D.tables.forEach(build);
})();
"""


def page_css() -> str:
    return (":root{" + PAGE_CSS_VARS_LIGHT + "}\n"
            "@media (prefers-color-scheme:dark){:root:not([data-theme=\"light\"]){"
            + PAGE_CSS_VARS_DARK + "}}\n"
            ":root[data-theme=\"dark\"]{" + PAGE_CSS_VARS_DARK + "}\n"
            ":root[data-theme=\"light\"]{" + PAGE_CSS_VARS_LIGHT + "}\n"
            + PAGE_CSS_BODY)


def _table_block(tid: str, heading: str, sub: str) -> str:
    return (f'<section class="chunk"><h2>{_esc(heading)}</h2>'
            f'<p class="lede">{sub}</p>'
            f'<div class="tbar">'
            f'<input id="{tid}-search" type="search" placeholder="search every column…" '
            f'aria-label="search {_esc(heading)}">'
            f'<button id="{tid}-csv" type="button">Download CSV</button>'
            f'<span class="count" id="{tid}-count"></span></div>'
            f'<div class="tablecard"><div id="{tid}"></div></div>'
            f'<p class="hint">Click a column header to sort it; click again to reverse. '
            f'Numeric columns sort numerically. Drag a header to reorder, and use its '
            f'⋮ menu to hide columns. A dotted-underlined value carries the reason '
            f'the ledger recorded for it — hover to read it.</p></section>')


def page_html(history: list[dict]) -> str:
    allrows = load_all()
    notes = Notes()
    # Every table indexes `run` into the SAME list — the raw lines of the file.
    # Indexing the cells against measurement-rows-only would have made `run 2`
    # mean two different rows in two tables sitting on one page.
    mrows = matrix_rows(allrows, notes)
    orows = other_rows(allrows)
    rrows = run_rows(allrows)
    payload = {
        "notes": notes.list,
        "tables": [
            {"id": "t-cells", "cols": MATRIX_COLS, "rows": mrows, "height": "640px"},
            {"id": "t-values", "cols": OTHER_COLS, "rows": orows, "height": "560px"},
            {"id": "t-runs", "cols": RUN_COLS, "rows": rrows, "height": "440px"},
        ],
    }
    # Two invariants, enforced here rather than discovered in a browser: no
    # column is declared twice, and NO RECORDED FIELD IS WITHOUT A COLUMN. The
    # second one is the whole point of this page — a value that reached the
    # payload but has no column would be data the table silently hides.
    for t in payload["tables"]:
        fields = [c["f"] for c in t["cols"]]
        dup = sorted({f for f in fields if fields.count(f) > 1})
        if dup:
            raise SystemExit(f"ledger: {t['id']} declares column(s) {dup} twice")
        orphan = sorted({k for r in t["rows"] for k in r
                         if not k.endswith("_why")} - set(fields))
        if orphan:
            raise SystemExit(f"ledger: {t['id']} carries field(s) {orphan} with no "
                             "column — every recorded value must be reachable")
    # allow_nan=False: a NaN in a ledger is a bug, and it must not become `NaN`
    # in a JSON blob that no parser will accept.
    blob = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).replace("</", "<\\/")

    n_cells = len(mrows)
    n_meas = sum(1 for r in mrows if r.get("status") == "measured")
    latest = history[-1]

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>vyr measurement ledger</title>
<link rel="stylesheet" href="vendor/{TAB_CSS}">
<style>{page_css()}</style>
</head>
<body>
<div class="wrap">

<div class="top">
  <div>
    <span class="eyebrow">vyr · measurement ledger · schema {SCHEMA}</span>
    <h1>The data</h1>
  </div>
  <button id="themetoggle" class="themetoggle" type="button">Theme: auto</button>
</div>
<p class="lede">Every value recorded in
<a href="history.jsonl"><code>history.jsonl</code></a>, as flat sortable tables:
the performance matrix cell by cell, every other recorded value one value per
row, and one row per line of the file. Nothing is summarised, ranked or
coloured here. Regenerated from the file by <code>./dev.py track</code>
(<code>scripts/ledger.py</code>) and never hand-edited; to change a number, take
a measurement.</p>
<p class="meta"><b>{len(allrows)}</b> lines in the ledger ·
<b>{len(rrows)}</b> runs · <b>{n_cells}</b> matrix cells
(<b>{n_meas}</b> measured) · <b>{len(orows)}</b> other recorded values ·
latest <code>{_esc(latest['commit'])}</code> {_esc(latest.get('subject', ''))} ·
how each number is produced:
<a href="https://github.com/awtoau/vyr/blob/main/docs/performance.md">docs/performance.md</a>.</p>

{_table_block("t-cells", "Matrix cells",
              "One row per measurement cell — platform × tier × opt-level "
              "× run — with every field the ledger carries for it, including "
              "the provenance of each figure and the written reason behind every "
              "null.")}

{_table_block("t-values", "Every other recorded value",
              "The host resolution ladder, anim and cross-ISA runs, bench medians, "
              "static size, stock-QEMU M4 quantities, real silicon, the projections "
              "the ledger derives and the matrix's own metadata. These sections have "
              "different shapes, so each recorded value is its own row: filter "
              "<code>section</code> or sort <code>path</code> to get a series back.")}

{_table_block("t-runs", "Runs",
              "One row per line of <code>history.jsonl</code>, including the schema "
              "note and any commit that could not be measured. <code>has …</code> "
              "is 1 where the run recorded that section — rows are sparse because "
              "a run records what it measured and nothing else.")}

<p class="foot">Generated by <code>scripts/ledger.py</code> from
<code>docs/perf/history.jsonl</code> — one writer, one file, one page. Table
rendering by <a href="https://tabulator.info/">Tabulator</a> {TAB_VERSION}, MIT,
vendored under <a href="vendor/README.md"><code>docs/perf/vendor/</code></a> so
this page fetches nothing when it is opened.</p>

</div>
<script src="vendor/{TAB_JS}"></script>
<script id="ledger-data" type="application/json">{blob}</script>
<script>{TABLE_JS}</script>
<script>{THEME_JS}</script>
</body>
</html>
"""


def regen(history: list[dict]) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    missing = [f for f in (TAB_JS, TAB_CSS) if not (VENDOR_DIR / f).exists()]
    if missing:
        raise SystemExit(
            f"ledger: vendored {', '.join(missing)} missing from "
            f"{VENDOR_DIR.relative_to(REPO)} — the page builds its tables from them and "
            "must never fall back to a CDN. See docs/perf/vendor/README.md.")
    # Derived artifacts have no legacy: the SVG charts this page used to draw are
    # not regenerated, so the files go rather than linger unreferenced.
    for stale in LEDGER_DIR.glob("*.svg"):
        stale.unlink()
        log(f"pruned stale chart {stale.relative_to(REPO)}")
    html = page_html(history)
    (LEDGER_DIR / "index.html").write_text(html)
    log(f"page → {(LEDGER_DIR / 'index.html').relative_to(REPO)} "
        f"({len(history)} run(s), {len(html):,} bytes)")


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

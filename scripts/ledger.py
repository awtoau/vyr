#!/usr/bin/env python3
"""ledger — THE vyr measurement ledger: one writer, one file, one page (#25).

Replaces the two parallel ledgers this repo used to keep (``docs/perf`` written
by ``perf-history.py`` and ``docs/metrics`` written by ``metrics-history.py``).
There is now exactly ONE canonical ledger:

    docs/perf/history.jsonl      append-only, committed, one row per run
    docs/perf/index.html         regenerated from it (+ SVG charts beside it)

Canonical entry point: ``./dev.py track``. Output timestamped → tmp/ledger.log.

ROW SCHEMA (``"schema": 2``) — a flat identity envelope plus independent,
OPTIONAL measurement sections. A row that lacks a section simply did not measure
it; sparse rows are honest, invented values are not. Nothing is ever
back-filled.

    ts, commit, dirty, host, cpu, arch      identity (dirty = unclean worktree)
    ladder    {measured_at, rungs[6]}       host resolution ladder 120→4K
    anim      {…, run_hash}                 host anim acceptance run
    arm       {…, cross_isa}                qemu-user cross-ISA verdict
    bench     {source, ns_px{…}}            committed vyr-bench medians
    size      {target, profile, flash_kib}  linked M4 ELF sizes
    m4_qemu   {machine, tiers{…}}           stock-qemu heap/hash/coverage ONLY
    insns     {tool, firmwares{…}}          EXACT insns (QEMU + libinsn plugin)
    silicon   {board, clock, tiers{…}}      real F429 cycles (DWT_CYCCNT)
    board_anim{…}                           dirty-rect animation cost model
    superseded{…}                           kept, labelled, never charted
    derived   {…}                           projections computed from the above

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
    tmp/qemu-insn-{vyr-exact,vyr-draft,lvgl}.json   scripts/qemu-insn.py
    tmp/lvgl-m4-result.json     scripts/lvgl-m4-bench/run.py (LVGL provenance)
    tmp/board-result.json       scripts/board-run.py     (real silicon)
    tmp/board-anim.json         scripts/board-anim.py    (SPI dirty-rect model)

Usage: scripts/ledger.py [--regen-only]
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
SCHEMA = 2

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
            "Deterministic here: frame hash, heap peak, Draft fast-path coverage. "
            "sys_clock_cs is HOST WALL TIME on a plugin-less qemu and is NOT an "
            "instruction count (docs/performance.md §5) — never charted, never "
            "multiplied into insns. Exact counts live in the `insns` section."
        ),
        "tiers": tiers,
    }


_INSN_FIRMWARES = {"vyr-exact": "vyr_exact", "vyr-draft": "vyr_draft", "lvgl": "lvgl"}


def _console_facts(runs: list[dict]) -> dict:
    """Pull the deterministic guest-console facts out of an insn run."""
    out: dict = {}
    text = "\n".join(runs[0].get("guest_console", [])) if runs else ""
    m = re.search(r"frame fnv1a=(0x[0-9a-f]{16})", text)
    if m:
        out["frame_hash"] = m.group(1)
    m = re.search(r"heap peak=(\d+) B", text)
    if m:
        out["heap_peak_b"] = int(m.group(1))
    m = re.search(r"F16 fast-path: \d+ / \d+ delivered px \(([\d.]+)%\)", text)
    if m:
        out["fastpath_coverage_pct"] = float(m.group(1))
    return out


def sec_insns() -> dict | None:
    firmwares: dict = {}
    tool = None
    newest = None
    for name, key in _INSN_FIRMWARES.items():
        p = TMP / f"qemu-insn-{name}.json"
        d = _read(p)
        if not d or not d.get("insns_per_frame"):
            continue
        newest = max(newest or "", d.get("timestamp") or _mtime_iso(p))
        tool = tool or {
            "qemu": d.get("qemu", {}).get("version"),
            "qemu_source_commit": d.get("qemu", {}).get("source_commit"),
            "qemu_built_by": d.get("qemu", {}).get("built_by"),
            "plugin": "tests/tcg/plugins/libinsn.so",
            "plugin_args": d.get("plugin_args"),
            "icount": d.get("icount"),
            "machine": d.get("machine"),
        }
        fw = {
            "insns_per_frame": d["insns_per_frame"],
            "insn_px": round(d["insns_per_frame"] / SCENE_PX, 1),
            "timed_frames": d.get("timed_frames"),
            "timed_window_insns": d.get("timed_window_insns"),
            "elf_sha256": d.get("elf_sha256"),
            "repeat": d.get("repeat"),
            "deterministic": d.get("deterministic"),
            "measured_at": d.get("timestamp"),
        }
        fw.update(_console_facts(d.get("runs", [])))
        firmwares[key] = fw
    if not firmwares:
        return None
    # The LVGL anchor is deliberately unpinned upstream — record WHICH commit
    # it was built from, or the number is not an anchor (performance.md §6).
    lv = _read(TMP / "lvgl-m4-result.json")
    if lv and "lvgl" in firmwares and lv.get("lvgl"):
        u = lv["lvgl"]
        firmwares["lvgl"]["upstream"] = {
            k: u[k]
            for k in ("remote", "commit", "short", "version", "date", "dirty", "ahead_of_origin")
            if k in u
        }
    if "lvgl" in firmwares and "vyr_draft" in firmwares:
        firmwares["vyr_draft"]["vs_lvgl_x"] = round(
            firmwares["vyr_draft"]["insns_per_frame"] / firmwares["lvgl"]["insns_per_frame"], 4
        )
    return {
        "what": "architectural instruction counts, exact (QEMU + libinsn TCG plugin)",
        "scene": {"w": 480, "h": 270, "band_h": 16},
        "measured_at": newest,
        "tool": tool,
        "firmwares": firmwares,
    }


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


def derived(row: dict) -> dict:
    """Projections computed from the sections present — never a new measurement."""
    out: dict = {}
    for key, (width, field) in BRIDGE.items():
        for rung in (row.get("ladder") or {}).get("rungs", []):
            if rung.get("w") == width and rung.get(field) is not None:
                out[key] = round(rung[field], 4)
                break
    ins = (row.get("insns") or {}).get("firmwares", {})
    sil = (row.get("silicon") or {}).get("tiers", {})
    cpi = {}
    for tier, fwk in (("exact", "vyr_exact"), ("draft", "vyr_draft")):
        i = ins.get(fwk, {}).get("insns_per_frame")
        c = sil.get(tier, {}).get("cycles_per_frame")
        if i and c:
            cpi[tier] = round(c / i, 3)
    if cpi:
        out["cycles_per_insn"] = cpi
    return out


def build_row() -> dict:
    row: dict = {
        "schema": SCHEMA,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "commit": _git("rev-parse", "--short", "HEAD") or "unknown",
        "dirty": bool(_git("status", "--porcelain")),
        "host": platform.node(),
        "cpu": _cpu_model(),
        "arch": platform.machine(),
    }
    for key, fn in (
        ("ladder", sec_ladder),
        ("anim", lambda: sec_passthrough("rig-anim-stats.json")),
        ("arm", lambda: sec_passthrough("rig-arm.json")),
        ("bench", sec_bench),
        ("size", sec_size),
        ("m4_qemu", sec_m4_qemu),
        ("insns", sec_insns),
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


def load_history() -> list[dict]:
    if not HISTORY.exists():
        return []
    rows = [json.loads(ln) for ln in HISTORY.read_text().splitlines() if ln.strip()]
    bad = [r.get("commit") for r in rows if r.get("schema") != SCHEMA]
    if bad:
        raise SystemExit(
            f"ledger: rows {bad} are not schema {SCHEMA}. The ledger has ONE format; "
            "there is no read-old-format path. Rewrite the file or start a new one."
        )
    return rows


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
            p.append(f'<polyline class="{cls}" points="{coords}" fill="none" stroke-width="2"/>')
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

SECTIONS = ["ladder", "anim", "arm", "bench", "size", "m4_qemu", "insns",
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

    figure("Exact instructions per frame, 480×270 (QEMU + libinsn)", "insns / frame",
           [(lab, [(i, _get(r, "insns", "firmwares", k, "insns_per_frame"))
                   for i, r in enumerate(history)
                   if _get(r, "insns", "firmwares", k, "insns_per_frame")])
            for lab, k in (("vyr Exact", "vyr_exact"), ("vyr Draft", "vyr_draft"),
                           ("LVGL anchor", "lvgl"))],
           "insns-per-frame.svg", "{:.3g}")
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
    figure("M4 workload heap peak (emulated)", "KiB",
           [(lab, [(i, _heap_kib(r, k)) for i, r in enumerate(history)
                   if _heap_kib(r, k) is not None])
            for lab, k in (("Draft", "draft"), ("Exact", "exact"))],
           "mem-heap.svg", "{:.0f}")
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

    r = _latest_with(history, "insns")
    if r:
        ins = r["insns"]["firmwares"]
        base = ins.get("lvgl", {}).get("insns_per_frame")
        rows = []
        for lab, k in (("vyr Exact", "vyr_exact"), ("vyr Draft", "vyr_draft"),
                       ("LVGL anchor", "lvgl")):
            fw = ins.get(k)
            if not fw:
                continue
            up = fw.get("upstream", {})
            src = f"<code>{_esc(up['short'])}</code> ({_esc(up.get('version', ''))})" if up else \
                f"<code>{_esc((fw.get('elf_sha256') or '')[:12])}</code>"
            rows.append([lab, f"{fw['insns_per_frame']:,}", f"{fw['insn_px']:.1f}",
                         f"{fw['insns_per_frame'] / base:.4f}×" if base else "—",
                         f"{fw.get('timed_frames', '—')}", src])
        t = r["insns"]["tool"]
        blocks.append(
            f"<h2>Exact instruction counts — <code>{_esc(r['commit'])}</code></h2>"
            + _tbl(["firmware", "insns/frame", "insn/px", "vs LVGL", "frames",
                    "elf / upstream"], rows)
            + f'<p class="lede">{_esc(t.get("qemu"))}, built with '
              f"<code>--enable-plugins --enable-capstone</code> from "
              f"<code>{_esc((t.get('qemu_source_commit') or '')[:12])}</code>; plugin "
              f"<code>{_esc(t.get('plugin'))}</code> <code>{_esc(t.get('plugin_args'))}</code>, "
              f"machine <code>{_esc(t.get('machine'))}</code>. Not an equal-output "
              "comparison — Draft has no anti-aliasing "
              '(<a href="https://github.com/awtoau/vyr/issues/27">#27</a>).</p>'
        )

    r = _latest_with(history, "silicon")
    if r:
        s = r["silicon"]
        cpi = _get(r, "derived", "cycles_per_insn") or {}
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
(<a href="https://github.com/awtoau/vyr/issues/25">#25</a>): host resolution
ladder and anim acceptance, cross-ISA verdicts, static size, exact M4
instruction counts (QEMU + <code>libinsn</code>), and real F429 silicon cycles —
one append-only row per run in
<a href="history.jsonl"><code>history.jsonl</code></a>, this page regenerated
from it by <code>./dev.py track</code>. Sections are independent and optional:
a run records what it measured and nothing else. <b>Rows are sparse where the
measurement was never taken</b> — see the coverage matrix. Provenance for every
number: <a href="https://github.com/awtoau/vyr/blob/main/docs/performance.md">docs/performance.md</a>.</p>

{"".join(blocks)}

<h2>Trends</h2>
<p class="lede">A series is charted only where it has two or more observations;
single observations stay in the tables above rather than being drawn as a
one-point line.</p>
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
    if rest:
        log(f"ERROR: unknown args: {rest}")
        return 2
    if not regen_only and append_row() is None:
        return 1
    history = load_history()
    if not history:
        log(f"ERROR: {HISTORY.relative_to(REPO)} is empty — nothing to chart")
        return 1
    regen(history)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

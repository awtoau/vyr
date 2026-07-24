#!/usr/bin/env python3
"""board-present.py — MEASURE both presentation modes on the real
STM32F429I-DISC1 (#45): render DIRECT into the SDRAM framebuffer versus render
into a small buffer and flush.

`docs/plan.md` §"Presentation modes" lists three display topologies the single
entry point `render(tree, area, buf, stride)` is meant to serve. Only one of
them (partial + flush) had ever been measured. This runner drives the firmware
that measures the other resident-framebuffer one against it, same scene, same
tier, same clock, DWT_CYCCNT, render cost and blit cost reported SEPARATELY:

  * full-frame and dirty-rect updates in each mode,
  * all three quality tiers (Exact / Fast / Draft) in ONE flash -- `Quality` is
    a runtime argument, so no per-tier rebuild is needed,
  * achieved SDRAM bandwidth per mode, plus standalone memory probes (CCM vs
    SDRAM, with LTDC scanning RGB565 / RGB888 / not scanning at all),
  * blanking-vs-active-scan-out write cost, and LTDC's own FIFO-underrun flag,
  * and the correctness proof: both modes must produce IDENTICAL pixels.

What it CANNOT check: whether the picture is right on the glass. Only a human
looking at the board can settle that; the summary says which claims are
machine-verified and which need eyes.

SHARED HARDWARE: another agent may be driving the same board. Every probe-rs
invocation is wrapped in an exclusive lock (atomic mkdir of
/mnt/2tb/git/vyr/tmp/.board.lock -- the PRIMARY checkout's tmp/, so that a
linked worktree does not silently take a private lock) held for exactly one
flash+run and released in a finally:.

Usage:
  python3 scripts/board-present.py                # build + flash + run + report
  python3 scripts/board-present.py --verify-plain # ALSO re-run plain `board`
                                                  # and assert hash + cycles

Output: tmp/board-present.json, semihosting in tmp/board-present-run.log,
log tmp/board-present.log.
"""
import argparse
import datetime
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TMP = REPO / "tmp"

CHIP = "STM32F429ZI"
# THIS probe only. A second F42x board (0483:374e:...) is attached to the same
# workstation and must never be driven from this repo.
PROBE = "0483:3752:0671FF484971754867174427"

TARGET = "thumbv7em-none-eabihf"
PROFILE = "release-mcu"
ELF = REPO / "target" / TARGET / PROFILE / "vyr-size"

# Python-side wall-clock guard (never a shell timeout, never `sleep`). The
# `present` build adds, on top of the ltdc leg's ~20 s: 3 tiers x (4 full-frame
# reps x 2 modes + 8 dirty reps x 2 modes x 2 rect sets) plus framebuffer
# read-back hashes. Exact full-frame is the expensive cell at ~0.4 s a
# repetition. 600 s is several times a healthy run and still kills a target
# wedged in a bring-up spin.
DEADLINE_S = 600

sys.path.insert(0, str(HERE))
from board_lock import BoardLock, LOCK  # noqa: E402

AGENT = "board-present"

# The cross-ISA reference for the 480x270 measurement frame. The presentation
# comparison runs BEFORE that window opens and must not move either number.
REFERENCE_HASH = {
    "Exact": "0x24dcaff531c6eb01",
    "Fast": "0x930d03610b07ea6f",
    "Draft": "0xf98cbbdddd6da1ba",
}
# Board cycles/frame at Exact for the 480x270 measurement frame, MEASURED on
# this board 2026-07-24 at `5482dad`. Reported as a delta, never asserted to the
# cycle: DWT counts real silicon and a few hundred cycles of bus-contention
# drift is not a regression.
#
# docs/performance.md §4.1 still quotes 112,328,558. That figure predates
# `5482dad`, which took the FNV frame fold OUT of the timed window; the window
# now measures rendering and not rendering-plus-hashing, which is why this is
# 24 % lower. Nothing got faster.
REFERENCE_CYCLES = {"Exact": 85_307_608}
# The 240x320 PANEL_IR scene hash, folded from the RGB888 bytes vyr-core
# produces BEFORE any display-format conversion -- a property of the RENDERER,
# so the SPI-to-GRAM path, the LTDC banded path and the new DIRECT path must
# all agree.
PANEL_SCENE_HASH = {"Exact": "0xc8a77478f7f9055a", "Draft": "0x8af0208ab4cbd221"}


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg, logf):
    line = f"[{now()}] {msg}"
    print(line)
    logf.write(line + "\n")
    logf.flush()


def build(feats, tag, logf):
    """Build a feature set and SNAPSHOT the ELF.

    cargo writes every feature combination of this bin to the same path, so a
    concurrent build can silently swap the image between build and flash.
    Snapshotting makes every run provably flash the bytes this invocation
    built.
    """
    cmd = [
        "cargo", "build", "-p", "vyr-size",
        "--target", TARGET, "--profile", PROFILE,
        "--no-default-features", "--features", feats,
    ]
    log(f"build [{tag}]: {' '.join(cmd)}", logf)
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    logf.write(r.stdout + r.stderr + "\n")
    logf.flush()
    if r.returncode != 0:
        log(f"BUILD FAILED [{tag}] — see log", logf)
        return None
    snap = TMP / f"board-{tag}.elf"
    shutil.copyfile(ELF, snap)
    digest = hashlib.sha256(snap.read_bytes()).hexdigest()
    log(f"snapshot {snap} (sha256:{digest[:16]})", logf)
    return str(snap), digest


def run_once(elf, semilog, logf):
    """One flash+run. THE ONLY place the board is touched — lock held here and
    nowhere else, for exactly as long as probe-rs is running."""
    args = [
        "probe-rs", "run",
        "--chip", CHIP,
        "--probe", PROBE,
        "--catch-hardfault",
        "--non-interactive",
        "--disable-progressbars",
        elf,
    ]
    log("probe-rs: " + " ".join(args), logf)
    log(f"board lock path: {LOCK}", logf)
    t0 = time.monotonic()
    lock = BoardLock(AGENT, lambda m: log(m, logf))
    try:
        lock.__enter__()
        try:
            p = subprocess.run(args, cwd=REPO, capture_output=True, text=True,
                               timeout=DEADLINE_S)
            out, rc = p.stdout + p.stderr, p.returncode
        except subprocess.TimeoutExpired as e:
            def dec(x):
                x = x or b""
                return x.decode(errors="replace") if isinstance(x, bytes) else x
            out, rc = dec(e.stdout) + dec(e.stderr), None
            log(f"ERROR: target did not finish inside the {DEADLINE_S}s guard "
                f"— killed; partial output in {semilog}", logf)
    finally:
        lock.__exit__(None, None, None)
    Path(semilog).write_text(out)
    log(f"probe-rs wall {time.monotonic() - t0:.1f}s rc={rc}; "
        f"semihosting -> {semilog}", logf)
    return out


# --- parsing -----------------------------------------------------------------

ROW_RE = re.compile(
    r"present: mode=(?P<mode>\w+) tier=(?P<tier>\w+) rect=(?P<rect>\w+) "
    r"area_px=(?P<area_px>\d+) render=(?P<render>\d+) c blit=(?P<blit>\d+) c "
    r"total=(?P<total>\d+) c px_written=(?P<px>\d+) hash=(?P<hash>0x[0-9a-f]+) "
    r"sdram_bytes=(?P<sdram_bytes>\d+) sdram_kbps=(?P<sdram_kbps>\d+) "
    r"ns_per_px=(?P<ns_per_px>\d+)")

VERDICT_RE = re.compile(
    r"present: verdict tier=(?P<tier>\w+) rect=(?P<rect>\w+) "
    r"banded_total=(?P<banded>\d+) c direct_total=(?P<direct>\d+) c "
    r"winner=(?P<winner>\w+) margin_pct=(?P<margin>-?\d+) "
    r"pixels_equal=(?P<equal>\w+) banded_hash=(?P<bhash>0x[0-9a-f]+) "
    r"direct_hash=(?P<dhash>0x[0-9a-f]+)")

ISR_RE = re.compile(
    r"present: ltdc_isr tier=(?P<tier>\w+) rect=(?P<rect>\w+) "
    r"raw=(?P<raw>0x[0-9a-f]+) fifo_underrun=(?P<fuif>\w+) "
    r"transfer_error=(?P<terr>\w+)")

INTS = ("area_px", "render", "blit", "total", "px", "sdram_bytes",
        "sdram_kbps", "ns_per_px", "banded", "direct", "margin")


def _cast(d):
    return {k: (int(v) if k in INTS else v) for k, v in d.items()}


def parse(text):
    def g(pat, cast=str):
        m = re.search(pat, text)
        return cast(m.group(1)) if m else None

    r = {
        "is_board_image": "REAL SILICON" in text,
        "sysclk_hz": g(r"sysclk_hz=(\d+)", int),
        "on_pll": g(r"on_pll=(\w+)") == "true",
        "latency": g(r"latency=(\d+)", int),
        "prften": g(r"prften=(\d+)", int),
        "icen": g(r"icen=(\d+)", int),
        "dcen": g(r"dcen=(\d+)", int),

        # --- the ltdc leg this builds on -----------------------------------
        "sdram_ok": g(r"sdram test .*ok=(\w+)") == "true",
        "sdram_write_mbps": g(r"write=\d+ c \((\d+) MB/s\)", int),
        "sdram_read_mbps": g(r"read=\d+ c \((\d+) MB/s\)", int),
        "scanout_live": "scan-out LIVE" in text,
        "refresh_mhz_measured": g(r"= (\d+) mHz refresh", int),
        "ltdc_frame_hash": g(r"ltdc: panel frame fnv1a=(0x[0-9a-f]{16})"),
        "ltdc_full_render": g(r"full-frame cost render=(\d+) c", int),
        "ltdc_full_blit": g(r"full-frame cost render=\d+ c blit=(\d+) c", int),
        "ltdc_error": g(r"ERROR \[vyr-size\] ltdc scene failed: (.+)"),

        # --- #45 -------------------------------------------------------------
        "fb888_announced": g(r"present: RGB888 framebuffer at (0x[0-9a-f]{8})"),
        "dirty_n": g(r"present: dirty_rects\(prev,next\) n=(\d+)", int),
        "dirty_px": g(r"dirty_rects\(prev,next\) n=\d+ px=(\d+)", int),
        "dirty_pct": g(r"px=\d+ \((\d+)% of screen\)", int),
        "dirty_diff_cycles": g(r"of screen\) diff=(\d+) c", int),
        "dirty_rect_list": g(r"of screen\) diff=\d+ c rects=(.*)"),
        "pixels_identical": g(r"pixels_identical_across_modes=(\w+)") == "true",

        # --- memory probes ---------------------------------------------------
        "bw_burst_bytes": g(r"present: bw burst=(\d+) B", int),
        "ccm_w_cycles": g(r"ccm_w=(\d+) c", int),
        "ccm_w_mbps": g(r"ccm_w=\d+ c \((\d+) MB/s\)", int),
        "ccm_r_mbps": g(r"ccm_r=\d+ c \((\d+) MB/s\)", int),
        "sdram_w_rgb565scan_cycles": g(r"sdram_w_rgb565scan=(\d+) c", int),
        "sdram_w_rgb565scan_mbps": g(r"sdram_w_rgb565scan=\d+ c \((\d+) MB/s\)", int),
        "sdram_r_rgb565scan_mbps": g(r"sdram_r_rgb565scan=\d+ c \((\d+) MB/s\)", int),
        "sdram_w_rgb888scan_mbps": g(r"sdram_w_rgb888scan=\d+ c \((\d+) MB/s\)", int),
        "sdram_r_rgb888scan_mbps": g(r"sdram_r_rgb888scan=\d+ c \((\d+) MB/s\)", int),
        "sdram_w_layeroff_mbps": g(r"sdram_w_layeroff=\d+ c \((\d+) MB/s\)", int),
        "sdram_r_layeroff_mbps": g(r"sdram_r_layeroff=\d+ c \((\d+) MB/s\)", int),
        "ccm_vs_sdram": g(r"contention ccm_vs_sdram=(\d+)x", int),
        "scanout_cost_rgb565_pct": g(r"scanout_cost_rgb565_pct=(-?\d+)", int),
        "scanout_cost_rgb888_pct": g(r"scanout_cost_rgb888_pct=(-?\d+)", int),
        "phase_reps": g(r"present: phase reps=(\d+)", int),
        "phase_blank_min": g(r"blank_min=(\d+) c", int),
        "phase_blank_mean": g(r"blank_mean=(\d+) c", int),
        "phase_blank_mbps": g(r"blank_mean=\d+ c \((\d+) MB/s\)", int),
        "phase_active_min": g(r"active_min=(\d+) c", int),
        "phase_active_mean": g(r"active_mean=(\d+) c", int),
        "phase_active_mbps": g(r"active_mean=\d+ c \((\d+) MB/s\)", int),
        "phase_active_penalty_pct": g(r"active_penalty_pct=(-?\d+)", int),

        # --- the R/B swap and the final visual state -------------------------
        "swap_cycles": g(r"present: rb_swap cycles=(\d+)", int),
        "swap_kbps": g(r"rb_swap cycles=\d+ bytes=\d+ kbps=(\d+)", int),
        "swap_correct": g(r"rb_swap .*correct=(\w+)") == "true",
        "now_rgb888": "present: now scanning RGB888" in text,
        "rgb888_underrun": g(r"now scanning RGB888 .*fifo_underrun=(\w+)") == "true",
        "rgb888_l1pfcr": g(r"now scanning RGB888 .*L1PFCR=(0x[0-9a-f]+)"),
        "rgb888_cfbar": g(r"now scanning RGB888 .*L1CFBAR=(0x[0-9a-f]+)"),
        "rgb888_cfblr": g(r"now scanning RGB888 .*L1CFBLR=(0x[0-9a-f]+)"),
        "present_on_glass": "the panel is now showing the DIRECT-rendered" in text,

        # --- the measurement half (must be untouched) ------------------------
        "frame_hash": g(r"\[vyr-size\] frame fnv1a=(0x[0-9a-f]{16})"),
        "timed_frames": g(r"timed: (\d+) warmed frames", int),
        "timed_cycles": g(r"warmed frames in (-?\d+) c", int),
        "heap_peak": g(r"workload ok: heap peak=(\d+)", int),
        "workload_ok": "workload ok" in text,
        "hardfault": "cpu exception" in text or "HardFault" in text,
        "panic": "FATAL [vyr-size] panic" in text,
    }
    if r["timed_cycles"] is not None and r["timed_cycles"] < 0:
        r["timed_cycles"] += 1 << 32
    n = r["timed_frames"]
    r["cycles_per_frame"] = (
        r["timed_cycles"] // n if r["timed_cycles"] and n else None)
    r["clock_ok"] = bool(r["on_pll"] and r["latency"] == 5 and r["prften"] == 1
                         and r["icen"] == 1 and r["dcen"] == 1)
    r["rows"] = [_cast(m.groupdict()) for m in ROW_RE.finditer(text)]
    r["verdicts"] = [_cast(m.groupdict()) for m in VERDICT_RE.finditer(text)]
    r["isr"] = [m.groupdict() for m in ISR_RE.finditer(text)]
    return r


# --- the comparison table ----------------------------------------------------

RECT_LABEL = {
    "full": "full frame 240x320",
    "dirty1": "dirty rect 96x32 (the vy_lcd readout)",
    "dirtyN": "dirty_rects(prev,next)",
}


def table(r, logf):
    """Print render and blit cost SEPARATELY per (tier, rect, mode)."""
    hz = r["sysclk_hz"] or 180_000_000
    by = {}
    for row in r["rows"]:
        by.setdefault((row["tier"], row["rect"]), {})[row["mode"]] = row
    log("", logf)
    log("PRESENTATION MODES — render and blit priced separately, DWT_CYCCNT "
        f"at {hz} Hz", logf)
    hdr = (f"{'tier':<6} {'rect':<7} {'mode':<7} {'area px':>8} "
           f"{'render c':>11} {'blit c':>10} {'total c':>11} {'ms':>7} "
           f"{'ns/px':>7} {'SDRAM MB/s':>11}")
    log(hdr, logf)
    log("-" * len(hdr), logf)
    for tier in ("Exact", "Fast", "Draft"):
        for rect in ("full", "dirty1", "dirtyN"):
            pair = by.get((tier, rect))
            if not pair:
                continue
            for mode in ("banded", "direct"):
                row = pair.get(mode)
                if not row:
                    continue
                log(f"{tier:<6} {rect:<7} {mode:<7} {row['area_px']:>8} "
                    f"{row['render']:>11,} {row['blit']:>10,} "
                    f"{row['total']:>11,} {row['total'] / hz * 1e3:>7.2f} "
                    f"{row['ns_per_px']:>7} {row['sdram_kbps'] / 1000:>11.1f}",
                    logf)
    log("", logf)
    log(f"{'tier':<6} {'rect':<7} {'banded c':>11} {'direct c':>11} "
        f"{'winner':>8} {'margin':>8} {'pixels equal':>13}", logf)
    for v in r["verdicts"]:
        log(f"{v['tier']:<6} {v['rect']:<7} {v['banded']:>11,} "
            f"{v['direct']:>11,} {v['winner']:>8} {v['margin']:>7}% "
            f"{v['equal']:>13}", logf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify-plain", action="store_true",
                    help="also flash+run the plain `board` build afterwards "
                         "and assert its hash and cycle count")
    ap.add_argument("--out", default=str(TMP / "board-present.json"))
    args = ap.parse_args()

    feats = "board,ltdc,present"
    TMP.mkdir(exist_ok=True)
    out = {
        "when": now(),
        "issue": 45,
        "board": "STM32F429I-DISC1 (STM32F429ZI, Cortex-M4F)",
        "question": "render DIRECT into the SDRAM framebuffer vs render into a "
                    "small buffer and flush — measured, not assumed",
        "probe": PROBE,
        "lock": str(LOCK),
        "tier": "all three (Quality is a runtime argument)",
    }
    rc = 0
    with open(TMP / "board-present.log", "a") as logf:
        log("=" * 78, logf)
        log(f"board-present: {CHIP} via ST-LINK {PROBE}, features={feats}", logf)

        built = build(feats, "present", logf)
        if not built:
            return 2
        elf, sha = built
        out["elf"], out["elf_sha256"] = elf, sha
        text = run_once(elf, str(TMP / "board-present-run.log"), logf)
        r = parse(text)
        out["run"] = r

        log(f"image is board build: {r['is_board_image']}; clock "
            f"{r['sysclk_hz']} Hz clock_ok={r['clock_ok']}", logf)
        log(f"SDRAM ok={r['sdram_ok']} bulk write={r['sdram_write_mbps']} MB/s "
            f"read={r['sdram_read_mbps']} MB/s; scanout_live={r['scanout_live']} "
            f"refresh={r['refresh_mhz_measured']} mHz", logf)
        log(f"RGB888 framebuffer at {r['fb888_announced']}", logf)
        log(f"dirty_rects: n={r['dirty_n']} px={r['dirty_px']} "
            f"({r['dirty_pct']}% of screen) diff={r['dirty_diff_cycles']} c "
            f"rects={r['dirty_rect_list']}", logf)

        table(r, logf)

        log("", logf)
        log("MEMORY SYSTEM (same 8 KiB store/load loop, only LTDC differs):", logf)
        log(f"  CCM            write {r['ccm_w_mbps']} MB/s   read {r['ccm_r_mbps']} MB/s", logf)
        log(f"  SDRAM, RGB565 scan-out   write {r['sdram_w_rgb565scan_mbps']} MB/s "
            f"read {r['sdram_r_rgb565scan_mbps']} MB/s", logf)
        log(f"  SDRAM, RGB888 scan-out   write {r['sdram_w_rgb888scan_mbps']} MB/s "
            f"read {r['sdram_r_rgb888scan_mbps']} MB/s", logf)
        log(f"  SDRAM, layer OFF         write {r['sdram_w_layeroff_mbps']} MB/s "
            f"read {r['sdram_r_layeroff_mbps']} MB/s", logf)
        log(f"  CCM is {r['ccm_vs_sdram']}x faster than SDRAM for writes; "
            f"scan-out costs {r['scanout_cost_rgb565_pct']}% (RGB565) / "
            f"{r['scanout_cost_rgb888_pct']}% (RGB888) of CPU write time", logf)
        log(f"  blanking vs active scan-out: blank {r['phase_blank_mean']} c "
            f"({r['phase_blank_mbps']} MB/s) vs active {r['phase_active_mean']} c "
            f"({r['phase_active_mbps']} MB/s) => "
            f"{r['phase_active_penalty_pct']}% penalty during active scan "
            f"({r['phase_reps']} reps)", logf)
        log(f"  LTDC FIFO underruns seen: "
            f"{[i for i in r['isr'] if i['fuif'] == 'true']}", logf)
        log(f"  R/B swap pass (direct + correct colours on glass): "
            f"{r['swap_cycles']} c ({r['swap_kbps']} kB/s), "
            f"correct={r['swap_correct']}", logf)

        log("", logf)
        log(f"MEASUREMENT half untouched: hash={r['frame_hash']} "
            f"(ref {REFERENCE_HASH['Exact']}) "
            f"cycles/frame={r['cycles_per_frame']} "
            f"heap_peak={r['heap_peak']} ok={r['workload_ok']}", logf)

        # --- verdicts the machine can settle ---------------------------------
        out["pixels_identical_across_modes"] = bool(r["pixels_identical"])
        out["all_verdict_rows_equal"] = all(
            v["equal"] == "true" for v in r["verdicts"]) and bool(r["verdicts"])
        out["panel_scene_matches_spi_path"] = (
            r["ltdc_frame_hash"] == PANEL_SCENE_HASH["Exact"])
        out["measurement_unaffected"] = bool(
            r["workload_ok"] and r["frame_hash"] == REFERENCE_HASH["Exact"]
            and r["clock_ok"])
        if r["cycles_per_frame"]:
            out["cycles_delta_ppm"] = round(
                1e6 * (r["cycles_per_frame"] - REFERENCE_CYCLES["Exact"])
                / REFERENCE_CYCLES["Exact"])
        out["underruns_seen"] = [i for i in r["isr"] if i["fuif"] == "true"]

        if not r["sdram_ok"]:
            log("*** SDRAM MEMORY TEST FAILED ***", logf)
            rc = 3
        elif not r["scanout_live"]:
            log("*** LTDC IS NOT SCANNING ***", logf)
            rc = 3
        if not r["rows"]:
            log("*** NO PRESENTATION ROWS PARSED — the comparison did not run "
                "***", logf)
            rc = rc or 8
        if not out["all_verdict_rows_equal"]:
            bad = [v for v in r["verdicts"] if v["equal"] != "true"]
            log(f"*** MODES DISAGREE ON PIXELS: {bad} ***", logf)
            rc = rc or 7
        if not out["panel_scene_matches_spi_path"]:
            log(f"*** PANEL SCENE HASH {r['ltdc_frame_hash']} != SPI-path "
                f"reference {PANEL_SCENE_HASH['Exact']} ***", logf)
            rc = rc or 7
        if not out["measurement_unaffected"]:
            log(f"*** MEASUREMENT REGRESSED: hash {r['frame_hash']} vs "
                f"{REFERENCE_HASH['Exact']}, clock_ok={r['clock_ok']} ***", logf)
            rc = rc or 4

        if args.verify_plain:
            b2 = build("board", "plain", logf)
            if b2:
                t2 = run_once(b2[0], str(TMP / "board-plain-run.log"), logf)
                r2 = parse(t2)
                out["plain_board_run"] = {
                    k: r2[k] for k in
                    ("frame_hash", "cycles_per_frame", "heap_peak", "workload_ok")}
                out["plain_hash_matches"] = (
                    r2["frame_hash"] == REFERENCE_HASH["Exact"])
                log(f"PLAIN `board`: hash={r2['frame_hash']} "
                    f"cycles/frame={r2['cycles_per_frame']} "
                    f"matches={out['plain_hash_matches']}", logf)
                if not out["plain_hash_matches"]:
                    rc = rc or 5

        out["machine_verified"] = {
            "both_modes_ran": bool(r["rows"]),
            "pixels_identical_across_modes": out["pixels_identical_across_modes"],
            "renderer_hash_equals_spi_path": out["panel_scene_matches_spi_path"],
            "sdram_proven": bool(r["sdram_ok"]),
            "ltdc_scanning": bool(r["scanout_live"]),
            "measured_refresh_mhz": r["refresh_mhz_measured"],
            "rgb888_layer_live_no_underrun": bool(
                r["now_rgb888"] and not r["rgb888_underrun"]),
            "rb_swap_correct": bool(r["swap_correct"]),
            "measurement_unaffected": out["measurement_unaffected"],
        }
        out["visual_confirmation"] = (
            "NOT MACHINE-VERIFIABLE. The panel is left scanning the RGB888 "
            "framebuffer that vyr-core wrote DIRECTLY, with the R/B swap "
            "applied. Machine evidence goes as far as: both modes produced "
            "byte-identical RGB888 pixels; the renderer hash equals the "
            "SPI-to-GRAM path's; the RGB888 layer is enabled with the expected "
            "L1PFCR/CFBAR/CFBLR and LTDC reports no FIFO underrun; the swap "
            "pass reproduced the independently-computed swapped hash. None of "
            "that proves light came out of the glass in the right colours — "
            "only a human looking at the board can settle that.")
        Path(args.out).write_text(json.dumps(out, indent=2))
        log(f"wrote {args.out}", logf)
    return rc


if __name__ == "__main__":
    sys.exit(main())

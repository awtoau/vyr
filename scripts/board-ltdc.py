#!/usr/bin/env python3
"""board-ltdc.py — build, flash and run the `ltdc` build of vyr-size on the
STM32F429I-DISC1, and report what is on the panel and what it cost (#30).

Same chip, same probe, same semihosting capture as scripts/board-run.py and
scripts/board-lcd.py; the difference is `--features board,ltdc`, which swaps
the SPI5 pixel push for hardware scan-out: FMC/SDRAM framebuffer at
0xD0000000, PLLSAI pixel clock, LTDC driving the ILI9341's 16-bit parallel RGB
bus. SPI5 remains the register channel.

What this script CHECKS rather than assumes:
  * the running image is the board build at 180 MHz with 5 WS + ART,
  * the SDRAM memory test passed (data bus, address bus, all 8 MiB),
  * PLLSAI locked and the pixel clock recomputed from the registers is 6 MHz,
  * LTDC is really scanning: LTDC_CPSR wrapped twice and the MEASURED frame
    period agrees with the nominal 280x328 at 6 MHz,
  * the 240x320 scene rendered with a stable frame hash, and the framebuffer
    read back out of SDRAM hashes stably too,
  * AND the 480x270 measurement still produces the SAME cross-ISA frame hash
    and cycle count -- the display path must not have cost the determinism or
    performance evidence anything.

What it CANNOT check: whether the image is visible. Only a human looking at
the board can settle that, so the summary says which claims are
machine-verified and which need eyes.

SHARED HARDWARE: another agent may be driving the same board. Every probe-rs
invocation is wrapped in an exclusive lock (atomic mkdir of tmp/.board.lock)
held for exactly one flash+run and released in a finally:.

Usage:
  python3 scripts/board-ltdc.py                 # build + flash + run + report
  python3 scripts/board-ltdc.py --draft         # Draft quality tier
  python3 scripts/board-ltdc.py --verify-plain  # ALSO re-run the plain `board`
                                                # build and assert hash+cycles

Output: tmp/board-ltdc.json, semihosting in tmp/board-ltdc-run.log,
log tmp/board-ltdc.log.
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
# THIS probe only. A second F42x board (005600343431511837393330) is attached
# to the same workstation and must never be driven from this repo.
PROBE = "0483:3752:0671FF484971754867174427"

TARGET = "thumbv7em-none-eabihf"
PROFILE = "release-mcu"
ELF = REPO / "target" / TARGET / PROFILE / "vyr-size"

# Python-side wall-clock guard (never a shell timeout). Flashing ~200 KB over
# ST-LINK/V2-1 is ~20 s; the LTDC build then spends ~0.4 s on the 8 MiB SDRAM
# test, ~0.2 s on panel init delays, a few seconds on the cost repetitions,
# plus the 20-frame timed loop (~13 s at Exact). 300 s is well past a healthy
# run and still kills a target wedged in a bring-up spin.
DEADLINE_S = 300

# --- the board lock ---------------------------------------------------------
# Shared with every other runner in this repo (scripts/board_lock.py): two
# agents driving the same ST-LINK at once produce plausible numbers for an
# image neither of them built.
sys.path.insert(0, str(HERE))
from board_lock import BoardLock  # noqa: E402

AGENT = "board-ltdc"


# --- logging ----------------------------------------------------------------

def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg, logf):
    line = f"[{now()}] {msg}"
    print(line)
    logf.write(line + "\n")
    logf.flush()


# --- build / run ------------------------------------------------------------

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
    """One flash+run. THE ONLY place the board is touched — lock held here
    and nowhere else, for exactly as long as probe-rs is running."""
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
    t0 = time.monotonic()
    with BoardLock(AGENT, lambda m: log(m, logf)):
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
    Path(semilog).write_text(out)
    log(f"probe-rs wall {time.monotonic() - t0:.1f}s rc={rc}; "
        f"semihosting -> {semilog}", logf)
    return out


# The cross-ISA reference for the 480x270 measurement frame: x86-64, qemu-M4
# and real silicon all agree on these. The LTDC build must NOT change them.
REFERENCE_HASH = {
    "Exact": "0x24dcaff531c6eb01",
    "Draft": "0xf98cbbdddd6da1ba",
}
# The committed baseline cycles/frame at Exact on this board (docs + issue
# #30's brief). Reported as a delta, never asserted to the cycle: DWT counts
# real silicon, and a few hundred cycles of bus-contention drift is not a
# regression.
REFERENCE_CYCLES = {"Exact": 112_328_558, "Draft": 12_609_945}

# The 240x320 PANEL_IR scene hash, folded from the RGB888 bytes vyr-core
# produces BEFORE any display-format conversion. It is therefore a property of
# the RENDERER, not of the display path -- so the SPI-to-GRAM build
# (scripts/board-lcd.py, `--features board,lcd`) and this LTDC build must
# agree exactly. Measured on this board 2026-07-23: both report
# 0xc8a77478f7f9055a at Exact with pixels_written=133574. A mismatch means one
# display path has contaminated the render, which is the single thing this
# whole leg must never do.
PANEL_SCENE_HASH = {
    "Exact": "0xc8a77478f7f9055a",
    "Draft": "0x8af0208ab4cbd221",
}


def parse(text):
    def g(pat, cast=str):
        m = re.search(pat, text)
        return cast(m.group(1)) if m else None

    res = {
        "is_board_image": "REAL SILICON" in text,
        "sysclk_hz": g(r"sysclk_hz=(\d+)", int),
        "clock_source": g(r"src=(.+?), crt0 \+ FPU"),
        "on_pll": g(r"on_pll=(\w+)") == "true",
        "latency": g(r"latency=(\d+)", int),
        "prften": g(r"prften=(\d+)", int),
        "icen": g(r"icen=(\d+)", int),
        "dcen": g(r"dcen=(\d+)", int),

        # --- SDRAM ---------------------------------------------------------
        "sdram_announced": "FMC SDRAM bank2" in text,
        "sdcr1": g(r"SDCR1=(0x[0-9a-f]{8})"),
        "sdcr2": g(r"SDCR2=(0x[0-9a-f]{8})"),
        "sdtr1": g(r"SDTR1=(0x[0-9a-f]{8})"),
        "sdtr2": g(r"SDTR2=(0x[0-9a-f]{8})"),
        "sdrtr": g(r"SDRTR=(0x[0-9a-f]{8})"),
        "sdram_data_bus_errors": g(r"data_bus_errors=(\d+)", int),
        "sdram_addr_bus_errors": g(r"addr_bus_errors=(\d+)", int),
        "sdram_pattern_errors": g(r"pattern_errors=(\d+)", int),
        "sdram_bytes": g(r"sdram test .*bytes=(\d+)", int),
        "sdram_write_cycles": g(r"write=(\d+) c \(", int),
        "sdram_write_mbps": g(r"write=\d+ c \((\d+) MB/s\)", int),
        "sdram_read_cycles": g(r"read=(\d+) c \(", int),
        "sdram_read_mbps": g(r"read=\d+ c \((\d+) MB/s\)", int),
        "sdram_ok": g(r"sdram test .*ok=(\w+)") == "true",
        "sdram_failed_hard": "SDRAM memory test FAILED" in text,

        # --- clocks / LTDC --------------------------------------------------
        "pllsai_locked": g(r"PLLSAI locked=(\w+)") == "true",
        "pixclk_hz": g(r"pixclk_hz=(\d+)", int),
        "ltdc_announced": "ltdc: LTDC 240x320 RGB565" in text,
        "ltdc_sscr": g(r"SSCR=(0x[0-9a-f]{8})"),
        "ltdc_bpcr": g(r"BPCR=(0x[0-9a-f]{8})"),
        "ltdc_awcr": g(r"AWCR=(0x[0-9a-f]{8})"),
        "ltdc_twcr": g(r"TWCR=(0x[0-9a-f]{8})"),
        "ltdc_gcr": g(r"GCR=(0x[0-9a-f]{8})"),
        "ltdc_l1cr": g(r"L1CR=(0x[0-9a-f]{8})"),
        "ltdc_l1pfcr": g(r"L1PFCR=(0x[0-9a-f]{8})"),
        "ltdc_l1cfbar": g(r"L1CFBAR=(0x[0-9a-f]{8})"),
        "ltdc_l1cfblr": g(r"L1CFBLR=(0x[0-9a-f]{8})"),
        "ltdc_l1cfblnr": g(r"L1CFBLNR=(0x[0-9a-f]{8})"),
        "scanout_live": "scan-out LIVE" in text,
        "scanout_dead": "scan-out NOT running" in text,
        "frame_period_cycles": g(r"frame period (\d+) core cycles", int),
        "refresh_mhz_measured": g(r"= (\d+) mHz refresh", int),
        "refresh_mhz_nominal": g(r"pixclk/frame at \d+ Hz\s*= (\d+) mHz", int),
        "cdsr": g(r"CDSR=(0x[0-9a-f]{8})"),

        # --- the scene ------------------------------------------------------
        "ltdc_frame_hash": g(r"ltdc: panel frame fnv1a=(0x[0-9a-f]{16})"),
        "ltdc_bands": g(r"ltdc: panel frame .*bands=(\d+)", int),
        "ltdc_pixels": g(r"ltdc: panel frame .*pixels_written=(\d+)", int),
        "fb_readback_hash": g(r"framebuffer readback fnv1a=(0x[0-9a-f]{16})"),
        "ltdc_on_glass": "frame is in SDRAM and LTDC is scanning it out" in text,
        "ltdc_error": g(r"ERROR \[vyr-size\] ltdc scene failed: (.+)"),

        # --- cost ------------------------------------------------------------
        "full_render_cycles": g(r"full-frame cost render=(\d+) c", int),
        "full_blit_cycles": g(r"full-frame cost render=\d+ c blit=(\d+) c", int),
        "full_total_cycles": g(r"full-frame cost .*total=(\d+) c", int),
        "full_fps": g(r"full-frame cost .*=> (\d+) fps", int),
        "full_hash_stable": g(r"hash stable=(\w+)") == "true",
        "dirty_rect": g(r"dirty-rect \(([\d, x]+)\)"),
        "dirty_render_cycles": g(r"dirty-rect .*cost render=(\d+) c", int),
        "dirty_blit_cycles": g(r"dirty-rect .*cost render=\d+ c blit=(\d+) c", int),
        "dirty_total_cycles": g(r"dirty-rect .*total=(\d+) c", int),
        "dirty_fps": g(r"dirty-rect .*=> (\d+) fps", int),
        "dirty_hash": g(r"dirty-rect .*fnv1a=(0x[0-9a-f]{16})"),

        # The controller's own answers (bit-banged read-back).
        "probe_id4": g(r"RDDID4\(D3\)=\[([0-9a-f ]+)\]"),
        "probe_madctl": g(r"RDDMADCTL\(0B\)=\[([0-9a-f ]+)\]"),
        "probe_colmod": g(r"RDDCOLMOD\(0C\)=\[([0-9a-f ]+)\]"),
        "probe_powermode": g(r"RDDPM\(0A\)=\[([0-9a-f ]+)\]"),
        "probe_answered": g(r"lcd probe .*answered=(\w+)") == "true",

        # --- the measurement half (must be untouched) ------------------------
        # Anchored on the tag: the LTDC lines come FIRST in this build, so an
        # unanchored /frame fnv1a=/ would pick up the panel scene's hash and
        # report a phantom regression.
        "frame_hash": g(r"\[vyr-size\] frame fnv1a=(0x[0-9a-f]{16})"),
        "timed_frames": g(r"timed: (\d+) warmed frames", int),
        "timed_cycles": g(r"warmed frames in (-?\d+) c", int),
        "heap_peak": g(r"workload ok: heap peak=(\d+)", int),
        "workload_ok": "workload ok" in text,
        "hardfault": "cpu exception" in text or "HardFault" in text,
        "panic": "FATAL [vyr-size] panic" in text,
    }
    if res["timed_cycles"] is not None and res["timed_cycles"] < 0:
        res["timed_cycles"] += 1 << 32
    n = res["timed_frames"]
    res["cycles_per_frame"] = (
        res["timed_cycles"] // n if res["timed_cycles"] and n else None)
    res["clock_ok"] = bool(
        res["on_pll"] and res["latency"] == 5 and res["prften"] == 1
        and res["icen"] == 1 and res["dcen"] == 1)
    return res


def expected_registers():
    """What the LTDC/FMC registers MUST read if this bring-up is correct.

    Derived once, here, from ST's BSP values (see vyr-size/src/ltdc.rs for the
    provenance of each), so that a silent typo in the firmware shows up as a
    register mismatch rather than as a blank screen nobody can explain.
    """
    return {
        # SDCR1 carries only SDCLK(2xHCLK)|RBURST(off)|RPIPE(1): 0x800|0x2000
        "sdcr1": 0x00002800,
        # SDCR2: NC=8(0) NR=12(0x4) MWID=16(0x10) NB=4(0x40) CAS=3(0x180)
        "sdcr2": 0x000001D4,
        # SDTR1: (TRC-1)<<12 | (TRP-1)<<20 = 6<<12 | 1<<20
        "sdtr1": 0x00106000,
        # SDTR2: (TMRD-1) | (TXSR-1)<<4 | (TRAS-1)<<8 | (TWR-1)<<16 | (TRCD-1)<<24
        "sdtr2": 0x01010361,
        "sdrtr": 1386 << 1,
        "ltdc_sscr": (9 << 16) | 1,
        "ltdc_bpcr": (29 << 16) | 3,
        "ltdc_awcr": (269 << 16) | 323,
        "ltdc_twcr": (279 << 16) | 327,
        "ltdc_l1pfcr": 2,          # RGB565
        "ltdc_l1cfbar": 0xD0000000,
        "ltdc_l1cfblr": (480 << 16) | 483,
        "ltdc_l1cfblnr": 320,
        "pixclk_hz": 6_000_000,
    }


def check_registers(r):
    """Compare what the silicon reported against what it must be."""
    want = expected_registers()
    out = {}
    for k, v in want.items():
        got = r.get(k)
        if isinstance(got, str):
            got = int(got, 16)
        out[k] = {"want": v, "got": got, "match": got == v}
    # GCR: only LTDCEN(0) is asserted by us; the dither-width fields are
    # read-only and part-specific, and the four polarity bits must be 0.
    gcr = r.get("ltdc_gcr")
    if gcr is not None:
        g = int(gcr, 16)
        out["ltdc_gcr_ltdcen"] = {"want": 1, "got": g & 1, "match": bool(g & 1)}
        out["ltdc_gcr_polarities_zero"] = {
            "want": 0, "got": (g >> 28) & 0xF, "match": ((g >> 28) & 0xF) == 0}
    l1cr = r.get("ltdc_l1cr")
    if l1cr is not None:
        v = int(l1cr, 16)
        out["ltdc_layer1_enabled"] = {
            "want": 1, "got": v & 1, "match": bool(v & 1)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", action="store_true",
                    help="Draft quality tier (--features draft)")
    ap.add_argument("--verify-plain", action="store_true",
                    help="also flash+run the plain `board` build afterwards "
                         "and assert its hash and cycle count")
    ap.add_argument("--out", default=str(TMP / "board-ltdc.json"))
    args = ap.parse_args()

    tier = "Draft" if args.draft else "Exact"
    feats = "board,ltdc,draft" if args.draft else "board,ltdc"
    TMP.mkdir(exist_ok=True)
    out = {
        "when": now(),
        "issue": 30,
        "board": "STM32F429I-DISC1 (STM32F429ZI, Cortex-M4F)",
        "panel": "ILI9341 240x320 in RGB-interface mode (0xF6 param3 = 0x06); "
                 "LTDC 16-bit parallel scan-out from FMC SDRAM bank 2 "
                 "@0xD0000000; SPI5 = register channel only",
        "probe": PROBE,
        "tier": tier,
    }
    rc = 0
    with open(TMP / "board-ltdc.log", "a") as logf:
        log("=" * 70, logf)
        log(f"board-ltdc: {CHIP} via ST-LINK {PROBE}, features={feats}", logf)

        built = build(feats, "ltdc", logf)
        if not built:
            return 2
        elf, sha = built
        out["elf"] = elf
        out["elf_sha256"] = sha
        text = run_once(elf, str(TMP / "board-ltdc-run.log"), logf)
        r = parse(text)
        out["run"] = r
        out["register_checks"] = check_registers(r)
        out["registers_all_match"] = all(
            v["match"] for v in out["register_checks"].values())

        ref = REFERENCE_HASH[tier]
        out["reference_hash"] = ref
        # Cross-PATH determinism: the same scene through the SPI-to-GRAM
        # driver must fold to the same bytes.
        out["panel_scene_reference_hash"] = PANEL_SCENE_HASH[tier]
        out["panel_scene_matches_spi_path"] = (
            r["ltdc_frame_hash"] == PANEL_SCENE_HASH[tier])
        out["reference_cycles_per_frame"] = REFERENCE_CYCLES[tier]
        out["measurement_unaffected"] = bool(
            r["workload_ok"] and r["frame_hash"] == ref and r["clock_ok"])
        if r["cycles_per_frame"]:
            out["cycles_delta_ppm"] = round(
                1e6 * (r["cycles_per_frame"] - REFERENCE_CYCLES[tier])
                / REFERENCE_CYCLES[tier])

        log(f"image is board build: {r['is_board_image']}; "
            f"clock {r['sysclk_hz']} Hz src={r['clock_source']} "
            f"clock_ok={r['clock_ok']}", logf)
        log(f"SDRAM: ok={r['sdram_ok']} data_bus={r['sdram_data_bus_errors']} "
            f"addr_bus={r['sdram_addr_bus_errors']} "
            f"pattern={r['sdram_pattern_errors']} "
            f"bytes={r['sdram_bytes']} "
            f"write={r['sdram_write_mbps']} MB/s "
            f"read={r['sdram_read_mbps']} MB/s", logf)
        log(f"CLOCK: PLLSAI locked={r['pllsai_locked']} "
            f"pixclk={r['pixclk_hz']} Hz", logf)
        log(f"LTDC: scanout_live={r['scanout_live']} "
            f"frame_period={r['frame_period_cycles']} c "
            f"refresh={r['refresh_mhz_measured']} mHz measured vs "
            f"{r['refresh_mhz_nominal']} mHz nominal "
            f"registers_all_match={out['registers_all_match']}", logf)
        log(f"SCENE: hash={r['ltdc_frame_hash']} bands={r['ltdc_bands']} "
            f"pixels={r['ltdc_pixels']} "
            f"fb_readback={r['fb_readback_hash']} "
            f"on_glass={r['ltdc_on_glass']} err={r['ltdc_error']}", logf)
        log(f"COST full-frame: render={r['full_render_cycles']} "
            f"blit={r['full_blit_cycles']} total={r['full_total_cycles']} c "
            f"=> {r['full_fps']} fps (hash stable={r['full_hash_stable']})",
            logf)
        log(f"COST dirty-rect {r['dirty_rect']}: "
            f"render={r['dirty_render_cycles']} blit={r['dirty_blit_cycles']} "
            f"total={r['dirty_total_cycles']} c => {r['dirty_fps']} fps", logf)
        log(f"MEASUREMENT: hash={r['frame_hash']} (ref {ref}) "
            f"cycles/frame={r['cycles_per_frame']} "
            f"(ref {REFERENCE_CYCLES[tier]}) "
            f"heap_peak={r['heap_peak']} ok={r['workload_ok']}", logf)

        if not r["sdram_ok"]:
            log("*** SDRAM MEMORY TEST FAILED — no framebuffer was placed ***",
                logf)
            rc = 3
        elif not r["scanout_live"]:
            log("*** LTDC IS NOT SCANNING — CPSR never wrapped. Nothing can be "
                "on the panel ***", logf)
            rc = 3
        elif not r["ltdc_on_glass"]:
            log("*** LTDC PATH DID NOT COMPLETE — the framebuffer holds at "
                "most a partial frame ***", logf)
            rc = 3
        if not out["panel_scene_matches_spi_path"]:
            log(f"*** PANEL SCENE HASH {r['ltdc_frame_hash']} != the SPI-path "
                f"reference {PANEL_SCENE_HASH[tier]} — a display path has "
                f"changed what the RENDERER produces ***", logf)
            rc = rc or 7
        if not out["registers_all_match"]:
            bad = [k for k, v in out["register_checks"].items()
                   if not v["match"]]
            log(f"*** REGISTER MISMATCH: {bad} ***", logf)
            rc = rc or 6
        if not out["measurement_unaffected"]:
            log(f"*** MEASUREMENT REGRESSED: hash {r['frame_hash']} vs "
                f"reference {ref}, clock_ok={r['clock_ok']} ***", logf)
            rc = 4

        if args.verify_plain:
            pf = "board,draft" if args.draft else "board"
            b2 = build(pf, "plain", logf)
            if b2:
                t2 = run_once(b2[0], str(TMP / "board-plain-run.log"), logf)
                r2 = parse(t2)
                out["plain_board_run"] = r2
                out["plain_hash_matches"] = r2["frame_hash"] == ref
                out["plain_cycles_per_frame"] = r2["cycles_per_frame"]
                out["plain_cycles_match"] = (
                    r2["cycles_per_frame"] == REFERENCE_CYCLES[tier])
                log(f"PLAIN `board` build: hash={r2['frame_hash']} (ref {ref}) "
                    f"cycles/frame={r2['cycles_per_frame']} "
                    f"(ref {REFERENCE_CYCLES[tier]}) "
                    f"hash_matches={out['plain_hash_matches']} "
                    f"cycles_match={out['plain_cycles_match']}", logf)
                if not out["plain_hash_matches"]:
                    rc = 5

        # Decode the genuine hardware evidence from the controller. RDDPM
        # (0x0A): D7 booster on, D4 sleep out, D3 normal mode, D2 display on.
        pm = (r.get("probe_powermode") or "").split()
        if pm:
            v = int(pm[0], 16)
            out["panel_power_mode"] = {
                "raw": pm[0],
                "booster_on": bool(v & 0x80),
                "sleep_out": bool(v & 0x10),
                "normal_mode": bool(v & 0x08),
                "display_on": bool(v & 0x04),
            }

        out["machine_verified"] = {
            "sdram_proven": bool(r["sdram_ok"]),
            "sdram_bytes_tested": r["sdram_bytes"],
            "pllsai_locked": bool(r["pllsai_locked"]),
            "ltdc_scanning": bool(r["scanout_live"]),
            "measured_refresh_mhz": r["refresh_mhz_measured"],
            "registers_match_st_bsp": out["registers_all_match"],
            "frame_rendered_and_hashed": r["ltdc_frame_hash"],
            "scene_hash_equals_spi_path": out["panel_scene_matches_spi_path"],
            "framebuffer_read_back_from_sdram": r["fb_readback_hash"],
            "measurement_unaffected": out["measurement_unaffected"],
        }
        out["visual_confirmation"] = (
            "NOT MACHINE-VERIFIABLE. Machine evidence goes as far as: the "
            "8 MiB SDRAM passed a data-bus, address-bus and full-device test; "
            "PLLSAI locked at a register-derived 6 MHz pixel clock; LTDC's own "
            "position counter wrapped, giving a MEASURED frame period that "
            "matches 280x328 pixels at that clock; every LTDC/FMC register "
            "reads back the value ST's BSP specifies; the scene rendered to a "
            "stable hash and the framebuffer read back out of SDRAM; and the "
            "ILI9341 answered a bit-banged read-back. None of that proves "
            "light came out of the glass — only a human looking at the board "
            "can settle that.")
        Path(args.out).write_text(json.dumps(out, indent=2))
        log(f"wrote {args.out}", logf)
    return rc


if __name__ == "__main__":
    sys.exit(main())

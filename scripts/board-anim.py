#!/usr/bin/env python3
"""board-anim.py — does the STM32F429I-DISC1's panel ANIMATE over SPI?

The claim under test (#28 follow-on, the counter-proposal to LTDC+SDRAM #30):
a full-screen ILI9341 flush is 153,600 B at 5.625 MHz = ~218 ms, which is why
the panel has been static-only -- but vyr renders arbitrary sub-rects and
`vyr_core::dirty_rects` says exactly which sub-rects changed, and the ILI9341
takes an arbitrary CASET/PASET GRAM window and retains every pixel outside it.
So BOTH the render and the wire cost should scale with dirty AREA rather than
screen area. This script builds `--features board,lcd,anim`, flashes it, and
reads back the DWT_CYCCNT breakdown the firmware measured:

    c_ir      building the next frame's IR JSON + Request::parse
    c_diff    dirty_rects(prev, next)
    c_render  render of the dirty sub-bands only
    c_flush   CASET/PASET + the RGB565 stream out of SPI5

reported per (quality tier x animation mode), with the dirty-area % those
correspond to -- because the whole claim is that cost tracks dirty area.

It also reads back the firmware's own byte-exactness proof: for each mode, an
incrementally-composited frame hash vs a full-render hash of the same IR. They
must be equal, or animation has quietly broken the determinism invariant.

And it re-flashes the PLAIN `--features board` build afterwards to prove the
measurement leg is untouched (reference 0x24dcaff531c6eb01 @ 112,328,558
cycles/frame).

What it CANNOT check: whether anything is visibly moving. Only a human looking
at the board can settle that; the summary says so explicitly.

SHARED HARDWARE: another agent drives the LTDC+SDRAM path on the SAME board.
Every probe-rs invocation is wrapped in an exclusive lock (atomic mkdir of
tmp/.board.lock in the SHARED checkout), held for one flash+run and released in
a finally: block. Builds and analysis happen OUTSIDE the lock.

Usage:
  python3 scripts/board-anim.py                 # build + flash + run + report
  python3 scripts/board-anim.py --no-baseline   # skip the plain `board` re-check
  python3 scripts/board-anim.py --preview       # ALSO render host PNGs of the
                                                # animated frames (vyr-cli)

Output: tmp/board-anim.json, semihosting in tmp/board-anim-run.log,
log tmp/board-anim.log.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
TMP = os.path.join(REPO, "tmp")

# The board is shared between concurrently running agents, so the lock must
# live in the SHARED checkout, not in this worktree's tmp/.
SHARED_TMP = "/mnt/2tb/git/vyr/tmp"
LOCK = os.path.join(SHARED_TMP, ".board.lock")
AGENT = "board-anim"

CHIP = "STM32F429ZI"
# THIS probe only. A second F42x board (005600343431511837393330) is attached
# to the same workstation and must never be driven from this repo.
PROBE = "0483:3752:0671FF484971754867174427"

TARGET = "thumbv7em-none-eabihf"
PROFILE = "release-mcu"
ELF = os.path.join(REPO, "target", TARGET, PROFILE, "vyr-size")

# Wall-clock guard on the probe-rs child (a Python timeout, never a shell one).
# Budget: ~20 s to flash ~200 KB over ST-LINK/V2-1, ~1 s for the static panel
# scene, ~25 s for the 8-cell animation sweep (the Exact/full cell alone is
# 6 frames x (0.37 s render + 0.22 s flush)), ~13 s for the untouched 20-frame
# Exact measurement loop. 300 s is ~4x a healthy run and still kills a target
# wedged in a bring-up spin.
DEADLINE_S = 300

# How long a lock may be held before it is presumed abandoned. One flash+run is
# <= DEADLINE_S; 10 minutes is well past that and is the brief's own threshold.
LOCK_STALE_S = 600
# How long to wait for the other agent to release the board before giving up.
LOCK_WAIT_S = 1800

SCREEN_PX = 240 * 320
SPI_HZ = 5_625_000

# The cross-ISA reference for the 480x270 measurement frame. Nothing in the
# animation path is allowed to move these.
REFERENCE_HASH = "0x24dcaff531c6eb01"
REFERENCE_CYCLES = 112_328_558


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg, logf):
    line = f"[{now()}] {msg}"
    print(line)
    logf.write(line + "\n")
    logf.flush()


# --- the board lock ---------------------------------------------------------


def lock_acquire(logf):
    """Atomic-mkdir exclusive lock on the physical board.

    mkdir is the primitive because it is atomic on every POSIX filesystem and
    leaves a directory whose mtime and contents diagnose a stale holder --
    unlike a PID file, which needs a create-exclusive open plus a write that is
    not part of the same atomic step.
    """
    os.makedirs(SHARED_TMP, exist_ok=True)
    deadline = time.monotonic() + LOCK_WAIT_S
    announced = False
    while True:
        try:
            os.mkdir(LOCK)
            with open(os.path.join(LOCK, "holder"), "w") as f:
                f.write(f"{AGENT} pid={os.getpid()} at={now()}\n")
            log(f"board lock ACQUIRED ({LOCK})", logf)
            return True
        except FileExistsError:
            holder = "<unreadable>"
            try:
                with open(os.path.join(LOCK, "holder")) as f:
                    holder = f.read().strip()
            except OSError:
                pass
            age = time.monotonic() - 0
            try:
                age = time.time() - os.path.getmtime(LOCK)
            except OSError:
                age = 0
            if age > LOCK_STALE_S:
                log(f"*** BREAKING A STALE BOARD LOCK: age {age:.0f}s > "
                    f"{LOCK_STALE_S}s, holder [{holder}] — if that agent is "
                    f"still alive this run and theirs will fight over the "
                    f"probe ***", logf)
                shutil.rmtree(LOCK, ignore_errors=True)
                continue
            if not announced:
                log(f"board lock held by [{holder}] ({age:.0f}s old) — waiting", logf)
                announced = True
            if time.monotonic() > deadline:
                log(f"ERROR: board lock still held after {LOCK_WAIT_S}s — giving up", logf)
                return False
            # Poll interval: a flash+run is tens of seconds, so 2 s costs at
            # most 2 s of extra latency and ~15 stat() calls per minute.
            time.sleep(2)


def lock_release(logf):
    shutil.rmtree(LOCK, ignore_errors=True)
    log("board lock released", logf)


# --- build / run ------------------------------------------------------------


def build(feats, tag, logf):
    """Build a feature set and SNAPSHOT the ELF (cargo writes every feature
    combination of this bin to the same path, so a concurrent build can swap
    the image between build and flash)."""
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
    snap = os.path.join(TMP, f"board-anim-{tag}.elf")
    shutil.copyfile(ELF, snap)
    with open(snap, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    log(f"snapshot {snap} (sha256:{digest[:16]})", logf)
    return snap, digest


def run_once(elf, semilog, logf):
    """Flash + run under the board lock, held for exactly this call."""
    args = [
        "probe-rs", "run",
        "--chip", CHIP,
        "--probe", PROBE,
        "--catch-hardfault",
        "--non-interactive",
        "--disable-progressbars",
        elf,
    ]
    if not lock_acquire(logf):
        return None
    try:
        log("probe-rs: " + " ".join(args), logf)
        t0 = time.monotonic()
        try:
            p = subprocess.run(args, cwd=REPO, capture_output=True, text=True,
                               timeout=DEADLINE_S)
            out, rc = p.stdout + p.stderr, p.returncode
        except subprocess.TimeoutExpired as e:
            def dec(x):
                x = x or b""
                return x.decode(errors="replace") if isinstance(x, bytes) else x
            out, rc = dec(e.stdout) + dec(e.stderr), None
            log(f"ERROR: target did not finish inside the {DEADLINE_S}s guard — "
                f"killed; partial output in {semilog}", logf)
        with open(semilog, "w") as f:
            f.write(out)
        log(f"probe-rs wall {time.monotonic() - t0:.1f}s rc={rc}; "
            f"semihosting -> {semilog}", logf)
        return out
    finally:
        lock_release(logf)


# --- parsing ----------------------------------------------------------------

ANIM_RE = re.compile(
    r"anim: q=(?P<q>\w+) mode=(?P<mode>\w+) frames=(?P<frames>\d+) "
    r"rects=(?P<rects>\d+) subbands=(?P<subbands>\d+) dirty_px=(?P<dirty_px>\d+) "
    r"screen_px=(?P<screen_px>\d+) c_ir=(?P<c_ir>\d+) c_diff=(?P<c_diff>\d+) "
    r"c_render=(?P<c_render>\d+) c_flush=(?P<c_flush>\d+)")

LIVE_RE = re.compile(
    r"anim live: q=(?P<q>\w+) mode=(?P<mode>\w+) frames=(?P<frames>\d+) "
    r"c_wall=(?P<c_wall>\d+) dirty_px=(?P<dirty_px>\d+) screen_px=(?P<screen_px>\d+)")

VERIFY_RE = re.compile(
    r"anim verify: q=(?P<q>\w+) mode=(?P<mode>\w+) rects=(?P<rects>\d+) "
    r"dirty_px=(?P<dirty_px>\d+) full=(?P<full>0x[0-9a-f]{16}) "
    r"composite=(?P<composite>0x[0-9a-f]{16}) match=(?P<match>\w+)")


def parse(text, logf):
    def g(pat, cast=str):
        m = re.search(pat, text)
        return cast(m.group(1)) if m else None

    sysclk = g(r"sysclk_hz=(\d+)", int) or 0
    res = {
        "is_board_image": "REAL SILICON" in text,
        "sysclk_hz": sysclk,
        "clock_source": g(r"src=(.+?), crt0 \+ FPU"),
        "on_pll": g(r"on_pll=(\w+)") == "true",
        "lcd_on_glass": "frame is on the glass" in text,
        "anim_done": "dirty-rect animation sweep done" in text,
        "anim_error": g(r"ERROR \[vyr-size\] anim failed: (.+)"),
        "lcd_error": g(r"ERROR \[vyr-size\] lcd scene failed: (.+)"),
        # The measurement half, which must be untouched. Anchored on the tag:
        # the panel scene's own hash line comes first in this build.
        "frame_hash": g(r"\[vyr-size\] frame fnv1a=(0x[0-9a-f]{16})"),
        "timed_frames": g(r"timed: (\d+) warmed frames", int),
        "timed_cycles": g(r"warmed frames in (-?\d+) c", int),
        "workload_ok": "workload ok" in text,
        "hardfault": "cpu exception" in text or "HardFault" in text,
        "panic": "FATAL [vyr-size] panic" in text,
    }
    if res["timed_cycles"] is not None and res["timed_cycles"] < 0:
        res["timed_cycles"] += 1 << 32
    n = res["timed_frames"]
    res["cycles_per_frame"] = (
        res["timed_cycles"] // n if res["timed_cycles"] and n else None)

    cells = []
    for m in ANIM_RE.finditer(text):
        d = {k: (int(v) if v.isdigit() else v) for k, v in m.groupdict().items()}
        f = d["frames"]
        total = d["c_ir"] + d["c_diff"] + d["c_render"] + d["c_flush"]
        dirty_per_frame = d["dirty_px"] / f
        cell = {
            "quality": d["q"],
            "mode": d["mode"],
            "frames": f,
            "rects_per_frame": d["rects"] / f,
            "subbands_per_frame": d["subbands"] / f,
            "dirty_px_per_frame": dirty_per_frame,
            "dirty_pct": 100.0 * dirty_per_frame / d["screen_px"],
            "bytes_per_frame": 2 * dirty_per_frame,
            "cycles": {
                "ir": d["c_ir"] / f,
                "diff": d["c_diff"] / f,
                "render": d["c_render"] / f,
                "flush": d["c_flush"] / f,
                "total": total / f,
            },
        }
        if sysclk:
            cell["ms"] = {k: 1000.0 * v / sysclk for k, v in cell["cycles"].items()}
            cell["fps"] = sysclk / (total / f) if total else None
        # The two per-dirty-pixel rates that say whether the claim holds:
        # flush should sit near 2 bytes x (sysclk/SPI_HZ) cycles/byte.
        if dirty_per_frame:
            cell["cycles_per_dirty_px"] = {
                "render": (d["c_render"] / f) / dirty_per_frame,
                "flush": (d["c_flush"] / f) / dirty_per_frame,
            }
        cells.append(cell)

    verifies = []
    for m in VERIFY_RE.finditer(text):
        d = m.groupdict()
        verifies.append({
            "quality": d["q"],
            "mode": d["mode"],
            "rects": int(d["rects"]),
            "dirty_px": int(d["dirty_px"]),
            "dirty_pct": 100.0 * int(d["dirty_px"]) / SCREEN_PX,
            "full_hash": d["full"],
            "composite_hash": d["composite"],
            "match": d["match"] == "true",
        })
    live = None
    m = LIVE_RE.search(text)
    if m:
        d = m.groupdict()
        f, wall = int(d["frames"]), int(d["c_wall"])
        live = {
            "quality": d["q"],
            "mode": d["mode"],
            "frames": f,
            "wall_cycles_per_frame": wall / f,
            "dirty_pct": 100.0 * int(d["dirty_px"]) / f / int(d["screen_px"]),
        }
        if sysclk:
            live["ms_per_frame"] = 1000.0 * wall / f / sysclk
            live["fps"] = sysclk * f / wall
            live["duration_s"] = wall / sysclk
    res["live"] = live

    res["cells"] = cells
    res["verify"] = verifies
    res["all_incremental_frames_byte_exact"] = (
        bool(verifies) and all(v["match"] for v in verifies))
    return res


# --- optional host preview --------------------------------------------------

# Mirrors vyr-size/src/anim.rs `ir()` -- a PREVIEW of the animated scene for a
# human, never the source of truth (the device builds its own JSON in Rust).
SINE32 = [50, 59, 67, 75, 82, 87, 92, 94, 95, 94, 92, 87, 82, 75, 67, 59,
          50, 41, 33, 25, 18, 13, 8, 6, 5, 6, 8, 13, 18, 25, 33, 41]
BACKDROPS = ["#22262B", "#3B2226", "#223B2B", "#2B3B22"]


def wave(f):
    return ",".join(str((2 * SINE32[(j + f * 2) % 32] + SINE32[(j * 3 + f) % 32]) // 3)
                    for j in range(48))


def preview_ir(f):
    return json.dumps({
        "schema_version": "0.6-vyvanse", "w": 240, "h": 320,
        "root": {"name": "view", "attrs": {"background": BACKDROPS[0]}, "children": [
            {"name": "vy_frame", "attrs": {"x": "8", "y": "8", "width": "224", "height": "40",
             "background": "#2E3440", "radius": "8", "border_width": "1",
             "border_color": "#4C566A"}},
            {"name": "vy_label", "attrs": {"x": "20", "y": "20", "width": "170", "height": "20",
             "text": "vyr dirty-rect anim", "color": "#ECEFF4"}},
            {"name": "vy_image", "attrs": {"x": "200", "y": "16", "width": "24", "height": "24",
             "src": "checker-24.png"}},
            {"name": "vy_gauge", "attrs": {"x": "16", "y": "56", "width": "100", "height": "100",
             "color": "#88C0D0"}},
            {"name": "vy_lcd", "attrs": {"x": "126", "y": "66", "width": "104", "height": "28",
             "text": str(1000 + (f * 137) % 900), "color": "#A3BE8C",
             "style_text_font": "roboto_20"}},
            {"name": "vy_label", "attrs": {"x": "128", "y": "98", "width": "100", "height": "16",
             "text": "rpm", "color": "#7A869A"}},
            {"name": "vy_toggle", "attrs": {"x": "126", "y": "120", "width": "56", "height": "28",
             "value": str(f % 2)}},
            {"name": "vy_label", "attrs": {"x": "188", "y": "126", "width": "46", "height": "18",
             "text": "run", "color": "#D8DEE9"}},
            {"name": "vy_chart", "attrs": {"x": "16", "y": "164", "width": "208", "height": "64",
             "mode": "scope", "color": "#88C0D0", "points": wave(f)}},
            {"name": "vy_slider", "attrs": {"x": "16", "y": "238", "width": "208", "height": "18",
             "value": str(5 + (f * 17) % 90)}},
            {"name": "vy_slider", "attrs": {"x": "16", "y": "262", "width": "208", "height": "18",
             "value": str(5 + (f * 29) % 90)}},
            {"name": "vy_progress", "attrs": {"x": "16", "y": "286", "width": "208", "height": "12",
             "value": str(5 + (f * 11) % 90)}},
            {"name": "vy_line", "attrs": {"x": "8", "y": "302", "width": "224", "height": "2",
             "background": "#4C566A"}},
            {"name": "vy_label", "attrs": {"x": "10", "y": "304", "width": "220", "height": "14",
             "text": "awto / vyr SPI dirty-rect", "color": "#7A869A"}},
        ]}
    })


def preview(logf):
    """Render the animated frames on the host so a human can see the scene AND
    so an IR mistake (a widget name, a chart attr) is caught before board time
    is spent on it."""
    outs = []
    for f in range(0, 8):
        p = os.path.join(TMP, f"anim-frame-{f}.json")
        with open(p, "w") as fh:
            fh.write(preview_ir(f))
        png = os.path.join(TMP, f"anim-frame-{f}.png")
        # The host preview uses the FULL roboto.ttf and the committed checker
        # PNG; the device uses the ASCII subset of the same face and the
        # pre-decoded RGBA twin of the same image. Same glyph outlines, so the
        # preview is representative -- but it is a preview, not the oracle. The
        # oracle is the firmware's own composite-vs-full hash comparison.
        env = dict(os.environ,
                   VYR_FONTS=os.path.join(REPO, "fonts"),
                   VYR_ASSETS=os.path.join(REPO, "vyr-core", "tests", "assets"))
        r = subprocess.run(
            ["cargo", "run", "-q", "-p", "vyr-cli", "--", "render", p, png],
            cwd=REPO, capture_output=True, text=True, env=env)
        logf.write(r.stdout + r.stderr + "\n")
        if r.returncode != 0:
            log(f"PREVIEW RENDER FAILED for frame {f} — the animated IR is not "
                f"renderable; see log", logf)
            return None
        outs.append(png)
    log(f"preview PNGs: {outs[0]} .. {outs[-1]}", logf)
    return outs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-baseline", action="store_true",
                    help="skip re-flashing the plain `board` build to re-assert "
                         "the reference hash/cycles")
    ap.add_argument("--preview", action="store_true",
                    help="also render host PNGs of the animated frames")
    ap.add_argument("--out", default=os.path.join(TMP, "board-anim.json"))
    args = ap.parse_args()

    os.makedirs(TMP, exist_ok=True)
    out = {
        "when": now(),
        "issue": "28 follow-on (vs LTDC+SDRAM #30)",
        "board": "STM32F429I-DISC1 (STM32F429ZI, Cortex-M4F @ 180 MHz)",
        "panel": "ILI9341 240x320 over SPI5 at PCLK2/16 = 5.625 MHz, "
                 "GRAM fed from the serial port (0xF6 RM=0 DM=00)",
        "probe": PROBE,
        "screen_px": SCREEN_PX,
        "spi_hz": SPI_HZ,
        "full_frame_bytes": SCREEN_PX * 2,
        "full_frame_wire_ms": 1000.0 * SCREEN_PX * 2 * 8 / SPI_HZ,
    }
    rc = 0
    with open(os.path.join(TMP, "board-anim.log"), "a") as logf:
        log("=" * 70, logf)
        log(f"board-anim: {CHIP} via ST-LINK {PROBE}", logf)

        if args.preview:
            out["preview_pngs"] = preview(logf)
            if out["preview_pngs"] is None:
                return 2

        built = build("board,lcd,anim", "anim", logf)
        if not built:
            return 2
        elf, sha = built
        out["elf"], out["elf_sha256"] = elf, sha

        text = run_once(elf, os.path.join(TMP, "board-anim-run.log"), logf)
        if text is None:
            log("ERROR: never got the board lock — nothing was flashed", logf)
            return 6
        r = parse(text, logf)
        out["run"] = r

        log(f"image is board build: {r['is_board_image']} @ {r['sysclk_hz']} Hz "
            f"src={r['clock_source']}", logf)
        log(f"static panel frame on glass: {r['lcd_on_glass']}; "
            f"anim sweep done: {r['anim_done']} err={r['anim_error']}", logf)
        for c in r["cells"]:
            m = c.get("ms", {})
            log(f"  {c['quality']:5s} {c['mode']:8s} dirty={c['dirty_pct']:6.2f}% "
                f"({c['dirty_px_per_frame']:8.0f} px, {c['rects_per_frame']:.1f} rects) "
                f"ir={m.get('ir', 0):7.2f} diff={m.get('diff', 0):6.3f} "
                f"render={m.get('render', 0):8.2f} flush={m.get('flush', 0):8.2f} "
                f"total={m.get('total', 0):8.2f} ms -> {c.get('fps', 0):6.2f} fps", logf)
        if r["live"]:
            lv = r["live"]
            log(f"  LIVE   {lv['quality']:5s} {lv['mode']:8s} "
                f"dirty={lv['dirty_pct']:6.2f}% {lv['frames']} continuous frames in "
                f"{lv.get('duration_s', 0):.2f} s = {lv.get('ms_per_frame', 0):.2f} ms/frame "
                f"-> {lv.get('fps', 0):.2f} fps END-TO-END", logf)
        else:
            log("  LIVE: no continuous-run line — the live loop did not report", logf)
        for v in r["verify"]:
            log(f"  verify {v['quality']:5s} {v['mode']:8s} "
                f"full={v['full_hash']} composite={v['composite_hash']} "
                f"match={v['match']}", logf)

        if not r["anim_done"]:
            log("*** ANIMATION SWEEP DID NOT COMPLETE ***", logf)
            rc = 3
        if not r["all_incremental_frames_byte_exact"]:
            log("*** INCREMENTAL REPAINT IS NOT BYTE-EXACT vs a full render — "
                "the determinism proof is broken ***", logf)
            rc = 4

        # The animation build's own measurement leg must still be the reference.
        out["measurement_in_anim_build"] = {
            "frame_hash": r["frame_hash"],
            "cycles_per_frame": r["cycles_per_frame"],
            "hash_matches": r["frame_hash"] == REFERENCE_HASH,
            "cycles_match": r["cycles_per_frame"] == REFERENCE_CYCLES,
        }
        if not out["measurement_in_anim_build"]["hash_matches"]:
            log(f"*** MEASUREMENT HASH REGRESSED in the anim build: "
                f"{r['frame_hash']} vs {REFERENCE_HASH} ***", logf)
            rc = 5

        if not args.no_baseline:
            b2 = build("board", "plain", logf)
            if b2:
                t2 = run_once(b2[0], os.path.join(TMP, "board-anim-plain-run.log"),
                              logf)
                if t2 is None:
                    log("ERROR: never got the board lock for the baseline re-check",
                        logf)
                    rc = 6
                else:
                    r2 = parse(t2, logf)
                    out["plain_board_run"] = {
                        "frame_hash": r2["frame_hash"],
                        "cycles_per_frame": r2["cycles_per_frame"],
                        "workload_ok": r2["workload_ok"],
                        "hash_matches": r2["frame_hash"] == REFERENCE_HASH,
                        "cycles_match": r2["cycles_per_frame"] == REFERENCE_CYCLES,
                        "reference_hash": REFERENCE_HASH,
                        "reference_cycles_per_frame": REFERENCE_CYCLES,
                    }
                    log(f"PLAIN `board`: hash={r2['frame_hash']} "
                        f"(ref {REFERENCE_HASH}) cycles/frame="
                        f"{r2['cycles_per_frame']} (ref {REFERENCE_CYCLES})", logf)
                    if not out["plain_board_run"]["hash_matches"]:
                        rc = 5

        out["visual_confirmation"] = (
            "NOT MACHINE-VERIFIABLE. Machine evidence goes as far as: the "
            "firmware set a CASET/PASET window per dirty rect and streamed "
            "RGB565 into it, and its own on-device composite hash equals a "
            "full render's. Whether the panel is VISIBLY moving -- i.e. "
            "whether the controller really retained the untouched pixels "
            "between updates -- can only be settled by a human watching the "
            "board.")
        with open(args.out, "w") as f:
            json.dump(out, f, indent=2)
        log(f"wrote {args.out} (rc={rc})", logf)
    return rc


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""board-lcd.py — build, flash and run the `lcd` build of vyr-size on the
STM32F429I-DISC1, and report what should now be on the panel (#28).

Same probe, same chip, same semihosting capture as scripts/board-run.py; the
difference is `--features board,lcd`, which adds the ILI9341-over-SPI5 display
path (vyr-size/src/lcd.rs) in front of the existing measurement workload.

What this script checks, rather than assumes:
  * the running image really is the LCD build (its own boot line),
  * the panel init + full-screen fill reported completion (pixel path),
  * the 240x320 panel frame rendered and flushed, with its own stable hash,
  * AND the 480x270 measurement still produced the SAME cross-ISA frame hash
    and a cycle count -- the display path must not have cost the determinism
    or performance evidence anything.

It CANNOT check the one thing that matters most: whether the image is visible.
Only a human looking at the board can do that, so the summary says exactly
which claims are machine-verified and which need eyes.

Usage:
  python3 scripts/board-lcd.py                # build + flash + run + report
  python3 scripts/board-lcd.py --draft        # Draft quality tier
  python3 scripts/board-lcd.py --verify-plain # ALSO re-run the plain `board`
                                              # build and assert its hash

Output: tmp/board-lcd.json, semihosting in tmp/board-lcd-run.log,
log tmp/board-lcd.log.
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

CHIP = "STM32F429ZI"
# THIS probe only. A second F42x board (005600343431511837393330) is attached
# to the same workstation and must never be driven from this repo.
PROBE = "0483:3752:0671FF484971754867174427"

TARGET = "thumbv7em-none-eabihf"
PROFILE = "release-mcu"
ELF = os.path.join(REPO, "target", TARGET, PROFILE, "vyr-size")

# Python-side wall-clock guard (never a shell timeout). Flashing ~200 KB over
# ST-LINK/V2-1 is ~20 s; the LCD build then spends ~0.5 s per SPI full-screen
# write plus the 20-frame timed loop (~13 s at the Exact tier). 240 s is well
# past a healthy run and still kills a target wedged in a bring-up spin.
DEADLINE_S = 240

# The cross-ISA reference for the 480x270 measurement frame: x86-64, qemu-M4
# and real silicon all agree on these. The LCD build must NOT change them.
REFERENCE_HASH = {
    "Exact": "0x24dcaff531c6eb01",
    "Draft": "0xf98cbbdddd6da1ba",
}


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
    concurrent `./dev.py size` can silently swap the image between build and
    flash (measured, see scripts/board-run.py). Snapshotting makes every run
    provably flash the bytes this invocation built.
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
    snap = os.path.join(TMP, f"board-{tag}.elf")
    shutil.copyfile(ELF, snap)
    with open(snap, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    log(f"snapshot {snap} (sha256:{digest[:16]})", logf)
    return snap, digest


def run_once(elf, semilog, logf):
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
    try:
        p = subprocess.run(args, cwd=REPO, capture_output=True, text=True,
                           timeout=DEADLINE_S)
        out = p.stdout + p.stderr
        rc = p.returncode
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
        "quality": g(r"quality=(\w+)"),
        # --- the LCD half -------------------------------------------------
        "lcd_announced": "lcd: ILI9341" in text,
        "lcd_fill_done": "panel init + full-screen fill done" in text,
        "lcd_frame_hash": g(r"lcd: panel frame fnv1a=(0x[0-9a-f]{16})"),
        "lcd_bands": g(r"lcd: panel frame .*bands=(\d+)", int),
        "lcd_pixels": g(r"lcd: panel frame .*pixels_written=(\d+)", int),
        "lcd_bytes_flushed": g(r"flushed=(\d+) B over SPI", int),
        "lcd_on_glass": "frame is on the glass" in text,
        "lcd_error": g(r"ERROR \[vyr-size\] lcd scene failed: (.+)"),
        # The controller's own answers (bit-banged read-back). These are the
        # only claims in this file that are HARDWARE evidence rather than
        # "the firmware said it wrote some bytes".
        "probe_id4": g(r"RDDID4\(D3\)=\[([0-9a-f ]+)\]"),
        "probe_madctl": g(r"RDDMADCTL\(0B\)=\[([0-9a-f ]+)\]"),
        "probe_colmod": g(r"RDDCOLMOD\(0C\)=\[([0-9a-f ]+)\]"),
        "probe_powermode": g(r"RDDPM\(0A\)=\[([0-9a-f ]+)\]"),
        "probe_answered": g(r"lcd probe .*answered=(\w+)") == "true",
        # --- the measurement half (must be untouched) ----------------------
        # Anchored on the tag: the LCD line is "lcd: panel frame fnv1a=" and
        # comes FIRST in this build, so an unanchored /frame fnv1a=/ picks up
        # the panel scene's hash and reports a phantom regression.
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", action="store_true",
                    help="Draft quality tier (--features draft)")
    ap.add_argument("--verify-plain", action="store_true",
                    help="also flash+run the plain `board` build afterwards "
                         "and assert its hash is still the reference")
    ap.add_argument("--out", default=os.path.join(TMP, "board-lcd.json"))
    args = ap.parse_args()

    tier = "Draft" if args.draft else "Exact"
    feats = "board,lcd,draft" if args.draft else "board,lcd"
    os.makedirs(TMP, exist_ok=True)
    out = {
        "when": now(),
        "issue": 28,
        "board": "STM32F429I-DISC1 (STM32F429ZI, Cortex-M4F)",
        "panel": "ILI9341 240x320, SPI5 to controller GRAM "
                 "(SCK PF7 / MOSI PF9 / CS PC2 / DCX PD13)",
        "probe": PROBE,
        "tier": tier,
    }
    rc = 0
    with open(os.path.join(TMP, "board-lcd.log"), "a") as logf:
        log("=" * 70, logf)
        log(f"board-lcd: {CHIP} via ST-LINK {PROBE}, features={feats}", logf)

        built = build(feats, "lcd", logf)
        if not built:
            return 2
        elf, sha = built
        out["elf"] = elf
        out["elf_sha256"] = sha
        text = run_once(elf, os.path.join(TMP, "board-lcd-run.log"), logf)
        r = parse(text)
        out["run"] = r

        ref = REFERENCE_HASH[tier]
        out["reference_hash"] = ref
        out["measurement_unaffected"] = bool(
            r["workload_ok"] and r["frame_hash"] == ref and r["clock_ok"])

        log(f"image is board build: {r['is_board_image']}; "
            f"clock {r['sysclk_hz']} Hz src={r['clock_source']} "
            f"clock_ok={r['clock_ok']}", logf)
        log(f"LCD: announced={r['lcd_announced']} fill_done={r['lcd_fill_done']} "
            f"frame_hash={r['lcd_frame_hash']} bands={r['lcd_bands']} "
            f"pixels={r['lcd_pixels']} flushed={r['lcd_bytes_flushed']} B "
            f"on_glass={r['lcd_on_glass']} err={r['lcd_error']}", logf)
        log(f"MEASUREMENT: hash={r['frame_hash']} (ref {ref}) "
            f"cycles/frame={r['cycles_per_frame']} "
            f"heap_peak={r['heap_peak']} ok={r['workload_ok']}", logf)

        if not r["lcd_on_glass"]:
            log("*** LCD PATH DID NOT COMPLETE — nothing (or only a partial "
                "frame) is on the panel ***", logf)
            rc = 3
        if not out["measurement_unaffected"]:
            log(f"*** MEASUREMENT REGRESSED: hash {r['frame_hash']} vs "
                f"reference {ref}, clock_ok={r['clock_ok']} ***", logf)
            rc = 4

        if args.verify_plain:
            pf = "board,draft" if args.draft else "board"
            b2 = build(pf, "plain", logf)
            if b2:
                t2 = run_once(b2[0], os.path.join(TMP, "board-plain-run.log"),
                              logf)
                r2 = parse(t2)
                out["plain_board_run"] = r2
                out["plain_hash_matches"] = r2["frame_hash"] == ref
                log(f"PLAIN `board` build: hash={r2['frame_hash']} "
                    f"(ref {ref}) cycles/frame={r2['cycles_per_frame']} "
                    f"matches={out['plain_hash_matches']}", logf)
                if not out["plain_hash_matches"]:
                    rc = 5

        # Decode the one piece of genuine hardware evidence. RDDPM (0x0A):
        # D7 booster on, D4 sleep out, D3 normal mode, D2 display on.
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
        out["panel_echoed_config"] = {
            "madctl_written": "0xC8",
            "madctl_read": (r.get("probe_madctl") or "").split()[:1],
            "colmod_written": "0x55 (DPI 16bpp | DBI 16bpp)",
            "colmod_read": (r.get("probe_colmod") or "").split()[:1],
            "note": "RDDCOLMOD returns only the DBI field; 0x05 = 16 bits/pixel.",
        }

        # The claim this script cannot make.
        out["visual_confirmation"] = (
            "NOT MACHINE-VERIFIABLE. Machine evidence goes as far as: the "
            "ILI9341 answered a bit-banged read-back, echoed the MADCTL and "
            "pixel format it was sent, and reported booster on / sleep out / "
            "display on; and the firmware then streamed 153,600 B of RGB565 "
            "into its GRAM. Whether that is VISIBLE, right way up and the "
            "right colours can only be settled by a human looking at the "
            "board.")
        with open(args.out, "w") as f:
            json.dump(out, f, indent=2)
        log(f"wrote {args.out}", logf)
    return rc


if __name__ == "__main__":
    sys.exit(main())

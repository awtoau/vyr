#!/usr/bin/env python3
"""board-spirate.py -- how fast can the DISC1's ILI9341 actually be written,
and does DMA pay for itself? (#28 follow-on)

Two questions, one flashed image.

1. THE RATE LADDER. SPI5 runs at PCLK2/16 = 5.625 MHz, the universally-safe
   default. PCLK2 is pinned at 90 MHz and the SPI divisor is a power of two, so
   there are exactly four rungs:

       /16  5.625 MHz  218.5 ms/frame   shipped default
       /8  11.25  MHz  109.2 ms/frame
       /4  22.5   MHz   54.6 ms/frame
       /2  45     MHz   27.3 ms/frame

   There is NO authoritative datasheet for these clone panels, so no rung is
   labelled "in spec" or "overclock" -- the read-back below IS the qualifier,
   and it is per-unit. Marginal SPI timing corrupts pixels rather than failing
   cleanly, so "it looked fine" is not evidence. The firmware therefore writes a
   pseudo-random pattern into GRAM at each rung and READS IT BACK over the
   bit-banged serial port (the panel's 6.66 MHz read ceiling governs the reader;
   SPI5's baud rate does not touch it). A rung whose read-back hash equals the
   reference /16 rung's is bit-exact on the wire FOR THIS UNIT. That is a machine
   verdict, per rung, with no human
   in the loop -- see vyr-size/src/spirate.rs for the three controls
   (repeatability, sensitivity, survival) that stop it being a vacuous one.

2. THE FLUSH ENGINE. The CPU currently busy-waits on TXE for the entire flush
   -- 513 measured cycles per pixel against a theoretical 512, i.e. ~99.6% wire
   efficiency and ~512 cycles per pixel spent doing nothing. DMA is now the
   DEFAULT flush engine (DMA2 Stream 4 Channel 2, SPI5_TX per ST's own CubeMX
   device database); `--no-dma` (i.e. `--features spipio`) selects the busy-wait
   baseline. The same image prices programmed I/O, serial DMA and DMA overlapped
   with the next band's render against each other.

The ladder is swept INSIDE one image (`lcd::spi_set_br`), so the verdicts are
not confounded by four different ELFs. The build-time parameter `VYR_SPI_BR`
sets what the SCENE and the identity strip on the glass are drawn at, and phase
0 below proves it reaches the flashed bytes by building the whole ladder and
asserting four distinct ELF hashes.

Usage:
  python3 scripts/board-spirate.py                 # verdict run at the default /16
  python3 scripts/board-spirate.py --rate 1        # draw the card at PCLK2/4 too
  python3 scripts/board-spirate.py --no-dma        # busy-wait engine only
  python3 scripts/board-spirate.py --params-only   # phase 0, no board

Output: tmp/board-spirate.json, semihosting tmp/board-spirate-run.log,
log tmp/board-spirate.log.
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
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
from board_lock import BoardLock  # noqa: E402

TMP = REPO / "tmp"
AGENT = "board-spirate"

CHIP = "STM32F429ZI"
# THIS probe only. A second F42x board (005600343431511837393330) is attached to
# the same workstation and must never be driven from this repo.
PROBE = "0483:3752:0671FF484971754867174427"
TARGET = "thumbv7em-none-eabihf"
PROFILE = "release-mcu"
ELF = REPO / "target" / TARGET / PROFILE / "vyr-size"

# Python-side wall-clock guard, never a shell timeout. Flash ~20 s; panel scene
# + card ~1 s; the rate ladder is six write+read cycles whose read half is
# 115,200 B of ~1 MHz bit-bang (~0.6 s each, and the firmware reports the rate
# it actually achieved); the engine comparison is eight full-screen repaints;
# then the ~13 s measurement workload. 600 s is several times a healthy run and
# still kills a target wedged in a bring-up spin.
DEADLINE_S = 600

# The cross-ISA reference for the 480x270 measurement frame. Nothing in this
# exercise may move it: an SPI baud rate that changed the renderer's output
# would mean the two are coupled, which they must not be.
REFERENCE_HASH = {"Exact": "0x24dcaff531c6eb01",
                  "Draft": "0xf98cbbdddd6da1ba"}
# The card body hashes, host-verified. Same rule: the wire rate must never
# change what the renderer produced.
CARD_BODY = {"testcard": "0x65b88925c9a2ba19",
             "orientcard": "0x50bf6c0f146d4d4b"}

# MADCTL 0x00 -- read off the glass by the sibling sweep: it fixes both the 180
# degree rotation and the R/B channel swap on the direct path. This is now also
# lcd.rs's compiled default; passed explicitly here so the flashed image and the
# identity strip agree on the value regardless of any local default drift.
DEFAULT_MADCTL = 0x00

RUNGS = [3, 2, 1, 0]
PCLK2 = 90_000_000


def rate(br):
    return PCLK2 >> (br + 1)


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg, logf):
    line = f"[{now()}] {msg}"
    print(line)
    logf.write(line + "\n")
    logf.flush()


def build(feats, br, madctl, tag, logf):
    """Build with the compile-time parameters in the environment, then SNAPSHOT
    the ELF.

    cargo writes every feature combination of this bin to the same path, so a
    concurrent build can silently swap the image between build and flash
    (measured -- see scripts/board-run.py). Snapshotting makes every run
    provably flash the bytes this invocation built, and gives phase 0 something
    to hash.
    """
    env = dict(os.environ)
    env["VYR_SPI_BR"] = str(br)
    env["VYR_MADCTL"] = f"{madctl:#04x}"
    cmd = ["cargo", "build", "-p", "vyr-size", "--target", TARGET,
           "--profile", PROFILE, "--no-default-features", "--features", feats]
    log(f"build [{tag}]: VYR_SPI_BR={br} (PCLK2/{1 << (br + 1)} = "
        f"{rate(br)} Hz) VYR_MADCTL={env['VYR_MADCTL']} {' '.join(cmd)}", logf)
    r = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True,
                       check=False)
    logf.write(r.stdout + r.stderr + "\n")
    logf.flush()
    if r.returncode != 0:
        log(f"*** BUILD FAILED [{tag}] -- see log ***", logf)
        return None
    snap = TMP / f"board-spirate-{tag}.elf"
    shutil.copyfile(ELF, snap)
    digest = hashlib.sha256(snap.read_bytes()).hexdigest()
    log(f"snapshot {snap} (sha256:{digest[:16]}, {snap.stat().st_size:,} B)",
        logf)
    return str(snap), digest


def run_once(elf, semilog, logf):
    """One flash+run. THE ONLY place the board is touched -- the lock is held
    here and nowhere else, for exactly as long as probe-rs runs, and released in
    a finally whatever happens."""
    args = ["probe-rs", "run", "--chip", CHIP, "--probe", PROBE,
            "--catch-hardfault", "--non-interactive",
            "--disable-progressbars", elf]
    log("probe-rs: " + " ".join(args), logf)
    t0 = time.monotonic()
    lock = BoardLock(AGENT, lambda m: log(m, logf))
    try:
        lock.__enter__()
        try:
            p = subprocess.run(args, cwd=REPO, capture_output=True, text=True,
                               timeout=DEADLINE_S, check=False)
            out, rc = p.stdout + p.stderr, p.returncode
        except subprocess.TimeoutExpired as e:
            def dec(x):
                x = x or b""
                return x.decode(errors="replace") if isinstance(x, bytes) else x
            out, rc = dec(e.stdout) + dec(e.stderr), None
            log(f"ERROR: target did not finish inside the {DEADLINE_S}s guard "
                f"-- killed; partial output in {semilog}", logf)
    finally:
        lock.__exit__(None, None, None)
    Path(semilog).write_text(out)
    log(f"probe-rs wall {time.monotonic() - t0:.1f}s rc={rc}; "
        f"semihosting -> {semilog}", logf)
    return out


def parse(text):
    def g(pat, cast=str, default=None):
        m = re.search(pat, text)
        return cast(m.group(1)) if m else default

    res = {
        "is_board_image": "REAL SILICON" in text,
        "sysclk_hz": g(r"sysclk_hz=(\d+)", int),
        "quality": g(r"quality=(\w+)"),
        "build_spi_hz": g(r"build_spi_hz=(\d+)", int),
        "build_br": g(r"build_br=(\d+)", int),
        "madctl_written": g(r"madctl_written=(0x[0-9a-f]+)"),
        "probe_answered": g(r"lcd probe .*answered=(\w+)") == "true",
        "probe_madctl": g(r"RDDMADCTL\(0B\)=\[([0-9a-f ]+)\]"),
        "lcd_frame_hash": g(r"lcd: panel frame fnv1a=(0x[0-9a-f]{16})"),
        "card_hash": g(r"testcard: path=lcd-spi-gram body=\d+x\d+ "
                       r"fnv1a=(0x[0-9a-f]{16})"),
        "frame_hash": g(r"\[vyr-size\] frame fnv1a=(0x[0-9a-f]{16})"),
        "timed_frames": g(r"timed: (\d+) warmed frames", int),
        "timed_cycles": g(r"warmed frames in (-?\d+) c", int),
        "workload_ok": "workload ok" in text,
        "hardfault": "cpu exception" in text or "HardFault" in text,
        "panic": "FATAL [vyr-size] panic" in text,
        # --- the rate ladder ------------------------------------------------
        "repeatable": g(r"spirate controls: repeatable=(\w+)") == "true",
        "sensitive": g(r"spirate controls: .*sensitive=(\w+)") == "true",
        "survived": g(r"spirate survival: .*match=(\w+)") == "true",
        "best_br": g(r"spirate summary: highest bit_exact br=(\d+)", int),
        "spirate_ran": "spirate summary" in text,
        "spirate_error": g(r"ERROR \[vyr-size\] spirate failed: (.+)"),
    }
    if res["timed_cycles"] is not None and res["timed_cycles"] < 0:
        res["timed_cycles"] += 1 << 32
    n = res["timed_frames"]
    res["cycles_per_frame"] = (
        res["timed_cycles"] // n if res["timed_cycles"] and n else None)

    res["rungs"] = []
    for m in re.finditer(
            r"spirate verdict: br=(\d+) spi_hz=(\d+) in_spec=(\w+) "
            r"bit_exact=(\w+) bit_exact_pio=(\w+) bit_exact_dma=(\w+) "
            r"readback=(0x[0-9a-f]+) reference=(0x[0-9a-f]+) pattern=(\d+) "
            r"full_frame_ms=(\d+) conclusive=(\w+)", text):
        res["rungs"].append({
            "br": int(m.group(1)), "spi_hz": int(m.group(2)),
            "divisor": 1 << (int(m.group(1)) + 1),
            "in_spec": m.group(3) == "true",
            # bit_exact is the AND of the two engines: a rung passes only if both
            # gapped PIO and gapless DMA read back the reference.
            "bit_exact": m.group(4) == "true",
            "bit_exact_pio": m.group(5) == "true",
            "bit_exact_dma": m.group(6) == "true",
            "readback": m.group(7), "reference": m.group(8),
            "pattern": int(m.group(9)),
            "full_frame_ms": int(m.group(10)),
            "conclusive": m.group(11) == "true",
        })
    res["raw_rungs"] = []
    for m in re.finditer(
            r"spirate rung: (\S+) br=(\d+) spi_hz=(\d+) in_spec=(\w+) "
            r"px=(\d+) wrote_B=(\d+) write_cyc=(\d+) ideal_wire_cyc=(\d+) "
            r"cyc_per_px=(\d+) read_B=(\d+) read_cyc=(\d+) read_hz=(\d+) "
            r"fnv1a=(0x[0-9a-f]+)", text):
        res["raw_rungs"].append({
            "label": m.group(1), "br": int(m.group(2)),
            "spi_hz": int(m.group(3)), "in_spec": m.group(4) == "true",
            "px": int(m.group(5)), "wrote_B": int(m.group(6)),
            "write_cyc": int(m.group(7)), "ideal_wire_cyc": int(m.group(8)),
            "cyc_per_px": int(m.group(9)), "read_B": int(m.group(10)),
            "read_cyc": int(m.group(11)), "read_hz": int(m.group(12)),
            "fnv1a": m.group(13),
        })
    # --- the flush engines --------------------------------------------------
    res["flush"] = []
    for m in re.finditer(
            r"spirate flush: engine=(\S+) br=(\d+) spi_hz=(\d+) bands=(\d+) "
            r"wall_cyc=(\d+) wall_ms=(\d+) render_cyc=(\d+) render_ms=(\d+) "
            r"blocked_cyc=(\d+) blocked_ms=(\d+) cpu_cyc=(\d+) cpu_ms=(\d+) "
            r"cpu_pct=(\d+) fps_x100=(\d+) hidden_render_cyc=(\d+) "
            r"us_per_band=(\d+)", text):
        res["flush"].append({
            "engine": m.group(1), "br": int(m.group(2)),
            "spi_hz": int(m.group(3)), "bands": int(m.group(4)),
            "wall_cyc": int(m.group(5)), "wall_ms": int(m.group(6)),
            "render_cyc": int(m.group(7)), "render_ms": int(m.group(8)),
            "blocked_cyc": int(m.group(9)), "blocked_ms": int(m.group(10)),
            "cpu_cyc": int(m.group(11)), "cpu_ms": int(m.group(12)),
            "cpu_pct": int(m.group(13)), "fps": int(m.group(14)) / 100.0,
            "hidden_render_cyc": int(m.group(15)),
            "us_per_band": int(m.group(16)),
        })
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=int, default=3, choices=RUNGS,
                    help="VYR_SPI_BR the SCENE and the identity strip are "
                         "drawn at (the ladder is swept inside the image "
                         "regardless). 3=/16 (shipped default), 2=/8, 1=/4, 0=/2")
    ap.add_argument("--madctl", type=lambda s: int(s, 0),
                    default=DEFAULT_MADCTL)
    ap.add_argument("--no-dma", action="store_true",
                    help="build with --features spipio (busy-wait flush only; "
                         "DMA is otherwise the default engine)")
    ap.add_argument("--card", choices=["colour", "orient"], default="colour")
    ap.add_argument("--draft", action="store_true")
    ap.add_argument("--params-only", action="store_true",
                    help="phase 0 only: prove VYR_SPI_BR reaches the ELF")
    ap.add_argument("--out", default=str(TMP / "board-spirate.json"))
    args = ap.parse_args()

    tier = "Draft" if args.draft else "Exact"
    feats = ["board", "lcd", "spicheck"]
    if args.no_dma:
        feats.append("spipio")
    feats.append("orientcard" if args.card == "orient" else "testcard")
    if args.draft:
        feats.append("draft")
    feats = ",".join(feats)

    TMP.mkdir(exist_ok=True)
    out = {
        "when": now(), "issue": 28, "features": feats, "tier": tier,
        "board": "STM32F429I-DISC1 (STM32F429ZI, Cortex-M4F)",
        "panel": "ILI9341 240x320, SPI5 -> controller GRAM",
        "probe": PROBE, "pclk2_hz": PCLK2,
        "madctl": f"{args.madctl:#04x}",
        "spec": {"write_max_hz": 10_000_000, "read_max_hz": 6_660_000,
                 "source": "ST ili9341_spi.c: 'max baudrate is 10 MHz for "
                           "write and 6.66 MHz for read'"},
        "ladder": [{"br": b, "divisor": 1 << (b + 1), "spi_hz": rate(b),
                    "full_frame_ms": round(240 * 320 * 16 / rate(b) * 1000, 1),
                    "in_spec": rate(b) <= 10_000_000} for b in RUNGS],
    }
    rc = 0
    with open(TMP / "board-spirate.log", "a") as logf:
        log("=" * 70, logf)
        log(f"board-spirate: {CHIP} via ST-LINK {PROBE}, features={feats}",
            logf)

        # --- phase 0: the parameter must reach the flashed bytes -------------
        # Not ceremony. `option_env!` is a compile-time read, and a build system
        # that did not re-run rustc when the variable changed would produce four
        # identical images and four identical "results" -- a sweep that swept
        # nothing, and one that looks exactly like a sweep that did.
        params = {}
        for br in RUNGS:
            b = build(feats, br, args.madctl, f"br{br}", logf)
            if not b:
                return 2
            params[br] = {"elf": b[0], "sha256": b[1], "spi_hz": rate(br)}
        digests = {v["sha256"] for v in params.values()}
        out["param_reaches_elf"] = {
            "builds": params, "distinct_hashes": len(digests),
            "ok": len(digests) == len(RUNGS),
        }
        log(f"VYR_SPI_BR reaches the image: {len(digests)} distinct ELF "
            f"hashes across {len(RUNGS)} rungs -> "
            f"{out['param_reaches_elf']['ok']}", logf)
        if not out["param_reaches_elf"]["ok"]:
            log("*** VYR_SPI_BR DID NOT CHANGE THE IMAGE — every rung would "
                "have run the same bytes; refusing to report a sweep ***", logf)
            rc = 6
        if args.params_only:
            Path(args.out).write_text(json.dumps(out, indent=2))
            log(f"wrote {args.out} (params-only)", logf)
            return rc

        # --- phase 1: one flash, one run, the whole ladder --------------------
        elf = params[args.rate]["elf"]
        out["flashed"] = {"br": args.rate, "spi_hz": rate(args.rate),
                          "elf": elf, "sha256": params[args.rate]["sha256"]}
        text = run_once(elf, TMP / "board-spirate-run.log", logf)
        r = parse(text)
        out["run"] = r

        ref = REFERENCE_HASH[tier]
        card_ref = CARD_BODY["orientcard" if args.card == "orient"
                             else "testcard"]
        out["reference_hash"] = ref
        out["card_reference"] = card_ref
        out["measurement_unaffected"] = bool(
            r["workload_ok"] and r["frame_hash"] == ref)
        # The published card-body hashes are Exact-tier numbers; at Draft the
        # renderer legitimately produces different pixels, so there is nothing
        # to compare against and the hash is recorded rather than asserted.
        out["card_unaffected"] = (r["card_hash"] == card_ref if tier == "Exact"
                                  else None)

        for x in r["rungs"]:
            log(f"RUNG /{x['divisor']:<2} {x['spi_hz']:>8} Hz  "
                f"in_spec={str(x['in_spec']):<5} "
                f"bit_exact={str(x['bit_exact']):<5} "
                f"full_frame={x['full_frame_ms']:>4} ms  "
                f"readback={x['readback']}", logf)
        log(f"controls: repeatable={r['repeatable']} "
            f"sensitive={r['sensitive']} survived={r['survived']} -> "
            f"conclusive={r['repeatable'] and r['sensitive'] and r['survived']}",
            logf)
        for f in r["flush"]:
            log(f"FLUSH {f['engine']:<12} /{1 << (f['br'] + 1):<2} "
                f"wall={f['wall_ms']:>4} ms cpu={f['cpu_ms']:>4} ms "
                f"({f['cpu_pct']:>3}%) blocked={f['blocked_ms']:>4} ms "
                f"fps={f['fps']:.2f}", logf)
        log(f"MEASUREMENT: hash={r['frame_hash']} (ref {ref}) "
            f"cycles/frame={r['cycles_per_frame']} ok={r['workload_ok']}",
            logf)
        log(f"CARD BODY: {r['card_hash']} (ref {card_ref}) "
            f"unchanged={out['card_unaffected']}", logf)

        conclusive = bool(r["repeatable"] and r["sensitive"] and r["survived"])
        out["machine_verdict_conclusive"] = conclusive
        best = None
        if conclusive:
            passing = [x for x in r["rungs"] if x["bit_exact"]]
            if passing:
                best = max(passing, key=lambda x: x["spi_hz"])
        out["highest_bit_exact"] = best
        if best:
            log(f"HIGHEST BIT-EXACT: /{best['divisor']} = {best['spi_hz']} Hz, "
                f"full frame {best['full_frame_ms']} ms, "
                f"in_spec={best['in_spec']}"
                + ("" if best["in_spec"] else
                   "  *** THIS IS AN OVERCLOCK: bit-exact on THIS board at "
                   "room temperature is not a characterisation. ***"), logf)

        if not r["spirate_ran"]:
            log("*** THE RATE LEG DID NOT COMPLETE — no verdict ***", logf)
            rc = 3
        if not conclusive:
            log("*** CONTROLS FAILED — rung results are INCONCLUSIVE, not "
                "passes. The read-back either is not deterministic, does not "
                "track the write, or the panel did not survive the sweep. ***",
                logf)
            rc = 3
        if not out["measurement_unaffected"]:
            log(f"*** MEASUREMENT REGRESSED: {r['frame_hash']} vs {ref} ***",
                logf)
            rc = 4
        if out["card_unaffected"] is False:
            log(f"*** CARD BODY HASH MOVED: {r['card_hash']} vs {card_ref} — "
                f"the renderer changed, which an SPI rate must never do ***",
                logf)
            rc = 5

        out["human_confirmation"] = (
            "NOT MACHINE-VERIFIABLE. The read-back settles whether the BITS "
            "arrived; only a person looking at the panel can settle whether "
            "the whole image is right, and the labelled colour card is what "
            "makes that a reading rather than a judgement. The identity strip "
            "on the glass names the rate the card was drawn at, so a "
            "photograph is self-describing.")
        Path(args.out).write_text(json.dumps(out, indent=2))
        log(f"wrote {args.out}", logf)
    return rc


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""board-qualify.py -- demonstrate the SPI link SELF-QUALIFIER on the bench unit
(#49).

`spirate::qualify` promotes the read-back from a measurement to a BEHAVIOUR: at
bring-up it picks the fastest bit-exact SPI rate THIS panel actually honours,
applies a one-rung-slower margin, records it where the flush path reads it, and
fails safe if even /16 does not read back clean. There is no datasheet for these
clone ILI9341s, so the read-back IS the qualifier, and the verdict is per-unit.

This runner flashes THREE images and checks the qualifier's decision in each --
one honest run and the two NEGATIVE demonstrations #49 asks for, driven by the
compile-time knob VYR_QUAL_FORCE_FAIL_HZ (a wire rate at/above which a rung's
read-back verdict is forced to fail, for the qualifier's decision only):

  1. normal    (knob unset)      -> qualifies the fastest safe rate; the real
                                    scene is flushed at it.
  2. marginal  (knob=45000000)   -> the /2 rung is FORCED to fail; the qualifier
                                    must reject it and STEP DOWN one rung.
  3. failsafe  (knob=1)          -> every rung including the /16 floor is forced
                                    to fail; the qualifier must report a FAULT
                                    and refuse to accelerate.

The three ELFs are snapshotted and asserted distinct first (phase 0), so a build
system that ignored the knob cannot masquerade as a passing sweep. The card body
and the 480x270 measurement frame hashes are checked UNCHANGED in every image --
a self-qualifier that moved the renderer's output would be a bug.

The read-back settles whether the BITS arrived; only a person looking at the
glass can settle whether the whole picture is right. The identity strip names the
qualified rate, so a photograph is self-describing. This script reports only the
MACHINE verdict.

Usage:
  python3 scripts/board-qualify.py                 # all three variants
  python3 scripts/board-qualify.py --only normal   # one variant
  python3 scripts/board-qualify.py --params-only    # phase 0, no board

Output: tmp/board-qualify.json, semihosting tmp/board-qualify-<v>-run.log,
log tmp/board-qualify.log.
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
AGENT = "board-qualify"

CHIP = "STM32F429ZI"
# THIS probe only -- the STM32F429I-DISC1 with the ILI9341. A second F42x board
# on the same workstation must never be driven from here.
PROBE = "0483:3752:0671FF484971754867174427"
TARGET = "thumbv7em-none-eabihf"
PROFILE = "release-mcu"
ELF = REPO / "target" / TARGET / PROFILE / "vyr-size"
FEATS = "board,lcd,spicheck,testcard"
DEFAULT_MADCTL = 0x00

# Same wall-clock guard board-spirate uses: flash ~20 s, the ladder's read-back
# half is ~1 MHz bit-bang, the engine comparison is full-screen repaints, then
# the ~13 s measurement workload. Never a shell timeout; a Python guard on the
# probe-rs subprocess.
DEADLINE_S = 600

PCLK2 = 90_000_000
RUNGS = [3, 2, 1, 0]

# The invariants no SPI rate or fault-injection may move.
REFERENCE_FRAME = "0x24dcaff531c6eb01"          # Exact-tier 480x270 measurement
CARD_BODY = "0x65b88925c9a2ba19"                # testcard body

# variant -> (VYR_QUAL_FORCE_FAIL_HZ or None, expected chosen br, expected status)
# Bench unit reads bit-exact to /2, so: normal ships /4 (one rung below /2);
# forcing /2 to fail pushes the fastest pass to /4 and ships /8; forcing the
# floor to fail trips the fail-safe.
VARIANTS = {
    "normal":   {"knob": None,     "want_br": 1, "want_status": "OK",
                 "want_fastest_br": 0},
    "marginal": {"knob": 45_000_000, "want_br": 2, "want_status": "OK",
                 "want_fastest_br": 1},
    "failsafe": {"knob": 1,        "want_br": 3, "want_status": "FAULT",
                 "want_fastest_br": 3},
}


def rate(br):
    return PCLK2 >> (br + 1)


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=list(VARIANTS), default=None,
                    help="run just one variant (default: all three)")
    ap.add_argument("--madctl", type=lambda s: int(s, 0), default=DEFAULT_MADCTL)
    ap.add_argument("--params-only", action="store_true",
                    help="phase 0 only: build + prove the knob reaches the ELF")
    ap.add_argument("--out", default=str(TMP / "board-qualify.json"))
    args = ap.parse_args()

    order = [args.only] if args.only else list(VARIANTS)
    TMP.mkdir(exist_ok=True)
    logf = open(TMP / "board-qualify.log", "a")

    def log(m):
        line = f"[{now()}] {m}"
        print(line)
        logf.write(line + "\n")
        logf.flush()

    def build(variant):
        v = VARIANTS[variant]
        env = dict(os.environ)
        env["VYR_SPI_BR"] = "3"          # scene/strip drawn at the safe default
        env["VYR_MADCTL"] = f"{args.madctl:#04x}"
        if v["knob"] is not None:
            env["VYR_QUAL_FORCE_FAIL_HZ"] = str(v["knob"])
        else:
            env.pop("VYR_QUAL_FORCE_FAIL_HZ", None)
        cmd = ["cargo", "build", "-p", "vyr-size", "--target", TARGET,
               "--profile", PROFILE, "--no-default-features", "--features", FEATS]
        log(f"build [{variant}]: VYR_QUAL_FORCE_FAIL_HZ="
            f"{env.get('VYR_QUAL_FORCE_FAIL_HZ', '(unset)')} {' '.join(cmd)}")
        r = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True,
                           text=True, check=False)
        logf.write(r.stdout + r.stderr + "\n")
        logf.flush()
        if r.returncode != 0:
            log(f"*** BUILD FAILED [{variant}] -- see log ***")
            return None
        snap = TMP / f"board-qualify-{variant}.elf"
        shutil.copyfile(ELF, snap)
        digest = hashlib.sha256(snap.read_bytes()).hexdigest()
        log(f"snapshot {snap} (sha256:{digest[:16]}, {snap.stat().st_size:,} B)")
        return str(snap), digest

    def run_once(elf, semilog):
        args_pr = ["probe-rs", "run", "--chip", CHIP, "--probe", PROBE,
                   "--catch-hardfault", "--non-interactive",
                   "--disable-progressbars", elf]
        log("probe-rs: " + " ".join(args_pr))
        t0 = time.monotonic()
        lock = BoardLock(AGENT, log)
        try:
            lock.__enter__()
            try:
                p = subprocess.run(args_pr, cwd=REPO, capture_output=True,
                                   text=True, timeout=DEADLINE_S, check=False)
                out = p.stdout + p.stderr
            except subprocess.TimeoutExpired as e:
                def dec(x):
                    x = x or b""
                    return x.decode(errors="replace") if isinstance(x, bytes) else x
                out = dec(e.stdout) + dec(e.stderr)
                log(f"ERROR: target did not finish inside {DEADLINE_S}s -- killed")
        finally:
            lock.__exit__(None, None, None)
        Path(semilog).write_text(out)
        log(f"probe-rs wall {time.monotonic() - t0:.1f}s; semihosting -> {semilog}")
        return out

    def parse(text):
        def g(pat, cast=str, default=None):
            m = re.search(pat, text)
            return cast(m.group(1)) if m else default

        q = re.search(
            r"spirate qualify: chosen_br=(\d+) chosen_spi_hz=(\d+) "
            r"fastest_pass_br=(\d+) fastest_pass_spi_hz=(\d+) "
            r"margin=one-rung-slower floor_ok=(\w+) contiguous=(\w+) "
            r"conclusive=(\w+) trusted=(\w+) floor_failed=(\w+) "
            r"force_fail_hz=(\d+) status=(\w+) chosen_full_frame_ms=(\d+)", text)
        qualify = None
        if q:
            qualify = {
                "chosen_br": int(q.group(1)), "chosen_spi_hz": int(q.group(2)),
                "fastest_pass_br": int(q.group(3)),
                "fastest_pass_spi_hz": int(q.group(4)),
                "floor_ok": q.group(5) == "true",
                "contiguous": q.group(6) == "true",
                "conclusive": q.group(7) == "true",
                "trusted": q.group(8) == "true",
                "floor_failed": q.group(9) == "true",
                "force_fail_hz": int(q.group(10)),
                "status": q.group(11),
                "chosen_full_frame_ms": int(q.group(12)),
            }
        qf = re.search(
            r"spirate qualify-flush: flushing the real scene at the QUALIFIED "
            r"rate br=(\d+) spi_hz=(\d+)", text)
        # the flush cost at the qualified rate: the dma-overlap (or, under
        # spipio, pio) engine line for the chosen br.
        flush = []
        for m in re.finditer(
                r"spirate flush: engine=(\S+) br=(\d+) spi_hz=(\d+) bands=\d+ "
                r"wall_cyc=\d+ wall_ms=(\d+) render_cyc=\d+ render_ms=\d+ "
                r"blocked_cyc=\d+ blocked_ms=(\d+) cpu_cyc=\d+ cpu_ms=(\d+)",
                text):
            flush.append({"engine": m.group(1), "br": int(m.group(2)),
                          "spi_hz": int(m.group(3)), "wall_ms": int(m.group(4)),
                          "blocked_ms": int(m.group(5)), "cpu_ms": int(m.group(6))})
        return {
            "is_board_image": "REAL SILICON" in text,
            "qualify": qualify,
            "qualify_flush_br": int(qf.group(1)) if qf else None,
            "qualify_flush_spi_hz": int(qf.group(2)) if qf else None,
            "flush": flush,
            "highest_bit_exact_br": g(r"spirate summary: highest bit_exact br=(\d+)", int),
            "repeatable": g(r"spirate controls: repeatable=(\w+)") == "true",
            "sensitive": g(r"spirate controls: .*sensitive=(\w+)") == "true",
            "survived": g(r"spirate survival: .*match=(\w+)") == "true",
            "failsafe_alert": "FAIL-SAFE" in text,
            "inconclusive_alert": "INCONCLUSIVE" in text,
            "card_hash": g(r"testcard: path=lcd-spi-gram body=\d+x\d+ "
                           r"fnv1a=(0x[0-9a-f]{16})"),
            "frame_hash": g(r"\[vyr-size\] frame fnv1a=(0x[0-9a-f]{16})"),
            "workload_ok": "workload ok" in text,
            "hardfault": "cpu exception" in text or "HardFault" in text,
            "panic": "FATAL [vyr-size] panic" in text,
            "spirate_error": g(r"ERROR \[vyr-size\] spirate failed: (.+)"),
        }

    out = {
        "when": now(), "issue": 49, "features": FEATS,
        "board": "STM32F429I-DISC1 (STM32F429ZI, Cortex-M4F)",
        "panel": "ILI9341 240x320, SPI5 -> controller GRAM",
        "probe": PROBE, "pclk2_hz": PCLK2, "madctl": f"{args.madctl:#04x}",
        "margin_policy": "one rung slower than the fastest bit-exact rung, "
                         "floored at /16; non-contiguous or inconclusive -> floor",
        "reference_frame": REFERENCE_FRAME, "card_body": CARD_BODY,
        "ladder": [{"br": b, "divisor": 1 << (b + 1), "spi_hz": rate(b)}
                   for b in RUNGS],
        "variants": {},
    }
    rc = 0
    log("=" * 70)
    log(f"board-qualify: {CHIP} via ST-LINK {PROBE}, features={FEATS}")

    # --- phase 0: build all, prove the knob reaches distinct ELFs ------------
    builds = {}
    for v in order:
        b = build(v)
        if not b:
            logf.close()
            return 2
        builds[v] = {"elf": b[0], "sha256": b[1], "knob": VARIANTS[v]["knob"]}
    if len(order) > 1:
        digests = {builds[v]["sha256"] for v in order}
        out["knob_reaches_elf"] = {"distinct_hashes": len(digests),
                                   "ok": len(digests) == len(order)}
        log(f"VYR_QUAL_FORCE_FAIL_HZ reaches the image: {len(digests)} distinct "
            f"ELF hashes across {len(order)} variants -> "
            f"{out['knob_reaches_elf']['ok']}")
        if not out["knob_reaches_elf"]["ok"]:
            log("*** THE KNOB DID NOT CHANGE THE IMAGE -- refusing to report ***")
            rc = 6
    if args.params_only:
        Path(args.out).write_text(json.dumps(out, indent=2))
        log(f"wrote {args.out} (params-only)")
        logf.close()
        return rc

    # --- phase 1: flash each, one lock hold per flash ------------------------
    for v in order:
        want = VARIANTS[v]
        text = run_once(builds[v]["elf"], TMP / f"board-qualify-{v}-run.log")
        r = parse(text)
        entry = {"build": builds[v], "run": r, "want": want, "checks": {}}

        q = r["qualify"]
        c = entry["checks"]
        c["qualify_ran"] = q is not None
        c["controls_ok"] = bool(r["repeatable"] and r["sensitive"] and r["survived"])
        if q:
            c["chosen_br"] = (q["chosen_br"] == want["want_br"])
            c["status"] = (q["status"] == want["want_status"])
            c["fastest_pass_br"] = (q["fastest_pass_br"] == want["want_fastest_br"])
            if v == "normal":
                # the fastest passing rung must be the actual hardware ceiling
                c["hw_ceiling_matches"] = (
                    r["highest_bit_exact_br"] == q["fastest_pass_br"])
                # the real scene must have been flushed AT the chosen rate
                c["flushed_at_chosen"] = (r["qualify_flush_br"] == q["chosen_br"])
            if v == "marginal":
                # stepped DOWN relative to the honest run's /4 -> /8
                c["stepped_down"] = (q["chosen_br"] == 2 and q["fastest_pass_br"] == 1)
                c["flushed_at_chosen"] = (r["qualify_flush_br"] == q["chosen_br"])
            if v == "failsafe":
                c["failsafe_fired"] = r["failsafe_alert"]
                c["did_not_accelerate"] = (r["qualify_flush_br"] is None)
        # invariants: the renderer must be untouched in every image
        c["frame_unchanged"] = (r["frame_hash"] == REFERENCE_FRAME)
        c["card_unchanged"] = (r["card_hash"] == CARD_BODY)
        c["no_hardfault"] = not r["hardfault"] and not r["panic"]

        ok = all(c.values())
        entry["ok"] = ok
        out["variants"][v] = entry
        if not ok:
            rc = rc or 3

        if q:
            log(f"[{v}] chosen /{1 << (q['chosen_br'] + 1)} = {q['chosen_spi_hz']} "
                f"Hz (fastest bit-exact /{1 << (q['fastest_pass_br'] + 1)}), "
                f"status={q['status']}, floor_ok={q['floor_ok']}, "
                f"contiguous={q['contiguous']}, conclusive={q['conclusive']}, "
                f"full_frame={q['chosen_full_frame_ms']} ms")
        if r["qualify_flush_br"] is not None:
            fl = [x for x in r["flush"]
                  if x["br"] == r["qualify_flush_br"]]
            for x in fl:
                log(f"[{v}] flush engine={x['engine']:<12} at qualified rate: "
                    f"wall={x['wall_ms']} ms cpu={x['cpu_ms']} ms "
                    f"blocked={x['blocked_ms']} ms")
        log(f"[{v}] checks: " + " ".join(
            f"{k}={'OK' if val else 'FAIL'}" for k, val in c.items()))
        log(f"[{v}] frame={r['frame_hash']} (ref {REFERENCE_FRAME}) "
            f"card={r['card_hash']} (ref {CARD_BODY}) workload_ok={r['workload_ok']}")
        log(f"[{v}] -> {'PASS' if ok else 'FAIL'}")

    out["rc"] = rc
    out["all_pass"] = (rc == 0)
    out["human_confirmation"] = (
        "NOT MACHINE-VERIFIABLE. The read-back settles whether the BITS "
        "arrived bit-exact -- that is the qualifier's verdict and it is a "
        "machine one. Whether the whole image is right is a human reading of "
        "the labelled card; the identity strip names the qualified rate, so a "
        "photograph is self-describing.")
    Path(args.out).write_text(json.dumps(out, indent=2))
    log(f"wrote {args.out}")
    log(f"BOARD-QUALIFY: {'ALL PASS' if rc == 0 else 'FAILURES ABOVE'} (rc={rc})")
    logf.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())

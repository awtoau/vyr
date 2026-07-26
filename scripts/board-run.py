#!/usr/bin/env python3
"""board-run.py — build, flash and run the vyr-size workload on REAL SILICON
(STM32F429I-DISC1), capture the semihosting report, and emit JSON.

The F9 board half of the M4 story. Same shape as scripts/lvgl-m4-bench/run.py
(build -> run under a hard Python-side wall-clock guard -> parse -> JSON), but
the "machine" is an actual F429ZI reached over an ST-LINK/V2-1 rather than
qemu, and the timer is DWT_CYCCNT: REAL CPU cycles, including flash wait
states, the ART accelerator and bus contention, none of which emulation models.

What it verifies rather than assumes:
  * the firmware prints its own RCC/FLASH/PWR registers at boot; this script
    ASSERTS 5 flash wait states + prefetch + I-cache + D-cache + SYSCLK on the
    PLL, and marks the result `clock_ok: false` if any of that is not true.
  * the frame hash is compared against the cross-ISA reference, so a clock
    change that silently corrupted flash fetches cannot pass as a fast run.

Usage:
  python3 scripts/board-run.py                 # Exact tier, 1 run
  python3 scripts/board-run.py --draft         # Draft tier (--features draft)
  python3 scripts/board-run.py --repeat 5      # run-to-run spread
  python3 scripts/board-run.py --both --repeat 5

Output: tmp/board-result.json (+ tmp/board-<tier>.log per-run semihosting),
log tmp/board-run.log.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
TMP = os.path.join(REPO, "tmp")

TARGET = "thumbv7em-none-eabihf"
PROFILE = "release-mcu"
ELF = os.path.join(REPO, "target", TARGET, PROFILE, "vyr-size")

# --- the silicon target registry (#52) --------------------------------------
# One runner drives every board; only the map, clock, probe and clock-validity
# rule differ. `clock_ok(res)` takes the parsed boot registers and returns
# whether the part came up in the configuration the numbers claim. `ported` is
# False until a board has its own linker script + clock init — the runner then
# refuses it loudly (never a silent wrong flash) and points at its issue.
TARGETS = {
    "f429": {
        "board": "STM32F429I-DISC1 (STM32F429ZI, Cortex-M4F)",
        "chip": "STM32F429ZI",
        "probe": "0483:3752:0671FF484971754867174427",
        "core": "Cortex-M4F",
        "sysclk_hz": 192_000_000,
        "ported": True,
        # F4: PLL on, 5 flash wait states, ART prefetch + I-cache + D-cache.
        "clock_ok": lambda r: bool(
            r["on_pll"] and r["latency"] == 5
            and r["prften"] == 1 and r["icen"] == 1 and r["dcen"] == 1),
    },
    "f769": {
        "board": "STM32F769I-DISCO (STM32F769NI, Cortex-M7 @ 216 MHz)",
        "chip": "STM32F769NIHx",
        "probe": None,  # a second V2-1; fill the serial when connected
        "core": "Cortex-M7",
        "sysclk_hz": 216_000_000,
        "ported": False, "issue": 53,
        # M7: PLL on + L1 I/D cache enabled (icen/dcen) + its own flash latency.
        "clock_ok": lambda r: bool(r["on_pll"] and r["icen"] == 1 and r["dcen"] == 1),
    },
    "h750": {
        "board": "STM32H750B-DK (STM32H750XB, Cortex-M7 @ 480 MHz)",
        "chip": "STM32H750XBHx",
        "probe": "0483:374e:005600343431511837393330",
        "core": "Cortex-M7",
        "sysclk_hz": 480_000_000,
        "ported": False, "issue": 54,
        "clock_ok": lambda r: bool(r["on_pll"] and r["icen"] == 1 and r["dcen"] == 1),
    },
}

# Set from --target in main(); the module-level defaults keep the F429 the
# out-of-the-box board so `board-run.py` with no args behaves as it always did.
CHIP = TARGETS["f429"]["chip"]
PROBE = TARGETS["f429"]["probe"]
_CLOCK_OK = TARGETS["f429"]["clock_ok"]

# Wall-clock guard (Python-side deadline + kill, never a shell timeout). Flash
# of the ~200 KB image takes ~20 s over ST-LINK/V2-1 and the workload itself is
# a few seconds; 180 s is far past a healthy run and still kills a target that
# wedged in a bring-up spin.
DEADLINE_S = 180

# The cross-ISA references: x86-64, qemu-M4 and real silicon must all agree.
# Draft is a different rasteriser, so it has its own hash. Both were confirmed
# against the host leg of the same workload
# (`cargo run -p vyr-size --no-default-features --features run-qemu[,draft]`).
REFERENCE_HASH = {
    "Exact": "0x24dcaff531c6eb01",
    "Fast": "0x930d03610b07ea6f",
    "Draft": "0xf98cbbdddd6da1ba",
}


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg, logf):
    line = f"[{now()}] {msg}"
    print(line)
    logf.write(line + "\n")
    logf.flush()


TIER_FEATURE = {"exact": "board", "fast": "board,fast", "draft": "board,draft"}


def build(tier, logf, verify=False):
    """Build the tier and SNAPSHOT the ELF to a private path.

    cargo writes every feature combination of this bin to the SAME path
    (target/<triple>/release-mcu/vyr-size), so `./dev.py size` or `qemu-m4`
    running in another shell silently replaces it mid-batch. Measured: run 0
    of a batch flashed the board image and run 1, three minutes later, flashed
    a netduinoplus2 (`run-qemu`) image that had appeared at the same path — it
    then wedged, because probe-rs does not implement the SYS_CLOCK semihosting
    call that build uses. Copying to tmp/board-<tier>.elf makes every run in a
    batch flash provably the same bytes.
    """
    # #45: the PERF build (no `verify`) has no fold and gives DWT cycles but no
    # frame hash; the VERIFY build folds and proves the hash but is UNTIMED. The
    # board leg flashes both — cycles from perf, hash from verify.
    feats = TIER_FEATURE[tier]
    snap = tier
    if verify:
        feats += ",verify"
        snap += "-verify"
    cmd = [
        "cargo", "build", "-p", "vyr-size",
        "--target", TARGET, "--profile", PROFILE,
        "--no-default-features", "--features", feats,
    ]
    log(f"build: {' '.join(cmd)}", logf)
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    logf.write(r.stdout + r.stderr + "\n")
    logf.flush()
    if r.returncode != 0:
        log("BUILD FAILED — see log", logf)
        return None
    snapshot = os.path.join(TMP, f"board-{snap}.elf")
    shutil.copyfile(ELF, snapshot)
    with open(ELF, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    log(f"snapshot {snapshot} (sha256:{digest[:16]})", logf)
    return snapshot, digest


def run_once(elf, logf, semilog):
    """Flash + run + capture semihosting. Returns the captured text or None."""
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
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"")
        err = (e.stderr or b"")
        out = out.decode(errors="replace") if isinstance(out, bytes) else out
        err = err.decode(errors="replace") if isinstance(err, bytes) else err
        with open(semilog, "w") as f:
            f.write(out + err)
        log(f"ERROR: target did not finish within the {DEADLINE_S}s guard — "
            f"killed. Partial output in {semilog}", logf)
        return None, None
    wall = time.monotonic() - t0
    out = p.stdout + p.stderr
    with open(semilog, "w") as f:
        f.write(out)
    log(f"probe-rs wall {wall:.1f}s rc={p.returncode}; semihosting -> {semilog}",
        logf)
    return out, wall


def parse(text, logf):
    def g(pat, cast=str):
        m = re.search(pat, text)
        return cast(m.group(1)) if m else None

    res = {
        "sysclk_hz": g(r"sysclk_hz=(\d+)", int),
        # The fallback description contains commas, so match up to the fixed
        # tail rather than to the first comma.
        "clock_source": g(r"src=(.+?), crt0 \+ FPU"),
        "rcc_cr": g(r"RCC_CR=(0x[0-9a-f]+)"),
        "rcc_pllcfgr": g(r"RCC_PLLCFGR=(0x[0-9a-f]+)"),
        "rcc_cfgr": g(r"RCC_CFGR=(0x[0-9a-f]+)"),
        "flash_acr": g(r"FLASH_ACR=(0x[0-9a-f]+)"),
        "pwr_csr": g(r"PWR_CSR=(0x[0-9a-f]+)"),
        "latency": g(r"latency=(\d+)", int),
        "prften": g(r"prften=(\d+)", int),
        "icen": g(r"icen=(\d+)", int),
        "dcen": g(r"dcen=(\d+)", int),
        "on_pll": g(r"on_pll=(\w+)") == "true",
        "quality": g(r"quality=(\w+)"),
        "frame_hash": g(r"frame fnv1a=(0x[0-9a-f]{16})"),
        "bands": g(r"bands=(\d+)", int),
        "pixels_written": g(r"pixels_written=(\d+)", int),
        "timed_frames": g(r"timed: (\d+) warmed frames", int),
        # Accept both wordings of the timed line. With --features board the
        # clock IS DWT_CYCCNT whatever the label says, so a firmware built from
        # a workload.rs that still prints the qemu wording ("... cs virtual")
        # is still reporting cycles — and prints them SIGNED, which overflows
        # negative past 2^31 (~2.1e9, i.e. any Exact run). Normalised below.
        "timed_cycles": g(r"warmed frames in (-?\d+) c", int),
        "heap_peak": g(r"workload ok: heap peak=(\d+)", int),
        "ok": "workload ok" in text,
        # Identity of the image that actually ran. A `run-qemu` build prints
        # "netduinoplus2" here and uses a semihosting call probe-rs does not
        # implement; catching it explicitly beats debugging the silence.
        "is_board_image": "REAL SILICON" in text,
    }
    if not res["is_board_image"]:
        res["ok"] = False
    # Normalise the timed delta to unsigned 32-bit and derive cycles/frame
    # here rather than trusting the firmware's own division — one place, one
    # rule, and it survives either wording of the line.
    if res["timed_cycles"] is not None and res["timed_cycles"] < 0:
        res["timed_cycles"] += 1 << 32
    n = res["timed_frames"]
    res["cycles_per_frame"] = (
        res["timed_cycles"] // n if res["timed_cycles"] and n else None
    )
    # The clock-validity rule is the selected target's (#52): the F4 checks ART
    # + 5 wait states + PLL; an M7 checks its L1 cache-enable + PLL. A run in the
    # wrong configuration is not the configuration being reported.
    res["clock_ok"] = _CLOCK_OK(res)
    return res


def one_tier(tier, repeat, logf):
    built = build(tier, logf)
    if not built:
        return None
    elf, elf_sha = built
    runs = []
    for i in range(repeat):
        semilog = os.path.join(TMP, f"board-{tier}-{i}.log")
        text, wall = run_once(elf, logf, semilog)
        if text is None:
            log(f"{tier} run {i}: NO OUTPUT (guard fired)", logf)
            runs.append({"ok": False, "error": "timeout"})
            continue
        r = parse(text, logf)
        r["wall_s"] = round(wall, 1)
        r["semihosting_log"] = semilog
        if not r["is_board_image"]:
            log(f"{tier} run {i}: WRONG IMAGE — the running firmware is not a "
                f"`board` build (no 'REAL SILICON' boot line). See {semilog}",
                logf)
        elif not r["ok"]:
            log(f"{tier} run {i}: target did not report 'workload ok' — "
                f"see {semilog}", logf)
        log(f"{tier} run {i}: sysclk={r['sysclk_hz']} src={r['clock_source']} "
            f"clock_ok={r['clock_ok']} hash={r['frame_hash']} "
            f"cycles/frame={r['cycles_per_frame']} heap_peak={r['heap_peak']}",
            logf)
        runs.append(r)

    good = [r for r in runs if r.get("ok") and r.get("cycles_per_frame")]
    cyc = [r["cycles_per_frame"] for r in good]

    # #45: the frame hash is the VERIFY build's product — flash one verify image
    # (untimed: it proves the pixels, it does not measure cycles). The cross-ISA
    # gate below then compares THIS hash, not the perf build's (which has none).
    verify_hash = None
    vbuilt = build(tier, logf, verify=True)
    if vbuilt:
        vsemi = os.path.join(TMP, f"board-{tier}-verify.log")
        vtext, _ = run_once(vbuilt[0], logf, vsemi)
        if vtext:
            verify_hash = parse(vtext, logf).get("frame_hash")
        log(f"{tier} verify: frame hash {verify_hash} (from the --features verify image)",
            logf)
    summary = {
        "tier": tier.capitalize(),
        "elf": elf,
        "elf_sha256": elf_sha,
        "runs": runs,
        "n_ok": len(good),
        "cycles_per_frame": {
            "min": min(cyc) if cyc else None,
            "max": max(cyc) if cyc else None,
            "median": int(statistics.median(cyc)) if cyc else None,
            "spread_ppm": (
                int((max(cyc) - min(cyc)) / statistics.median(cyc) * 1e6)
                if cyc and statistics.median(cyc) else None
            ),
            "all": cyc,
        },
        # #45: the hash comes from the verify image, not the perf runs.
        "frame_hashes": [verify_hash] if verify_hash else [],
        "verify_frame_hash": verify_hash,
        "clock_ok_all": all(r.get("clock_ok") for r in good) and bool(good),
    }
    ref = REFERENCE_HASH[summary["tier"]]
    summary["reference_hash"] = ref
    summary["hash_matches_reference"] = summary["frame_hashes"] == [ref]
    if good:
        g0 = good[0]
        summary["clock"] = {
            "sysclk_hz": g0["sysclk_hz"],
            "source": g0["clock_source"],
            "RCC_CR": g0["rcc_cr"],
            "RCC_PLLCFGR": g0["rcc_pllcfgr"],
            "RCC_CFGR": g0["rcc_cfgr"],
            "FLASH_ACR": g0["flash_acr"],
            "PWR_CSR": g0["pwr_csr"],
            "flash_wait_states": g0["latency"],
            "art_prefetch": g0["prften"],
            "art_icache": g0["icen"],
            "art_dcache": g0["dcen"],
        }
        summary["heap_peak"] = g0["heap_peak"]
        summary["timed_frames"] = g0["timed_frames"]
        if g0["cycles_per_frame"] and g0["sysclk_hz"]:
            summary["ms_per_frame"] = round(
                g0["cycles_per_frame"] / g0["sysclk_hz"] * 1e3, 3)
            summary["ms_per_frame_note"] = (
                "cycles / NOMINAL sysclk. The MEASUREMENT is the cycle count; "
                "the millisecond figure inherits the crystal's accuracy. "
                "scripts/board-diag.py gates DWT_CYCCNT against host wall time "
                "and brackets the real core clock."
            )
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="f429", choices=sorted(TARGETS),
                    help="which silicon board (registry #52). Default f429; an "
                         "unported target refuses with its bring-up issue.")
    ap.add_argument("--draft", action="store_true",
                    help="build/run the Draft quality tier (--features draft)")
    ap.add_argument("--fast", action="store_true",
                    help="build/run the Fast quality tier (#27, --features fast)")
    ap.add_argument("--both", action="store_true",
                    help="run Exact then Draft")
    ap.add_argument("--all", action="store_true",
                    help="run every tier: Exact, Fast, Draft")
    ap.add_argument("--repeat", type=int, default=1,
                    help="runs per tier (>=3 to see run-to-run spread)")
    ap.add_argument("--out", default=os.path.join(TMP, "board-result.json"))
    args = ap.parse_args()

    global CHIP, PROBE, _CLOCK_OK
    tgt = TARGETS[args.target]
    if not tgt["ported"]:
        print(f"board-run: target '{args.target}' ({tgt['board']}) is not yet "
              f"ported — needs its linker script + clock init. See issue "
              f"#{tgt.get('issue')}. Refusing rather than flash a wrong image.")
        return 5
    if not tgt["probe"]:
        print(f"board-run: target '{args.target}' has no probe serial registered "
              f"— connect the board and add it to TARGETS. Refusing.")
        return 5
    CHIP, PROBE, _CLOCK_OK = tgt["chip"], tgt["probe"], tgt["clock_ok"]

    os.makedirs(TMP, exist_ok=True)
    with open(os.path.join(TMP, "board-run.log"), "a") as logf:
        log("=" * 70, logf)
        log(f"board-run: target={args.target} {tgt['board']} via ST-LINK {PROBE}, "
            f"repeat={args.repeat}", logf)
        if args.all:
            tiers = ["exact", "fast", "draft"]
        elif args.both:
            tiers = ["exact", "draft"]
        elif args.fast:
            tiers = ["fast"]
        elif args.draft:
            tiers = ["draft"]
        else:
            tiers = ["exact"]
        out = {
            "when": now(),
            "target": args.target,
            "board": tgt["board"],
            "core": tgt["core"],
            "nominal_sysclk_hz": tgt["sysclk_hz"],
            "probe": PROBE,
            "chip": CHIP,
            "timer": "DWT_CYCCNT (real CPU cycles)",
            "tiers": {},
        }
        rc = 0
        for tier in tiers:
            s = one_tier(tier, args.repeat, logf)
            if s is None:
                rc = 2
                continue
            out["tiers"][s["tier"]] = s
            c = s["cycles_per_frame"]
            log(f"RESULT {s['tier']}: {c['median']} cycles/frame "
                f"(min {c['min']} max {c['max']}, spread {c['spread_ppm']} ppm "
                f"over {s['n_ok']} runs) @ {s.get('clock',{}).get('sysclk_hz')} Hz "
                f"= {s.get('ms_per_frame')} ms/frame", logf)
            log(f"RESULT {s['tier']}: frame hash(es) {s['frame_hashes']}", logf)
            if not s["clock_ok_all"]:
                log(f"*** {s['tier']}: CLOCK NOT AS SPECIFIED (PLL/5WS/ART) — "
                    f"these cycles are NOT the 192 MHz + ART configuration ***",
                    logf)
                rc = 3
            if not s["hash_matches_reference"]:
                log(f"*** {s['tier']}: FRAME HASH MISMATCH vs "
                    f"{s['reference_hash']} — cross-ISA determinism broken ***",
                    logf)
                rc = 4
        with open(args.out, "w") as f:
            json.dump(out, f, indent=2)
        log(f"wrote {args.out}", logf)
        return rc


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""check-f64.py — gate the MCU build against double-precision creep (#39).

A Cortex-M4F has a SINGLE-precision FPU: every `f64` op is a soft-float library
call. #32 was exactly this — libm's f32 kernels evaluating in f64 cost 11.4 M
insns/frame (17.7% of Exact) and lived for months because it is INVISIBLE on the
host (x86-64 has hardware f64) and `./dev.py check-mcu` only proves the crate
COMPILES. This gate makes the next one fail automatically.

It builds the measurement firmware UNSTRIPPED (the shipping `release-mcu`
codegen, `strip=false` so symbols survive — the same trick m4-attribute.py uses),
lists the f64 runtime helpers the linker pulled in, and compares that SET against
a committed baseline (`vyr-size/f64-baseline.json`). A helper the baseline does
not list is a REGRESSION: a stray `as f64`, an untyped float literal that
defaulted to f64, or a new libm call whose kernel promotes — each silently
thousands of insns/call. Fewer helpers than the baseline is an improvement; re-
bless with --bless.

This is the fast CI guard (symbol presence). The deeper question — is a linked
helper actually CALLED during a render, and how much — is answered by
scripts/m4-attribute.py (per-symbol instruction attribution under plugin QEMU).

Usage:  python3 scripts/check-f64.py [--bless]
Log:    tmp/check-f64.log
"""

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
BASELINE = REPO / "vyr-size" / "f64-baseline.json"
TARGET = "thumbv7em-none-eabihf"
NM_CANDIDATES = [
    "/opt/arm-gnu-toolchain/arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi/bin/arm-none-eabi-nm",
    "arm-none-eabi-nm", "llvm-nm", "nm",
]
# The measurement firmware: the full float render path (Exact tier) plus the
# semihosting report. #32 lived in the Exact trig path, so this is the build to
# watch; a new f64 helper anywhere in it is the signal.
FEATURES = "run-qemu"

# What counts as a double-precision runtime helper. Anchored so a hash suffix
# that merely contains "df3" (e.g. ...17hd8f0771df36b898e) never matches.
F64_PATTERNS = [
    re.compile(r"^__aeabi_d"),                       # dadd dsub dmul ddiv dcmp* d2f d2iz d2uiz
    re.compile(r"^__aeabi_[a-z0-9]+2d$"),            # i2d ui2d ul2d f2d
    re.compile(r"^__(add|sub|mul|div|neg)df3$"),     # C-style compiler-rt
    re.compile(r"^__(extendsfdf2|truncdfsf2|fixdfsi|floatsidf)$"),
    re.compile(r"___(add|sub|mul|div|neg|extend|trunc)[a-z]*df[a-z0-9]*$"),  # rust-mangled
]


def log(msg: str, lines: list) -> None:
    print(msg, flush=True)
    lines.append(msg)


def which_nm() -> str:
    for c in NM_CANDIDATES:
        r = subprocess.run(["bash", "-lc", f"command -v {c} || true"],
                           capture_output=True, text=True)
        if r.stdout.strip():
            return r.stdout.strip()
    raise SystemExit("check-f64: no nm found (need arm-none-eabi-nm / llvm-nm / nm)")


def canonical(sym: str) -> str:
    """Rust mangles compiler_builtins as _RNvNt…___adddf3; the trailing
    ___<name>df<n> is the stable identity. ARM EABI names are already stable."""
    m = re.search(r"(___[a-z]+df[a-z0-9]*)$", sym)
    return m.group(1) if m else sym


def f64_helpers(elf: Path, nm: str) -> set:
    out = subprocess.run([nm, str(elf)], capture_output=True, text=True)
    found = set()
    for line in out.stdout.splitlines():
        name = line.split()[-1] if line.split() else ""
        if any(p.search(name) for p in F64_PATTERNS):
            found.add(canonical(name))
    return found


def main() -> int:
    lines: list = []
    bless = "--bless" in sys.argv
    TMP.mkdir(exist_ok=True)

    nm = which_nm()
    log(f"check-f64: nm={nm}", lines)
    log(f"check-f64: building UNSTRIPPED release-mcu --features {FEATURES}", lines)
    build = subprocess.run(
        ["cargo", "build", "-q", "-p", "vyr-size", "--target", TARGET,
         "--profile", "release-mcu", "--no-default-features", "--features", FEATURES,
         "--config", "profile.release-mcu.strip=false"],
        cwd=REPO, capture_output=True, text=True)
    if build.returncode != 0:
        log("check-f64: BUILD FAILED\n" + build.stderr[-800:], lines)
        (TMP / "check-f64.log").write_text("\n".join(lines) + "\n")
        return 2
    elf = REPO / "target" / TARGET / "release-mcu" / "vyr-size"

    found = f64_helpers(elf, nm)
    log(f"check-f64: {len(found)} f64 runtime helper(s) linked:", lines)
    for s in sorted(found):
        log(f"    {s}", lines)

    if bless:
        BASELINE.write_text(json.dumps(
            {"features": FEATURES,
             "note": "f64 soft-float runtime helpers linked into the unstripped "
                     "release-mcu build (#39). A helper NOT in this set is a "
                     "regression — soft-f64 on the M4F's single-precision FPU. "
                     "Deeper hot-path check: scripts/m4-attribute.py.",
             "helpers": sorted(found)},
            indent=2) + "\n")
        log(f"check-f64: BLESSED {len(found)} helper(s) -> {BASELINE.name} "
            "(a reviewed act — commit it separately)", lines)
        (TMP / "check-f64.log").write_text("\n".join(lines) + "\n")
        return 0

    if not BASELINE.exists():
        log(f"check-f64: no baseline at {BASELINE} — run with --bless first", lines)
        (TMP / "check-f64.log").write_text("\n".join(lines) + "\n")
        return 2
    baseline = set(json.loads(BASELINE.read_text())["helpers"])
    added = found - baseline
    removed = baseline - found

    rc = 0
    if added:
        log("", lines)
        log(f"check-f64: FAIL — {len(added)} NEW f64 helper(s) not in the baseline:", lines)
        for s in sorted(added):
            log(f"    + {s}", lines)
        log("  A double-precision op reached the MCU build. On the M4F's "
            "single-precision FPU each is a soft-float call — thousands of insns "
            "each (#32). Find it (an `as f64`, an untyped float literal, a new "
            "libm call whose kernel promotes), fix it, or if truly unavoidable "
            "re-bless with --bless as a reviewed change.", lines)
        rc = 1
    if removed:
        log(f"check-f64: {len(removed)} helper(s) GONE vs baseline (an improvement — "
            f"re-bless to lock it in): {sorted(removed)}", lines)
    if rc == 0 and not removed:
        log(f"check-f64: OK — the f64 helper set matches the baseline "
            f"({len(found)} helpers)", lines)
    (TMP / "check-f64.log").write_text("\n".join(lines) + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())

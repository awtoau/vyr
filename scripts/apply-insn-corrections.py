#!/usr/bin/env python3
"""Replace the guessed M4 instruction anchors with the plugin-measured counts.

`dev.py` prices every F16 gap-recovery percentage against two hardcoded
literals. Both were derived from qemu SYS_CLOCK, which on a QEMU built without
TCG plugins reports HOST WALL TIME, not instructions (measured: the identical
workload moved 39 -> 58 cs purely from host CPU load). Exact counts now exist,
from a QEMU built with `--enable-plugins` and the `libinsn` TCG plugin:

    vyr Exact  75,000,000 (anchor)  ->  64,178,227   old figure +16.9% high
    LVGL       10,000,000 (anchor)  ->   9,220,422   old figure  +8.5% high

Each was verified three ways: bit-identical across 3 idle + 3 host-loaded runs;
a doubled-frame-count slope with remainder exactly 0; and a fixed window
overhead of only 7-11 instructions (the clock-read call itself).

Idempotent — re-running after a successful pass is a no-op. Verifies by
re-reading the file and printing the resulting gap arithmetic.

Logs to tmp/apply-insn-corrections.log.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEVPY = REPO / "dev.py"
TMP = REPO / "tmp"
LOG = TMP / "apply-insn-corrections.log"

MEASURED = {
    "QEMU_M4_EXACT_INSNS": ("vyr-exact", 64_178_227, 75_000_000),
    "QEMU_M4_LVGL_INSNS": ("lvgl", 9_220_422, 10_000_000),
}
DRAFT_JSON = "vyr-draft"

_lines: list[str] = []


def log(m: str = "") -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"{stamp} UTC  [insn-fix] {m}" if m else ""
    print(line)
    _lines.append(line)


def load_measured(name: str) -> dict | None:
    p = TMP / f"qemu-insn-{name}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def main() -> int:
    TMP.mkdir(exist_ok=True)
    log("verifying the measured JSON before touching dev.py")

    # 1. Re-derive every number from the result files rather than trusting the
    #    constants above — if the JSON disagrees, refuse to edit.
    resolved: dict[str, int] = {}
    ok = True
    for const, (name, expect, old) in MEASURED.items():
        d = load_measured(name)
        if d is None:
            log(f"  MISSING tmp/qemu-insn-{name}.json — cannot verify {const}")
            ok = False
            continue
        got = d["insns_per_frame"]
        det = d.get("deterministic")
        windows = {r["timed_window_insns"] for r in d["runs"]}
        loaded = load_measured(f"{name}-loaded")
        lwin = {r["timed_window_insns"] for r in loaded["runs"]} if loaded else set()
        agree = got == expect and det and len(windows) == 1 and (not lwin or windows == lwin)
        log(
            f"  {const:24} measured={got:>12,}  deterministic={det}  "
            f"idle_windows={len(windows)}  load_windows={len(lwin) or '-'}  "
            f"{'OK' if agree else 'MISMATCH'}"
        )
        if not agree:
            ok = False
        resolved[const] = got

    if not ok:
        log("REFUSING to edit dev.py — verification failed above")
        LOG.write_text("\n".join(_lines) + "\n")
        return 1

    # 2. Rewrite the two literals.
    src = DEVPY.read_text()
    before = src
    for const, value in resolved.items():
        pat = re.compile(rf"^({re.escape(const)}\s*=\s*)([0-9_]+)", re.M)
        m = pat.search(src)
        if not m:
            log(f"  {const}: NOT FOUND in dev.py — skipping")
            continue
        cur = int(m.group(2).replace("_", ""))
        if cur == value:
            log(f"  {const}: already {value:,} — no change")
            continue
        src = pat.sub(rf"\g<1>{value:_}", src, count=1)
        log(f"  {const}: {cur:,} -> {value:,}")

    # 3. Correct the comment that asserts these are measured anchors.
    stale = (
        "# f9-static.md, \"~75.0 M insns/frame\"); the LVGL figure is the ~10 M\n"
        "# insns/frame the M4 benchmark measured for an LVGL equivalent (the 7.4x\n"
        "# per-pixel gap that motivated F16). Both are anchors for the recovered-gap %,\n"
        "# not measured by this run — Draft's own insns/frame IS what the run measures."
    )
    fixed = (
        "# scripts/qemu-insn.py). BOTH are now EXACT instruction counts from a QEMU\n"
        "# built with --enable-plugins + the libinsn TCG plugin, verified\n"
        "# bit-identical across idle and host-loaded runs. They replace the previous\n"
        "# SYS_CLOCK-derived guesses (75 M / 10 M), which were host WALL TIME and read\n"
        "# 16.9% / 8.5% high. Both are anchors for the recovered-gap %, not measured by\n"
        "# this run — Draft's own insns/frame IS what the run measures."
    )
    if stale in src:
        src = src.replace(stale, fixed)
        log("  corrected the anchor provenance comment")

    if src != before:
        DEVPY.write_text(src)
        log("wrote dev.py")
    else:
        log("dev.py already current — nothing written")

    # 4. Report the resulting arithmetic, including Draft.
    draft = load_measured(DRAFT_JSON)
    if draft:
        d = draft["insns_per_frame"]
        e = resolved["QEMU_M4_EXACT_INSNS"]
        l = resolved["QEMU_M4_LVGL_INSNS"]
        gap = e - l
        recovered = (e - d) / gap * 100 if gap else float("nan")
        log()
        log("resulting F16 arithmetic, on exact counts:")
        log(f"  vyr Exact  {e:>12,}  insns/frame")
        log(f"  vyr Draft  {d:>12,}  insns/frame   ({e/d:.2f}x cheaper than Exact)")
        log(f"  LVGL       {l:>12,}  insns/frame")
        log(f"  Draft vs LVGL: {d/l:.4f}x — vyr {'cheaper' if d<l else 'DEARER'} "
            f"by {abs(100*(1-d/l)):.2f}%")
        log(f"  Draft recovers {recovered:.1f}% of the Exact->LVGL gap")

    # 5. dev.py must still parse and self-describe.
    r = subprocess.run(
        ["python3", str(DEVPY), "describe"], cwd=REPO, capture_output=True, text=True
    )
    log()
    log(f"./dev.py describe rc={r.returncode}" + ("" if r.returncode == 0 else f" — {r.stderr[:200]}"))

    LOG.write_text("\n".join(_lines) + "\n")
    return 0 if r.returncode == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

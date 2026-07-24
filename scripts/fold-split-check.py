#!/usr/bin/env python3
"""#44 verification: measure render_only / total / fold on BOTH harnesses.

Runs the LVGL anchor and all three vyr tiers under qemu with the hash fold OUT
of the timed window, at a frame count high enough that the 1 cs SYS_CLOCK
quantization is small against the differences being published.

Why 400 frames: SYS_CLOCK resolution is 1 cs (10^7 insns). At 40 frames one cs
is 250,000 insns/frame — ~6% of LVGL's render_only, the same order as the
vyr-vs-LVGL gap itself. At 400 it is 25,000/frame, ~0.6%, which is below every
difference this comparison reports.

Log: ./tmp/fold-split-check.log
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp" / "fold-split-check.log"
FRAMES = 400


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def run(cmd: list[str]) -> str:
    log("$ " + " ".join(cmd))
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        log(f"rc={p.returncode}\n{p.stdout[-3000:]}\n{p.stderr[-2000:]}")
        sys.exit(1)
    return p.stdout + p.stderr


def main() -> None:
    LOG.write_text("")
    results: dict[str, dict] = {}

    # --- LVGL anchor ------------------------------------------------------
    # Built by its own runner, then measured with the SAME instrument as vyr.
    # run.py's own figure comes from SYS_CLOCK on the system qemu, which is
    # host wall time on a build without TCG plugins — error 1 in
    # docs/measurements/perf-history.md. Using libinsn on both sides is the
    # only way the anchor and the thing it anchors are commensurable.
    run([sys.executable, "scripts/lvgl-m4-bench/run.py", "--frames", str(FRAMES)])
    # The default (published) build writes exactly this name; suffixed names
    # are non-default --opt runs and must not be picked up by accident.
    lvgl_elf = ROOT / "tmp" / "lvgl-m4.elf"
    if not lvgl_elf.exists():
        log(f"ERROR: {lvgl_elf} missing after the LVGL build")
        sys.exit(1)
    run([sys.executable, "scripts/qemu-insn.py", str(lvgl_elf), "--name", "fold44-lvgl"])
    j = json.loads((ROOT / "tmp" / "qemu-insn-fold44-lvgl.json").read_text())
    results["lvgl"] = {
        "render_only": j.get("render_only_insns_per_frame"),
        "total": j.get("with_fold_insns_per_frame"),
        "fold": j.get("fold_insns_per_frame"),
        "hash": j.get("frame_hash"),
    }
    log(f"LVGL: {results['lvgl']}")

    # --- vyr, all three tiers ---------------------------------------------
    # The instrument is qemu-insn.py (TCG libinsn plugin = exact instruction
    # counts), NOT SYS_CLOCK: on a qemu built without plugins SYS_CLOCK is host
    # wall time, which is error 1 in docs/measurements/perf-history.md.
    for tier, feats in (("Exact", "run-qemu"),
                        ("Fast", "run-qemu,fast"),
                        ("Draft", "run-qemu,draft")):
        run(["cargo", "build", "-q", "-p", "vyr-size",
             "--target", "thumbv7em-none-eabihf", "--profile", "release-mcu",
             "--no-default-features", "--features", feats])
        elf = "target/thumbv7em-none-eabihf/release-mcu/vyr-size"
        slug = f"fold44-{tier.lower()}"
        run([sys.executable, "scripts/qemu-insn.py", elf, "--name", slug])
        j = json.loads((ROOT / "tmp" / f"qemu-insn-{slug}.json").read_text())
        results[tier] = {
            "render_only": j.get("render_only_insns_per_frame"),
            "total": j.get("with_fold_insns_per_frame"),
            "fold": j.get("fold_insns_per_frame"),
            "hash": j.get("frame_hash"),
        }
        log(f"{tier}: {results[tier]}")

    # --- the re-derived comparison ----------------------------------------
    anchor = results["lvgl"]["render_only"]
    log("")
    log("=== #44: comparison re-derived from render_only ===")
    log(f"{'cell':<8} {'render_only':>13} {'total':>13} {'fold':>13} {'fold%':>7} {'vs LVGL':>10}")
    for k, v in results.items():
        share = 100.0 * v["fold"] / v["total"] if v["total"] else 0.0
        rel = f"{v['render_only'] / anchor:.2f}x" if anchor else "-"
        log(f"{k:<8} {v['render_only']:>13,} {v['total']:>13,} {v['fold']:>13,} "
            f"{share:>6.1f}% {rel:>10}")

    (ROOT / "tmp" / "fold-split-result.json").write_text(json.dumps(results, indent=2))
    log("wrote tmp/fold-split-result.json")


if __name__ == "__main__":
    main()

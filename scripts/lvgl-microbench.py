#!/usr/bin/env python3
"""lvgl-microbench.py — per-primitive vyr-vs-LVGL ratio, plugin-exact.

Populates the `lvgl_insns` / `lvgl_ratio` columns of tmp/microbench.db for the
handful of showcase primitives that have an unambiguous LVGL equivalent
(border, ring, line). The comparison is deliberately the cleanest possible:

  * BOTH sides measured with the SAME instrument — plugin QEMU + libinsn,
    `match=bkpt,trace=on`, exact architectural insns (never SYS_CLOCK, which
    lvgl-gap.md §5 shows is wall-influenced on a plugin-less qemu).
  * BOTH sides reported as insns ABOVE THE BACKGROUND-ONLY NULL scene, so the
    per-frame boot/flush overhead cancels and what remains is the primitive.
  * No pixels in the denominator — the two renderers count painted pixels
    differently (vyr: painted; LVGL: flushed), so insns/px is not comparable.
    insns-to-draw-the-shape is.

`docs/measurements/lvgl-gap.md` records FOUR past vyr-vs-LVGL measurement
errors, every one flattering vyr. So this refuses to write a number until the
LVGL null scene's per-frame cost sanity-checks against the published whole-scene
anchor: a per-primitive figure larger than the whole fixture would be error #5,
and is rejected loudly rather than stored.

Content matching is imperfect by construction (LVGL's arc sits half a pixel
inward, its border/rounding maths differ) — the ratio is INDICATIVE, and the
page labels it so.

Output: tmp/microbench.db (updated) + tmp/lvgl-microbench.log
Usage:  python3 scripts/lvgl-microbench.py [--db tmp/microbench.db]
                                           [--frames 40] [--tier exact]
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
QEMU_BUILD = Path(os.environ.get("VYR_QEMU_BUILD", "/mnt/2tb/git_debris/qemu-plugins-build"))
QEMU = QEMU_BUILD / "qemu-system-arm"
INSN = QEMU_BUILD / "tests" / "tcg" / "plugins" / "libinsn.so"
LVGL_RUN = REPO / "scripts" / "lvgl-m4-bench" / "run.py"
MACHINE = "netduinoplus2"
DEADLINE = 1200

# lvgl LV_PROBE value -> (vyr showcase point name, human label)
MAP = {1: ("border", "border"), 2: ("gauge", "ring"), 3: ("line_h", "line")}
# The published whole-scene LVGL render-only anchor (lvgl-gap.md, plugin-exact,
# release, -Os). A per-primitive figure must be WELL under this or it is wrong.
ANCHOR_WHOLE_SCENE = 7_112_541

_lines: list[str] = []


def log(m: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {m}"
    print(line, flush=True)
    _lines.append(line)
    (TMP).mkdir(exist_ok=True)
    (TMP / "lvgl-microbench.log").write_text("\n".join(_lines) + "\n")


def largest_window(elf: Path, tag: str) -> int | None:
    """insns in the biggest bkpt-bracketed window of an ELF — the timed frame
    loop (both the vyr probe and the LVGL harness bracket it with bkpt)."""
    plog = TMP / f"lvglmb-{tag}.log"
    plog.unlink(missing_ok=True)
    args = [str(QEMU), "-machine", MACHINE, "-nographic",
            "-semihosting-config", "enable=on,target=native",
            "-icount", "shift=0,sleep=off",
            "-plugin", f"{INSN},match=bkpt,trace=on",
            "-d", "plugin", "-D", str(plog), "-kernel", str(elf)]
    g = subprocess.run(args, capture_output=True, text=True, cwd=REPO, timeout=DEADLINE)
    if g.returncode != 0:
        log(f"  {tag}: guest rc={g.returncode}: {(g.stdout + g.stderr)[-300:]}")
        return None
    deltas = [int(d) for d in re.findall(r"Δ\+(\d+) since last match", plog.read_text())]
    if not deltas:
        log(f"  {tag}: no bkpt deltas (qemu without capstone?)")
        return None
    # LVGL harness: TIMED_FRAMES frames in one window -> divide. The window is
    # the largest delta; frames from the guest output.
    frames = 1
    m = re.search(r"timed_frames=\D*(\d+)", g.stdout + g.stderr)
    if m:
        frames = int(m.group(1))
    return max(deltas) // frames


def lvgl_frame(probe: int, frames: int) -> int | None:
    """Build LVGL with one primitive (main.c LV_PROBE) and plugin-price it."""
    r = subprocess.run([sys.executable, str(LVGL_RUN), "--lv-probe", str(probe),
                        "--frames", str(frames)], cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  lvgl build/run probe={probe} FAILED: {(r.stdout + r.stderr)[-400:]}")
        return None
    elf = TMP / f"lvgl-m4-probe{probe}.elf"
    if not elf.is_file():
        log(f"  lvgl probe={probe}: no ELF at {elf}")
        return None
    return largest_window(elf, f"lvgl{probe}")


def vyr_above_null(name: str, tier: str) -> int | None:
    """Build the vyr probe isolated to [null, name] and return the point's
    render insns minus null's, both from the same boot."""
    feat = {"exact": "run-qemu,probe", "fast": "run-qemu,probe,fast",
            "draft": "run-qemu,probe,draft"}[tier]
    env = {**os.environ, "CARGO_INCREMENTAL": "0", "VYR_PROBE_POINT": name,
           "CARGO_TARGET_DIR": str(TMP / "lvglmb-vyrtarget")}
    b = subprocess.run(["cargo", "build", "--profile", "release-mcu", "-p", "vyr-size",
                        "--target", "thumbv7em-none-eabihf", "--no-default-features",
                        "--features", feat], cwd=REPO, capture_output=True, text=True, env=env)
    if b.returncode != 0:
        log(f"  vyr build {name} FAILED: {(b.stdout + b.stderr)[-400:]}")
        return None
    elf = Path(env["CARGO_TARGET_DIR"]) / "thumbv7em-none-eabihf" / "release-mcu" / "vyr-size"
    plog = TMP / f"lvglmb-vyr-{name}.log"
    plog.unlink(missing_ok=True)
    args = [str(QEMU), "-machine", MACHINE, "-nographic",
            "-semihosting-config", "enable=on,target=native", "-icount", "shift=0,sleep=off",
            "-plugin", f"{INSN},match=bkpt,trace=on", "-d", "plugin", "-D", str(plog),
            "-kernel", str(elf)]
    g = subprocess.run(args, capture_output=True, text=True, cwd=REPO, timeout=DEADLINE)
    if g.returncode != 0:
        log(f"  vyr run {name} rc={g.returncode}")
        return None
    gout = g.stdout + g.stderr
    deltas = [int(d) for d in re.findall(r"Δ\+(\d+) since last match", plog.read_text())]
    # The probe prints header + 2 case-table lines (null, name), then per case
    # 2*REPS deltas: [render][gap]. Find the two render deltas by name order.
    ncases = len(re.findall(r"vyr-probe\] case i=", gout))
    reps = 1
    m = re.search(r"cases x (\d+) timed reps", gout)
    if m:
        reps = int(m.group(1))
    order = [mo.group(1) for mo in re.finditer(r"case i=\d+ name=(\S+)", gout)]
    if ncases != 2 or "null" not in order or name not in order:
        log(f"  vyr {name}: expected [null,{name}], got {order}")
        return None
    window = deltas[1 + ncases:]  # skip boot + the case-table bkpts
    renders = {order[ci]: min(window[ci * 2 * reps + 2 * r + 1] for r in range(reps))
               for ci in range(ncases)}
    return renders[name] - renders["null"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(REPO / "docs" / "perf" / "microbench.db"))
    ap.add_argument("--frames", type=int, default=40)
    ap.add_argument("--tier", default="exact")
    a = ap.parse_args()
    if not Path(a.db).exists():
        log(f"no DB at {a.db} — run ./dev.py microbench first")
        return 1

    log(f"per-primitive vyr({a.tier})-vs-LVGL, plugin-exact, above null "
        f"({a.frames} LVGL frames)")
    # LVGL null first — the floor AND the sanity anchor.
    lv_null = lvgl_frame(0, a.frames)
    if lv_null is None:
        log("LVGL null build/measure failed — cannot proceed")
        return 1
    log(f"LVGL null (background only): {lv_null:,} insns/frame")

    con = sqlite3.connect(a.db)
    run_id = con.execute("SELECT max(run_id) FROM run").fetchone()[0]
    written = 0
    for probe, (vyr_name, label) in MAP.items():
        lv = lvgl_frame(probe, a.frames)
        if lv is None:
            continue
        lv_above = lv - lv_null
        if not (0 < lv_above < ANCHOR_WHOLE_SCENE):
            log(f"  REJECT {label}: LVGL above-null {lv_above:,} is not in "
                f"(0, whole-scene anchor {ANCHOR_WHOLE_SCENE:,}) — not a trustworthy "
                f"per-primitive figure (error #5 guard). Not stored.")
            continue
        vy = vyr_above_null(vyr_name, a.tier)
        if vy is None or vy <= 0:
            log(f"  {label}: vyr above-null unavailable — skipping")
            continue
        ratio = vy / lv_above
        con.execute("UPDATE points SET lvgl_insns=?, lvgl_ratio=? "
                    "WHERE run_id=? AND tier=? AND name=?",
                    (lv_above, ratio, run_id, a.tier, vyr_name))
        con.commit()
        written += 1
        log(f"  {label:8s} ({vyr_name}): vyr {vy:,} / LVGL {lv_above:,} = "
            f"{ratio:.2f}x  {'vyr slower' if ratio > 1 else 'vyr faster'}")
    con.close()
    log(f"wrote lvgl_ratio for {written} primitives into run {run_id} ({a.tier} tier). "
        f"Indicative — content matching is imperfect (lvgl-gap.md).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

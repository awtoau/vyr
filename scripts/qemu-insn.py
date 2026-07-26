#!/usr/bin/env python3
"""qemu-insn.py — EXACT guest instruction counts for M4 firmware, via a
QEMU TCG plugin.

Why this exists
---------------
The M4 benchmark used to derive "insns/frame" from ARM semihosting SYS_CLOCK
under `-icount shift=0,sleep=off`, multiplying centiseconds by 1e7. That is
NOT an instruction count: on Fedora's stock qemu-system-arm the identical
unchanged workload read 39 cs idle and 58 cs with the host CPU loaded (a 49%
swing), and idle repeats drifted 39/39/40/39. SYS_CLOCK there tracks HOST WALL
TIME. Stock Fedora qemu is also built WITHOUT --enable-plugins
(`-plugin help` -> "plugin interface not enabled in this build"), so there was
no way to do better.

This script uses a locally built plugin-enabled QEMU (see
scripts/qemu-plugins-build.py) plus upstream's `libinsn.so`, which registers a
per-instruction execution callback. Its count is an exact architectural
instruction count and is bit-identical across runs and host load.

Windowing the timed loop
------------------------
`total insns` covers the whole run (boot, parse, first frame, hashing). The
per-frame figure needs just the timed loop. Both firmwares bracket that loop
with a semihosting clock read and execute NO semihosting inside it:

    t0 = clock();  for (N frames) { render(); }  t1 = clock();

ARM semihosting is `bkpt #0xab`, so libinsn's `match=bkpt,trace=on` prints the
instruction delta between consecutive semihosting traps. The single largest
delta IS the timed window, exactly — no host clock involved. (This needs a
QEMU built with capstone: qemu has no in-tree ARM disassembler, and without
disassembly `match=` silently matches nothing.)

Usage
-----
  python3 scripts/qemu-insn.py <elf> [--name NAME] [--repeat N] [--load N]

  --repeat N  run the same ELF N times and assert every count is identical
  --load N    spawn N CPU-burner processes for the duration (determinism proof
              under host load); they are killed when the runs finish

Output: tmp/qemu-insn-<name>.json, log tmp/qemu-insn.log
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import pathlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# VYR_PERF_TMP: a per-worker scratch namespace. The output name is derived from
# a caller-supplied tag that REPEATS across specimens (ph-exact-z-fold), so two
# parallel replay shards would otherwise write the same file.
TMP = pathlib.Path(os.environ["VYR_PERF_TMP"]) if os.environ.get("VYR_PERF_TMP") else REPO / "tmp"
LOG = TMP / "qemu-insn.log"

QEMU_BUILD = Path(os.environ.get("VYR_QEMU_BUILD", "/mnt/2tb/git_debris/qemu-plugins-build"))
QEMU = QEMU_BUILD / "qemu-system-arm"
# Upstream moved insn.c out of contrib/plugins into tests/tcg/plugins; accept
# either so this keeps working across QEMU versions.
PLUGIN_CANDIDATES = [
    QEMU_BUILD / "tests" / "tcg" / "plugins" / "libinsn.so",
    QEMU_BUILD / "contrib" / "plugins" / "libinsn.so",
]
QEMU_SRC = Path(os.environ.get("VYR_QEMU_SRC", "/mnt/2tb/git_mirror/qemu"))

MACHINE = "netduinoplus2"  # STM32F405: M4F + 192 KiB SRAM — same as dev.py qemu-m4
# Hard wall-clock guard for a wedged guest. The plugin's per-insn callback
# slows TCG roughly 5x, and a full Exact run is ~1.4e9 guest insns, so a
# healthy run is tens of seconds; 900 s means the guest is stuck, not slow.
DEADLINE_S = 900


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line, flush=True)
    with LOG.open("a") as fh:
        fh.write(line + "\n")


def qemu_identity() -> dict:
    ver = subprocess.run([str(QEMU), "--version"], capture_output=True, text=True,
                         check=False).stdout.strip().splitlines()
    commit = subprocess.run(["git", "-C", str(QEMU_SRC), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=False).stdout.strip()
    describe = subprocess.run(["git", "-C", str(QEMU_SRC), "log", "-1", "--format=%cI"],
                              capture_output=True, text=True, check=False).stdout.strip()
    return {
        "binary": str(QEMU),
        "version": ver[0] if ver else None,
        "source": str(QEMU_SRC),
        "source_commit": commit,
        "source_commit_date": describe,
        "built_by": "scripts/qemu-plugins-build.py (--enable-plugins --enable-capstone)",
    }


def plugin_path() -> Path:
    for p in PLUGIN_CANDIDATES:
        if p.exists():
            return p
    log("ERROR: no libinsn.so found in " + ", ".join(str(p) for p in PLUGIN_CANDIDATES))
    log("       run: python3 scripts/qemu-plugins-build.py")
    raise SystemExit(2)


def one_run(elf: Path, plugin: Path, tag: str) -> dict:
    """One emulated boot. Returns the exact counts parsed from the plugin log."""
    plog = TMP / f"qemu-insn-plugin-{tag}.log"
    if plog.exists():
        plog.unlink()
    args = [
        str(QEMU), "-machine", MACHINE, "-nographic",
        "-semihosting-config", "enable=on,target=native",
        "-icount", "shift=0,sleep=off",
        "-plugin", f"{plugin},match=bkpt,trace=on",
        "-d", "plugin", "-D", str(plog),
        "-kernel", str(elf),
    ]
    t0 = time.monotonic()
    try:
        guest = subprocess.run(args, capture_output=True, text=True, cwd=REPO,
                               check=False, timeout=DEADLINE_S)
    except subprocess.TimeoutExpired:
        log(f"ERROR: qemu hit the {DEADLINE_S}s guard on {elf} — guest wedged, killed")
        raise SystemExit(1)
    wall = time.monotonic() - t0
    gout = guest.stdout + guest.stderr  # semihosting console lands on stderr
    if guest.returncode != 0:
        log(f"ERROR: guest rc={guest.returncode}\n{gout}")
        raise SystemExit(1)

    text = plog.read_text()
    m = re.search(r"^total insns: (\d+)$", text, re.M)
    if not m:
        log(f"ERROR: plugin emitted no 'total insns' line (see {plog})")
        raise SystemExit(1)
    total = int(m.group(1))

    # The timed window: the largest gap between two consecutive semihosting
    # traps. Everything else is bracketed by console writes, so this is the
    # render loop and nothing else.
    hits = re.findall(
        r"^0x([0-9a-f]+), '([^']*)', \d+ hits , cpu \d+, \d+ match hits,"
        r" Δ\+(\d+) since last match", text, re.M)
    if not hits:
        log(f"ERROR: libinsn matched no bkpt — is this qemu built with capstone? "
            f"(no ARM disassembly => match= never fires). See {plog}")
        raise SystemExit(1)
    # #44 made this TWO windows, not one — but only for firmware BUILT since
    # #44. Every historical ref replayed by perf-harness.py has a single timed
    # pass, and assuming two would either pick an unrelated small delta as
    # "render_only" or trip the shape guard below on every old commit. So the
    # pass count is DETECTED from the guest's own output, not assumed.
    #
    # A TWO-pass build (the #44 era) reports BOTH a render-only pass and a
    # with-fold pass, and it says so with a `total=`/`total_cs=` token. The #45
    # perf build renders render-only in ONE pass and marks it `fold=absent-by-
    # build`: it still prints `render_only=`, but there is no `total=`, so keying
    # on `render_only=` alone would wrongly expect two windows and trip the shape
    # guard. Key on the `total` token, which is present ONLY when a second
    # (with-fold) pass was actually run. No token => one pass => the largest
    # delta is the whole timed window.
    ranked = sorted(hits, key=lambda h: int(h[2]), reverse=True)
    # The #44 two-pass marker is a space-prefixed ` total=` (vyr) or `total_cs=`
    # (LVGL). Match those exactly — a bare `total=` also occurs in LVGL's
    # `lv_mem_total=` line, which has nothing to do with the timed passes and
    # would falsely trip the two-window path on a #45 single-pass perf build.
    two_pass = (" total=" in gout) or ("total_cs=" in gout)

    if two_pass and len(ranked) > 1:
        # The with-fold pass is necessarily the larger of the two.
        total_addr, _d0, total_w = ranked[0]
        total_w = int(total_w)
        window_addr, _d1, window = ranked[1]
        window = int(window)
        fold_w = max(0, total_w - window)

        # Guard the assumption rather than trusting it: the two passes render
        # the same frames, so they must be within a small factor of each other,
        # and both must tower over the third-largest delta. If the shape is not
        # that, the window identification is wrong and so is everything below.
        third = int(ranked[2][2]) if len(ranked) > 2 else 0
        if window and (total_w > 3 * window or third > window // 4):
            log(f"ERROR: timed-window identification failed — deltas "
                f"{total_w:,} / {window:,} / {third:,}. Expected two comparable "
                f"timed passes far above the rest (see {plog})")
            raise SystemExit(1)
    else:
        # Single timed pass: the window is the largest delta, and whether it
        # contains the fold is a property of the ref, not something this script
        # can separate. Report it as the window and leave the split unknown —
        # perf-harness.py prices the fold for these refs by rebuilding with it
        # neutered, and records `fold_provenance` accordingly.
        window_addr, _disas, window = ranked[0]
        window = int(window)
        total_w = window
        fold_w = None

    # Cross-check against what the firmware itself reported.
    frames = None
    for pat in (r"timed: (\d+) warmed frames", r"timed_frames=\D*(\d+)"):
        fm = re.search(pat, gout)
        if fm:
            frames = int(fm.group(1))
            break
    cs = None
    for pat in (r"warmed frames in (\d+) cs", r"timed_cs=\D*(\d+)"):
        cm = re.search(pat, gout)
        if cm:
            cs = int(cm.group(1))
            break

    return {
        "total_insns": total,
        "timed_window_insns": window,
        # #44: all three, always. render_only is the headline series.
        "timed_passes": 2 if fold_w is not None else 1,
        "render_only_insns": window if fold_w is not None else None,
        "with_fold_insns": total_w if fold_w is not None else None,
        "fold_insns": fold_w,
        "render_only_insns_per_frame": (
            window // frames if (fold_w is not None and frames) else None),
        "with_fold_insns_per_frame": (
            total_w // frames if (fold_w is not None and frames) else None),
        "fold_insns_per_frame": fold_w // frames if (fold_w is not None and frames) else None,
        "timed_window_end_bkpt": "0x" + window_addr,
        "timed_frames": frames,
        "insns_per_frame": window // frames if frames else None,
        "sys_clock_cs": cs,
        "sys_clock_derived_insns_per_frame": (cs * 10_000_000 // frames)
                                             if (cs and frames) else None,
        "wall_s": round(wall, 2),
        "guest_console": gout.strip().splitlines(),
    }


class HostLoad:
    """N CPU burners, for proving the count does not move under host load.

    No shell sleep/timeout anywhere: the burners are Python child processes
    spinning on an unbounded loop, terminated explicitly when the block exits.
    """

    def __init__(self, n: int):
        self.n = n
        self.procs: list[subprocess.Popen] = []

    def __enter__(self):
        for _ in range(self.n):
            self.procs.append(subprocess.Popen(
                [sys.executable, "-c", "x=0\nwhile True: x+=1"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if self.n:
            log(f"host load: {self.n} CPU burner processes running")
        return self

    def __exit__(self, *exc):
        for p in self.procs:
            p.kill()
        for p in self.procs:
            p.wait()
        if self.n:
            log(f"host load: {self.n} burners stopped")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("elf", type=Path)
    ap.add_argument("--name", default=None, help="output slug (default: ELF stem)")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--load", type=int, default=0,
                    help="spawn N CPU burners for the duration (determinism proof)")
    a = ap.parse_args()

    TMP.mkdir(exist_ok=True)
    elf = a.elf.resolve()
    if not elf.exists():
        log(f"ERROR: no such ELF {elf}")
        return 1
    name = a.name or elf.stem
    plugin = plugin_path()

    log("=" * 68)
    log(f"qemu-insn: {name} — {elf}")
    ident = qemu_identity()
    log(f"  qemu    {ident['version']} @ {ident['source_commit'][:12]}")
    log(f"  plugin  {plugin}")
    sha = hashlib.sha256(elf.read_bytes()).hexdigest()
    log(f"  elf     sha256 {sha}")

    runs = []
    with HostLoad(a.load):
        for i in range(a.repeat):
            r = one_run(elf, plugin, f"{name}-{i}")
            r["host_load_procs"] = a.load
            runs.append(r)
            log(f"  run {i + 1}/{a.repeat} load={a.load}: total={r['total_insns']:,} "
                f"window={r['timed_window_insns']:,} frames={r['timed_frames']} "
                f"per_frame={r['insns_per_frame']:,} "
                f"(SYS_CLOCK said {r['sys_clock_cs']} cs) wall {r['wall_s']}s")

    totals = {r["total_insns"] for r in runs}
    windows = {r["timed_window_insns"] for r in runs}
    # The MEASUREMENT is the timed window. It must be bit-identical, always.
    deterministic = len(windows) == 1
    # `total insns` is a weaker check: the firmware PRINTS the wall-clock
    # SYS_CLOCK reading, so a 2-digit vs 3-digit centisecond value costs a
    # different number of formatting instructions. That handful of insns is
    # the only host-load-dependent execution in the whole guest, and it lives
    # entirely outside the timed window.
    log(f"  determinism: timed window distinct={len(windows)}"
        f" -> {'EXACT/REPRODUCIBLE' if deterministic else 'VARIES — SETUP IS WRONG'}")
    if len(totals) != 1:
        log(f"  note: total insns varies {sorted(totals)} — SYS_CLOCK digit count "
            f"printed by the guest (cs={sorted(r['sys_clock_cs'] for r in runs)}); "
            f"outside the timed window, does not affect insns/frame")

    out = {
        "name": name,
        "elf": str(elf),
        "elf_sha256": sha,
        "machine": MACHINE,
        "icount": "shift=0,sleep=off",
        "qemu": ident,
        "plugin": str(plugin),
        "plugin_args": "match=bkpt,trace=on",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repeat": a.repeat,
        "host_load_procs": a.load,
        "deterministic": deterministic,
        "total_insns_stable": len(totals) == 1,
        "total_insns_note": (
            "total covers the whole boot; it shifts by a few insns when the "
            "guest formats a longer SYS_CLOCK decimal (wall-clock derived). "
            "The timed window is unaffected."
        ),
        "total_insns": runs[0]["total_insns"] if deterministic else None,
        "timed_window_insns": runs[0]["timed_window_insns"] if deterministic else None,
        "timed_frames": runs[0]["timed_frames"],
        "insns_per_frame": runs[0]["insns_per_frame"] if deterministic else None,
        # #44: all three promoted, always. `insns_per_frame` above is an alias
        # for render_only so existing consumers keep working and keep meaning
        # the same thing they did once the fold left the window.
        "timed_passes": runs[0]["timed_passes"],
        "render_only_insns_per_frame": (
            runs[0]["render_only_insns_per_frame"] if deterministic else None),
        "with_fold_insns_per_frame": (
            runs[0]["with_fold_insns_per_frame"] if deterministic else None),
        "fold_insns_per_frame": (
            runs[0]["fold_insns_per_frame"] if deterministic else None),
        "runs": runs,
    }
    dest = TMP / f"qemu-insn-{name}.json"
    dest.write_text(json.dumps(out, indent=2) + "\n")
    log(f"  wrote {dest}")
    return 0 if deterministic else 1


if __name__ == "__main__":
    raise SystemExit(main())

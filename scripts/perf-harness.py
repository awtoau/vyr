#!/usr/bin/env python3
"""perf-harness.py — THE vyr performance instrument.

One script, one instrument, one record. Point it at a git ref and it fills as
much of the measurement MATRIX as the machine and that ref allow, and says in
writing which cells it could not fill and why.

Why one script
--------------
Four measurement errors (docs/measurements/perf-history.md) all had the same
root cause: the instrument kept changing under the numbers. The counter-measure
is that the HARNESS is constant and only the RENDERER varies — old commits are
measured with TODAY's tools, never with their own. `scripts/perf-replay.py`
walks history calling into this file; nothing else may produce an M4
instruction figure.

The matrix
----------
Axes:

  platform   host (x86-64) · qemu-m4 (thumbv7em-none-eabihf, plugin QEMU)
             board (STM32F429I-DISC1, DWT_CYCCNT) · arm32
             (armv7-unknown-linux-musleabihf under qemu-arm-static)
  tier       exact · fast · draft            (whichever the ref actually has)
  opt-level  z · s · 2 · 3                   (the release-mcu profile knob, #33)

`word_bits` and `float` are NOT free axes — they are properties of the
platform (you cannot ask for f64-in-software on x86-64), so every cell carries
them as recorded attributes. That is the point of the matrix: x86-64 computes
sin/cos in hardware and the M4F does not, which is exactly why the soft-f64
trig bill (#32, ~11.4 M insns/frame) was invisible in every host measurement
for months. A cell where the M4 column diverges from the host column is a
platform pathology BY CONSTRUCTION. Corollary worth stating: there is no point
optimising transcendentals for x86 — optimisation targets are per-platform.

Metrics per cell (null + a written reason when the platform cannot produce it):

  insns_per_frame_total        plugin QEMU + libinsn        qemu-m4 only
  harness_fold_insns           differential build           qemu-m4 only
  insns_per_frame_render_only  total - fold                 qemu-m4 only
  cycles_per_frame             DWT_CYCCNT                   board only
  ns_per_frame / ns_per_px     vyr-bench scene/panel_<tier> host only
  heap_peak_b                  counting allocator           every platform
  stack_high_water_b           vyr-size stack probe         NOT IMPLEMENTED YET
  flash_text_data_b            arm-none-eabi-size           MCU targets
  frame_hash                   the workload itself          every platform

**render_only is the headline, and HOW it was obtained is part of it.** Both M4
vehicles used to FNV-fold their own output inside the timed window (error 4:
54.7 % of LVGL's frame, 36.2 % of Draft's). Every cell therefore records
`fold_provenance`:

  absent-from-window-by-build  the timed pass contains no fold (#44) — total IS
                               render-only, by construction, nothing subtracted
  measured-differential        the SAME firmware rebuilt with the fold folding
                               an empty slice and the two counts differenced,
                               IN THIS CELL (same commit, tier and opt-level)
  derived-by-subtraction       a fold figure carried in from another cell —
                               never produced here, because the fold's own cost
                               moves with tier, opt-level and compiler, and a
                               stale one has already inverted a published result

`build_type` records perf vs verifying, since after #44 they are different
binaries. And a build whose frame hash does not match the host leg's has its
timing **suppressed rather than annotated**: measuring a firmware that renders
the wrong pixels is worse than reporting nothing.

Determinism is checked, not assumed: every instruction measurement is repeated
and a disagreement is recorded as a FAILED cell, never averaged.

Usage
-----
  python3 scripts/perf-harness.py --ref HEAD
  python3 scripts/perf-harness.py --ref v0.1.0 --all-platforms
  python3 scripts/perf-harness.py --ref HEAD --platforms host,qemu-m4 \\
                                  --opt-levels z,s,2,3        # the #33 sweep
  python3 scripts/perf-harness.py --in-place                  # this worktree

Output: tmp/perf-harness-<ref>.json      Log: tmp/perf-harness.log
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import importlib.util
import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HARNESS_VERSION = "perf-harness/1.0"
# Snapshotted at IMPORT, not at record time: every row of one replay must carry
# the sha of the code that actually ran, even if the file is edited underneath a
# long walk. (Reading it per row silently produced two different shas for one
# run — a small thing, but provenance that drifts is not provenance.)
HARNESS_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

REPO = Path(__file__).resolve().parent.parent  # the INSTRUMENT's checkout
TMP = REPO / "tmp"
LOG = TMP / "perf-harness.log"

# Detached specimen worktree + a shared cargo target dir. dev.py resolves
# binaries as REPO/target/... and ignores CARGO_TARGET_DIR, so the shared dir
# is wired in as a SYMLINK at <specimen>/target — the previous sweep learned
# this the hard way.
SPEC_WORKTREE = Path(os.environ.get("VYR_SPEC_WORKTREE", "/mnt/2tb/git_debris/vyr-perf-specimen"))
SHARED_TARGET = Path(os.environ.get("VYR_SPEC_TARGET", "/mnt/2tb/git_debris/vyr-perf-specimen-target"))

MCU_TARGET = "thumbv7em-none-eabihf"
ARM32_TARGET = "armv7-unknown-linux-musleabihf"
QEMU_ARM_STATIC = "/usr/bin/qemu-arm-static"
BOARD_PROBE = "0483:3752:0671FF484971754867174427"  # the DISC1; 0483:374e is NOT ours
SCENE_PX = 480 * 270

TIER_FEATURE = {"exact": "run-qemu", "fast": "run-qemu,fast", "draft": "run-qemu,draft"}
TIER_GATE = {"exact": None, "fast": "fast", "draft": "draft"}  # the cargo feature each tier needs
FOLD_NEEDLE = "fnv1a_fold(hash, core::hint::black_box(&band_buf[..len]))"
FOLD_PATCH = "fnv1a_fold(hash, core::hint::black_box(&band_buf[..0]))"

METRIC_KEYS = [
    "insns_per_frame_total",
    "harness_fold_insns",
    "insns_per_frame_render_only",
    "cycles_per_frame",
    "ns_per_frame",
    "ns_per_px",
    "heap_peak_b",
    "stack_high_water_b",
    "flash_text_data_b",
    "frame_hash",
]

# Platform attributes. These are RECORDED, not selectable — the whole value of
# the matrix is that they differ per platform.
PLATFORMS = {
    "host": {
        "isa": "x86-64",
        "word_bits": 64,
        "float": "hardware f32 AND f64 (SSE2); hardware transcendentals via libm's x86 paths",
        "target": None,
        "profile": "release",
    },
    "qemu-m4": {
        "isa": "ARMv7E-M (Cortex-M4F, STM32F405 via qemu netduinoplus2)",
        "word_bits": 32,
        "float": "hardware f32 only (VFPv4-SP); every f64 op is SOFTWARE (compiler-rt)",
        "target": MCU_TARGET,
        "profile": "release-mcu",
    },
    "board": {
        "isa": "ARMv7E-M (STM32F429ZI, real silicon @180 MHz)",
        "word_bits": 32,
        "float": "hardware f32 only (VFPv4-SP); every f64 op is SOFTWARE (compiler-rt)",
        "target": MCU_TARGET,
        "profile": "release-mcu",
    },
    "arm32": {
        "isa": "ARMv7-A (musl, under qemu-arm-static user mode)",
        "word_bits": 32,
        "float": "hardware f32 and f64 (VFPv3-D16)",
        "target": ARM32_TARGET,
        "profile": "release",
    },
}

_lines: list[str] = []


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line, flush=True)
    _lines.append(line)
    TMP.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(_lines) + "\n")


def sh(args: list[str], cwd: Path, env_extra: dict | None = None, deadline: int = 3600):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env,
                          check=False, timeout=deadline)


def git(*args: str, cwd: Path = REPO) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return r.stdout.strip()


# --- the instruction counter, imported so it is single-sourced ---------------


def _qemu_insn_module():
    spec = importlib.util.spec_from_file_location("qemu_insn", REPO / "scripts" / "qemu-insn.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


QI = None  # lazily loaded


# --- specimen ---------------------------------------------------------------


def prepare_specimen(ref: str, in_place: bool) -> tuple[Path, dict]:
    """Check `ref` out somewhere safe and return (dir, identity)."""
    if in_place:
        spec = REPO
    else:
        spec = SPEC_WORKTREE
        if not (spec / ".git").exists():
            SPEC_WORKTREE.parent.mkdir(parents=True, exist_ok=True)
            log(f"creating specimen worktree {spec}")
            r = sh(["git", "worktree", "add", "--detach", str(spec), "HEAD"], cwd=REPO)
            if r.returncode != 0:
                raise SystemExit(f"worktree add failed: {r.stderr}")
        sha = git("rev-parse", ref)
        r = sh(["git", "checkout", "--detach", "-f", sha], cwd=spec)
        if r.returncode != 0:
            raise SystemExit(f"checkout {ref} failed: {r.stderr}")
        sh(["git", "clean", "-qfdx", "--exclude=target"], cwd=spec)
        # Re-assert the shared-target symlink: `checkout -f` / `clean` can
        # replace the link with a real directory.
        SHARED_TARGET.mkdir(parents=True, exist_ok=True)
        tgt = spec / "target"
        if not tgt.is_symlink():
            if tgt.exists():
                shutil.rmtree(tgt, ignore_errors=True)
            tgt.symlink_to(SHARED_TARGET)

    ident = {
        "ref": ref,
        "dir": str(spec),
        "commit": git("rev-parse", "--short", "HEAD", cwd=spec),
        "commit_full": git("rev-parse", "HEAD", cwd=spec),
        "date": git("log", "-1", "--format=%aI", cwd=spec),
        "subject": git("log", "-1", "--format=%s", cwd=spec),
        "dirty": bool(git("status", "--porcelain", cwd=spec)),
    }
    return spec, ident


BUILD_INPUTS = ("vyr-core", "vyr-size", "vyr-bench", "Cargo.toml", "Cargo.lock",
                "rust-toolchain.toml")


def build_key_rev(rev: str = "HEAD", cwd: Path = REPO) -> str:
    """A hash of the git trees of everything that can change the measured
    binaries. Two commits with the same key produce byte-identical ELFs, so
    re-measuring the second is provably redundant rather than merely likely to
    agree — which is what makes a 46-commit replay affordable."""
    h = hashlib.sha256()
    for p in BUILD_INPUTS:
        t = git("rev-parse", f"{rev}:{p}", cwd=cwd) or "ABSENT"
        h.update(f"{p}={t}\n".encode())
    return h.hexdigest()[:16]


def build_key(spec: Path) -> str:
    return build_key_rev("HEAD", spec)


def capabilities(spec: Path) -> dict:
    """What this REF can do — detected, never assumed."""
    cap: dict = {"tiers": {}, "notes": []}
    ct = spec / "vyr-size" / "Cargo.toml"
    wl = spec / "vyr-size" / "src" / "workload.rs"
    root = spec / "Cargo.toml"
    feats: set[str] = set()
    if ct.exists():
        block = ct.read_text().split("[features]", 1)
        if len(block) > 1:
            body = block[1].split("\n[", 1)[0]
            feats = set(re.findall(r"^([A-Za-z][\w-]*)\s*=\s*\[", body, re.M))
    cap["vyr_size_features"] = sorted(feats)
    cap["has_m4_vehicle"] = wl.exists() and "run-qemu" in feats
    cap["has_board_feature"] = "board" in feats
    cap["has_release_mcu_profile"] = root.exists() and "[profile.release-mcu]" in root.read_text()
    # #44: the fold moved OUT of the timed window (an untimed verification pass
    # renders + folds and must reproduce the reference hash before the timed
    # pass runs, which renders only). From that commit on there is nothing to
    # subtract — total IS render-only — and the harness must say so rather than
    # quietly reporting a missing fold. Marker: the verification pass's binding.
    wl_text = wl.read_text() if wl.exists() else ""
    cap["fold_patchable"] = FOLD_NEEDLE in wl_text
    cap["fold_in_timed_window"] = (
        False if "h_verify" in wl_text else (True if FOLD_NEEDLE in wl_text else None)
    )
    cap["stack_probe"] = "stack-probe" in feats  # #33; absent before b93eec1
    if wl.exists():
        m = re.search(r"const TIMED_FRAMES: u32 = (\d+)", wl.read_text())
        cap["timed_frames_declared"] = int(m.group(1)) if m else None
    for tier, gate in TIER_GATE.items():
        cap["tiers"][tier] = (gate is None or gate in feats) and cap["has_m4_vehicle"]
    return cap


def probe_platforms(spec: Path, cap: dict, wanted: list[str]) -> tuple[dict, dict]:
    """(available, unavailable{name: reason}) — a sparse matrix must explain
    itself, so every refusal carries a written reason."""
    have, why = {}, {}
    installed = sh(["rustup", "target", "list", "--installed"], cwd=REPO).stdout.split()

    for p in wanted:
        if p == "host":
            if cap["has_m4_vehicle"]:
                have[p] = "native x86-64 run of the same run-qemu workload"
            else:
                why[p] = "this ref has no vyr-size run-qemu workload"
        elif p == "qemu-m4":
            global QI
            QI = QI or _qemu_insn_module()
            if not cap["has_m4_vehicle"]:
                why[p] = "this ref has no vyr-size run-qemu workload (M4 vehicle absent)"
            elif not cap["has_release_mcu_profile"]:
                why[p] = "this ref has no [profile.release-mcu]"
            elif MCU_TARGET not in installed:
                why[p] = f"rust target {MCU_TARGET} not installed"
            elif not QI.QEMU.exists():
                why[p] = f"plugin QEMU absent at {QI.QEMU} (run scripts/qemu-plugins-build.py)"
            elif not any(c.exists() for c in QI.PLUGIN_CANDIDATES):
                why[p] = "libinsn.so absent (QEMU built without --enable-plugins?)"
            else:
                have[p] = f"{QI.QEMU} + libinsn"
        elif p == "arm32":
            if not cap["has_m4_vehicle"]:
                why[p] = "this ref has no vyr-size run-qemu workload"
            elif ARM32_TARGET not in installed:
                why[p] = f"rust target {ARM32_TARGET} not installed"
            elif not Path(QEMU_ARM_STATIC).exists():
                why[p] = f"{QEMU_ARM_STATIC} absent"
            else:
                have[p] = f"{ARM32_TARGET} under {QEMU_ARM_STATIC}"
        elif p == "board":
            if not cap["has_board_feature"]:
                why[p] = "this ref has no vyr-size `board` feature"
            elif not (spec / "scripts" / "board-run.py").exists() and \
                    not (REPO / "scripts" / "board-run.py").exists():
                why[p] = "scripts/board-run.py absent"
            elif BOARD_PROBE not in _usb_serials():
                why[p] = f"ST-LINK {BOARD_PROBE} not attached"
            else:
                have[p] = f"STM32F429I-DISC1 via ST-LINK {BOARD_PROBE}"
        else:
            why[p] = "unknown platform"
    return have, why


def _usb_serials() -> set[str]:
    out = set()
    for ser in glob.glob("/sys/bus/usb/devices/*/serial"):
        d = os.path.dirname(ser)
        try:
            vid = open(d + "/idVendor").read().strip()
            pid = open(d + "/idProduct").read().strip()
            out.add(f"{vid}:{pid}:{open(ser).read().strip()}")
        except OSError:
            continue
    return out


# --- cells ------------------------------------------------------------------


def blank_cell(plat: str, tier: str, opt: str, firmware: str = "vyr") -> dict:
    a = PLATFORMS[plat]
    return {
        "platform": plat,
        "firmware": firmware,
        "tier": tier,
        "opt_level": opt,
        "target": a["target"],
        "profile": a["profile"],
        "isa": a["isa"],
        "word_bits": a["word_bits"],
        "float": a["float"],
        "status": "skipped",
        "reason": None,
        "metrics": {k: None for k in METRIC_KEYS},
        "metric_notes": {},
        "provenance": {},
    }


def _console_facts(console: list[str]) -> dict:
    text = "\n".join(console)
    out = {}
    m = re.search(r"frame fnv1a=(0x[0-9a-f]{16})", text)
    if m:
        out["frame_hash"] = m.group(1)
    m = re.search(r"heap peak=(\d+) B", text)
    if m:
        out["heap_peak_b"] = int(m.group(1))
    m = re.search(r"F16 fast-path: \d+ / \d+ delivered px \(([\d.]+)%\)", text)
    if m:
        out["fastpath_coverage_pct"] = float(m.group(1))
    return out


def cargo_build(spec: Path, args: list[str]) -> subprocess.CompletedProcess:
    # CARGO_INCREMENTAL=0: incremental codegen-unit boundaries move the
    # instruction count by tens of percent between otherwise identical builds.
    return sh(["cargo", "build", *args], cwd=spec, env_extra={"CARGO_INCREMENTAL": "0"})


def mcu_build_args(tier: str, opt: str) -> list[str]:
    args = ["--profile", "release-mcu", "-p", "vyr-size", "--target", MCU_TARGET,
            "--no-default-features", "--features", TIER_FEATURE[tier]]
    if opt != "z":
        # opt-level is a string for "s"/"z", a bare integer for 1/2/3.
        v = f'"{opt}"' if opt in ("s", "z") else opt
        args += ["--config", f"profile.release-mcu.opt-level={v}"]
    return args


def flash_size(elf: Path) -> tuple[int | None, dict]:
    exe = shutil.which("arm-none-eabi-size") or \
        "/opt/arm-gnu-toolchain/arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi/bin/arm-none-eabi-size"
    if not Path(exe).exists():
        return None, {"flash_text_data_b": "arm-none-eabi-size not on PATH"}
    r = sh([exe, "-B", str(elf)], cwd=REPO)
    m = re.search(r"^\s*(\d+)\s+(\d+)\s+(\d+)", r.stdout, re.M)
    if not m:
        return None, {"flash_text_data_b": "arm-none-eabi-size produced no parseable line"}
    return int(m.group(1)) + int(m.group(2)), {}


def count_insns(elf: Path, tag: str, repeat: int, cache: dict | None) -> dict:
    """Exact architectural instruction count for one ELF. Repeated; a
    disagreement is a FAILURE, never an average."""
    global QI
    QI = QI or _qemu_insn_module()
    sha = hashlib.sha256(elf.read_bytes()).hexdigest()
    if cache is not None and sha in cache:
        c = dict(cache[sha])
        c["cache_hit"] = True
        return c
    plugin = QI.plugin_path()
    runs = []
    for i in range(repeat):
        try:
            runs.append(QI.one_run(elf, plugin, f"{tag}-{i}"))
        except SystemExit as e:
            # A guest that exits non-zero has FAILED ITS OWN CHECKS — since #44
            # that includes the untimed verification pass refusing to hand over
            # a timed window when the frame hash does not reproduce. Report
            # nothing rather than a number from a firmware that renders wrong.
            return {"elf_sha256": sha, "deterministic": False, "repeat": repeat,
                    "insns_per_frame": None, "timed_frames": None,
                    "guest_failed": True, "console": [],
                    "error": f"guest run failed (SystemExit {e.code}) — see tmp/qemu-insn.log",
                    "cache_hit": False}
    windows = {r["timed_window_insns"] for r in runs}
    frames = runs[0]["timed_frames"]
    ok = len(windows) == 1 and frames
    out = {
        "elf_sha256": sha,
        "deterministic": len(windows) == 1,
        "repeat": repeat,
        "timed_window_insns": runs[0]["timed_window_insns"] if ok else None,
        "timed_frames": frames,
        "insns_per_frame": (runs[0]["timed_window_insns"] // frames) if ok else None,
        # #44: firmware built since the two-pass change MEASURES its own split
        # (render-only pass + with-fold pass in the same binary). When these are
        # present there is nothing to subtract and nothing to rebuild.
        "timed_passes": runs[0].get("timed_passes", 1),
        "render_only_per_frame": runs[0].get("render_only_insns_per_frame") if ok else None,
        "with_fold_per_frame": runs[0].get("with_fold_insns_per_frame") if ok else None,
        "fold_per_frame": runs[0].get("fold_insns_per_frame") if ok else None,
        "window_remainder": (runs[0]["timed_window_insns"] % frames) if ok else None,
        "windows_seen": sorted(windows),
        "console": runs[0]["guest_console"],
        "wall_s": [r["wall_s"] for r in runs],
        "cache_hit": False,
    }
    if cache is not None and ok:
        cache[sha] = {k: v for k, v in out.items() if k != "cache_hit"}
    return out


def boot_plain(elf: Path) -> str:
    """Boot the guest with no plugin — for facts the console prints (stack
    watermark) where an instruction count is not wanted."""
    global QI
    QI = QI or _qemu_insn_module()
    r = sh([str(QI.QEMU), "-machine", QI.MACHINE, "-nographic",
            "-semihosting-config", "enable=on,target=native",
            "-icount", "shift=0,sleep=off", "-kernel", str(elf)],
           cwd=REPO, deadline=900)
    return r.stdout + r.stderr


def measure_stack(spec: Path, tier: str, opt: str, cap: dict) -> tuple[int | None, str | None]:
    """Stack high-water via the `stack-probe` feature (#33). Deliberately a
    SEPARATE build: the probe paints CCM at boot and reads it back, so its ELF
    is not the one the instruction count was taken on and must never be."""
    if not cap["stack_probe"]:
        return None, "vyr-size has no `stack-probe` feature at this ref (added in #33)"
    args = mcu_build_args(tier, opt)
    args[args.index("--features") + 1] += ",stack-probe"
    r = cargo_build(spec, args)
    if r.returncode != 0:
        return None, "stack-probe build failed"
    elf = spec / "target" / MCU_TARGET / "release-mcu" / "vyr-size"
    m = re.search(r"stack watermark=(\d+) B", boot_plain(elf))
    return (int(m.group(1)), None) if m else (None, "guest printed no stack watermark")


def measure_qemu_m4(spec: Path, tier: str, opt: str, cap: dict, repeat: int,
                    do_fold: bool, cache: dict | None, do_stack: bool = False,
                    reference_hash: str | None = None) -> dict:
    cell = blank_cell("qemu-m4", tier, opt)
    cell["fold_provenance"] = "not-measured"
    cell["build_type"] = None
    cell["metric_notes"] = {
        "cycles_per_frame": "DWT_CYCCNT is real silicon only",
        "ns_per_frame": "emulated wall time is host TCG time, never recorded as a target number",
        "ns_per_px": "emulated wall time is host TCG time, never recorded as a target number",
    }
    if not cap["tiers"].get(tier):
        cell["reason"] = f"tier `{tier}` does not exist at this ref (no cargo feature)"
        return cell

    wl = spec / "vyr-size" / "src" / "workload.rs"
    original = wl.read_text()
    try:
        # --- 1. the real firmware: total, incl. the benchmark's own fold ---
        r = cargo_build(spec, mcu_build_args(tier, opt))
        if r.returncode != 0:
            cell["status"] = "failed"
            cell["reason"] = "build failed: " + (r.stderr[-600:] or r.stdout[-600:])
            return cell
        elf = spec / "target" / MCU_TARGET / "release-mcu" / "vyr-size"
        total = count_insns(elf, f"ph-{tier}-{opt}-fold", repeat, cache)
        if total.get("guest_failed"):
            cell["status"] = "failed"
            cell["reason"] = total["error"]
            return cell
        if not total["deterministic"]:
            cell["status"] = "failed"
            cell["reason"] = f"non-deterministic instruction window {total['windows_seen']} — " \
                             "recorded as FAILED rather than averaged"
            cell["provenance"]["total"] = total
            return cell
        facts = _console_facts(total["console"])
        if reference_hash and facts.get("frame_hash") and \
                facts["frame_hash"] != reference_hash:
            cell["status"] = "failed"
            cell["reason"] = (
                f"frame hash {facts['frame_hash']} != the host leg's {reference_hash} at "
                "this tier — the build renders the wrong pixels, so its timing is "
                "suppressed rather than annotated"
            )
            return cell
        cell["metrics"]["insns_per_frame_total"] = total["insns_per_frame"]
        cell["metrics"]["frame_hash"] = facts.get("frame_hash")
        cell["metrics"]["heap_peak_b"] = facts.get("heap_peak_b")
        fkib, notes = flash_size(elf)
        cell["metrics"]["flash_text_data_b"] = fkib
        cell["metric_notes"].update(notes)
        cell["provenance"]["fastpath_coverage_pct"] = facts.get("fastpath_coverage_pct")
        cell["provenance"]["total"] = {k: v for k, v in total.items() if k != "console"}
        cell["provenance"]["guest_console"] = total["console"]

        # --- 2. the same firmware with the fold folding NOTHING ---
        if total.get("timed_passes") == 2:
            # #44 two-pass firmware: render-only, with-fold and the difference
            # all come from ONE binary in ONE session. `insns_per_frame_total`
            # is the WITH-fold pass, so the `total` column keeps meaning what it
            # meant before the fold left the window and old rows stay comparable.
            cell["metrics"]["insns_per_frame_total"] = total["with_fold_per_frame"]
            cell["metrics"]["insns_per_frame_render_only"] = total["render_only_per_frame"]
            cell["metrics"]["harness_fold_insns"] = total["fold_per_frame"]
            cell["fold_provenance"] = "measured-in-cell (#44 two-pass)"
            cell["build_type"] = ("perf (timed pass renders only; a second timed pass "
                                  "prices the fold; an untimed verification pass must "
                                  "reproduce the hash before either)")
            cell["provenance"]["fold_share_of_total"] = (
                round(total["fold_per_frame"] / total["with_fold_per_frame"], 4)
                if total["with_fold_per_frame"] else None)
        elif cap.get("fold_in_timed_window") is False:
            # #44: there is nothing to subtract — the timed pass renders only,
            # and an untimed verification pass has already proven the hash.
            cell["metrics"]["insns_per_frame_render_only"] = total["insns_per_frame"]
            cell["metrics"]["harness_fold_insns"] = 0
            cell["fold_provenance"] = "absent-from-window-by-build (#44)"
            cell["build_type"] = "perf (timed pass renders only; fold in an untimed " \
                                 "verification pass that must reproduce the hash first)"
            cell["metric_notes"]["harness_fold_insns"] = (
                "zero BY CONSTRUCTION, not by subtraction: at this ref the fold is "
                "outside the timed window (#44), so total == render-only"
            )
        elif not do_fold:
            cell["metric_notes"]["harness_fold_insns"] = "--no-fold: fold not priced this run"
            cell["metric_notes"]["insns_per_frame_render_only"] = \
                "--no-fold: cannot separate render from the benchmark's own hash"
        elif not cap["fold_patchable"]:
            cell["metric_notes"]["harness_fold_insns"] = \
                "the fold call is not present verbatim in workload.rs at this ref"
            cell["metric_notes"]["insns_per_frame_render_only"] = \
                "fold not priceable at this ref, so render-only cannot be separated"
        else:
            wl.write_text(original.replace(FOLD_NEEDLE, FOLD_PATCH))
            r = cargo_build(spec, mcu_build_args(tier, opt))
            if r.returncode != 0:
                cell["metric_notes"]["harness_fold_insns"] = "fold-neutered build failed"
            else:
                nofold = count_insns(elf, f"ph-{tier}-{opt}-nofold", repeat, cache)
                if not nofold["deterministic"]:
                    cell["metric_notes"]["harness_fold_insns"] = \
                        f"fold-neutered run non-deterministic {nofold['windows_seen']}"
                else:
                    ro = nofold["insns_per_frame"]
                    fold = total["insns_per_frame"] - ro
                    cell["metrics"]["insns_per_frame_render_only"] = ro
                    cell["metrics"]["harness_fold_insns"] = fold
                    cell["fold_provenance"] = "measured-differential"
                    cell["build_type"] = ("perf+fold-in-window (pre-#44: the timed pass "
                                          "folds; render-only comes from a second build "
                                          "of the SAME firmware with the fold folding an "
                                          "empty slice, differenced)")
                    cell["provenance"]["fold_share_of_total"] = \
                        round(fold / total["insns_per_frame"], 4)
                    cell["provenance"]["render_only"] = \
                        {k: v for k, v in nofold.items() if k != "console"}
                    if fold <= 0 or fold / total["insns_per_frame"] > 0.9:
                        cell["metric_notes"]["harness_fold_insns"] = (
                            "IMPLAUSIBLE fold share — suspect the neutered build let the "
                            "optimizer eliminate the render; treat render_only as unproven"
                        )
        # --- 3. stack high-water, its own build (never the counted ELF) ---
        if do_stack and opt != "z":
            cell["metric_notes"]["stack_high_water_b"] = (
                "measured at opt-level z only: #33 swept all four levels and found the "
                "watermark invariant to +/-320 B, so a build per level buys nothing"
            )
        elif do_stack:
            hw, why = measure_stack(spec, tier, opt, cap)
            cell["metrics"]["stack_high_water_b"] = hw
            if why:
                cell["metric_notes"]["stack_high_water_b"] = why
        else:
            cell["metric_notes"]["stack_high_water_b"] = (
                "not measured this run (--stack): the probe needs its own build, and "
                "#33 measured the watermark opt-level- and tier-invariant to +/-320 B"
            )
    finally:
        wl.write_text(original)

    global QI
    QI = QI or _qemu_insn_module()
    ident = QI.qemu_identity()
    cell["provenance"]["instrument"] = {
        "qemu": ident,
        "plugin": str(QI.plugin_path()),
        "plugin_args": "match=bkpt,trace=on",
        "machine": QI.MACHINE,
        "icount": "shift=0,sleep=off",
        "build": " ".join(["cargo", "build", *mcu_build_args(tier, opt)]),
        "cargo_incremental": "0",
    }
    cell["status"] = "measured"
    return cell


def measure_host(spec: Path, tier: str, cap: dict, bench: dict | None) -> dict:
    cell = blank_cell("host", tier, "3")  # cargo `release` = opt-level 3
    cell["opt_level"] = "3 (cargo `release`)"
    cell["metric_notes"] = {
        "insns_per_frame_total": "no instruction counter on the host leg (libinsn is a QEMU plugin)",
        "harness_fold_insns": "no instruction counter on the host leg",
        "insns_per_frame_render_only": "no instruction counter on the host leg",
        "cycles_per_frame": "DWT_CYCCNT is real silicon only",
        "flash_text_data_b": "not an MCU target",
    }
    if not cap["tiers"].get(tier):
        cell["reason"] = f"tier `{tier}` does not exist at this ref"
        return cell
    r = cargo_build(spec, ["--release", "-p", "vyr-size", "--no-default-features",
                           "--features", TIER_FEATURE[tier]])
    if r.returncode != 0:
        cell["status"] = "failed"
        cell["reason"] = "host build failed: " + (r.stderr[-600:] or r.stdout[-600:])
        return cell
    exe = spec / "target" / "release" / "vyr-size"
    run = sh([str(exe)], cwd=spec)
    console = (run.stdout + run.stderr).splitlines()
    facts = _console_facts(console)
    cell["metrics"]["frame_hash"] = facts.get("frame_hash")
    cell["metrics"]["heap_peak_b"] = facts.get("heap_peak_b")
    if bench is not None:
        ns = bench.get(f"scene/panel_{tier}")
        if ns is None:
            cell["metric_notes"]["ns_per_frame"] = \
                f"vyr-bench has no scene/panel_{tier} case at this ref"
        else:
            cell["metrics"]["ns_per_frame"] = round(ns, 1)
            cell["metrics"]["ns_per_px"] = round(ns / SCENE_PX, 4)
    else:
        cell["metric_notes"]["ns_per_frame"] = "host bench not run (--no-host-bench)"
    cell["provenance"]["guest_console"] = console
    cell["provenance"]["instrument"] = {
        "build": f"cargo build --release -p vyr-size --no-default-features "
                 f"--features {TIER_FEATURE[tier]}",
        "runner": str(exe),
        "cpu": _cpu_model(),
    }
    cell["status"] = "measured"
    return cell


def host_bench(spec: Path) -> dict | None:
    """`vyr-bench only scene/panel_` — the 480x270 panel scene at every tier the
    ref registers, in one process. ns/iter per case."""
    r = sh(["cargo", "run", "--release", "-p", "vyr-bench", "--", "only", "scene/panel_"],
           cwd=spec, env_extra={"CARGO_INCREMENTAL": "0"})
    if r.returncode != 0:
        log(f"  host bench unavailable at this ref (rc={r.returncode})")
        return None
    # vyr-bench logs "<stamp> UTC INFO [vyr-bench] scene/panel_exact  249820.4 ns/iter"
    # on stderr; anchor on the case name, never on the line start.
    out = {}
    for m in re.finditer(r"((?:scene|prim)/\S+)\s+([\d.]+) ns/iter", r.stdout + r.stderr):
        out[m.group(1)] = float(m.group(2))
    return out or None


def measure_arm32(spec: Path, tier: str, cap: dict) -> dict:
    cell = blank_cell("arm32", tier, "3")
    cell["opt_level"] = "3 (cargo `release`)"
    cell["metric_notes"] = {
        "insns_per_frame_total": "the libinsn plugin is a system-mode QEMU plugin; "
                                 "qemu-arm-static here is stock user-mode with no plugin",
        "harness_fold_insns": "no instruction counter on this leg",
        "insns_per_frame_render_only": "no instruction counter on this leg",
        "cycles_per_frame": "DWT_CYCCNT is real silicon only",
        "ns_per_frame": "emulated wall time measures the host TCG, not a target",
        "ns_per_px": "emulated wall time measures the host TCG, not a target",
        "flash_text_data_b": "hosted musl binary, not a flash image",
    }
    if not cap["tiers"].get(tier):
        cell["reason"] = f"tier `{tier}` does not exist at this ref"
        return cell
    # rust-lld + the toolchain's self-contained musl runtime: no cross-gcc, fully
    # static. The host `cc`/ld.bfd cannot link an ARM object ("relocations in
    # generic ELF (EM: 40)"), which is what dev.py's cross-ISA rung learned.
    r = sh(["cargo", "build", "--release", "-p", "vyr-size", "--target", ARM32_TARGET,
            "--no-default-features", "--features", TIER_FEATURE[tier]],
           cwd=spec, env_extra={
               "CARGO_INCREMENTAL": "0",
               "CARGO_TARGET_ARMV7_UNKNOWN_LINUX_MUSLEABIHF_LINKER": "rust-lld"})
    if r.returncode != 0:
        cell["status"] = "failed"
        cell["reason"] = "arm32 build failed: " + (r.stderr[-600:] or r.stdout[-600:])
        return cell
    exe = spec / "target" / ARM32_TARGET / "release" / "vyr-size"
    run = sh([QEMU_ARM_STATIC, str(exe)], cwd=spec)
    console = (run.stdout + run.stderr).splitlines()
    facts = _console_facts(console)
    cell["metrics"]["frame_hash"] = facts.get("frame_hash")
    cell["metrics"]["heap_peak_b"] = facts.get("heap_peak_b")
    cell["provenance"]["guest_console"] = console
    cell["provenance"]["instrument"] = {"runner": QEMU_ARM_STATIC, "target": ARM32_TARGET}
    cell["status"] = "measured"
    return cell


def measure_board(spec: Path, tiers: list[str], cap: dict) -> list[dict]:
    """Real silicon. Delegates to scripts/board-run.py (the flashing, clock
    verification and DWT bookkeeping live there) under tmp/.board.lock, because
    another agent may hold the probe."""
    cells = [blank_cell("board", t, "z") for t in tiers]
    for c in cells:
        c["metric_notes"] = {
            "insns_per_frame_total": "no instruction counter on silicon; the M4 has no PMU here",
            "harness_fold_insns": "no instruction counter on silicon",
            "insns_per_frame_render_only": "no instruction counter on silicon",
            "ns_per_frame": "recorded as cycles; ms/frame is cycles/180 MHz",
            "ns_per_px": "recorded as cycles",
        }
    runner = (spec / "scripts" / "board-run.py")
    if not runner.exists():
        runner = REPO / "scripts" / "board-run.py"
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        from board_lock import BoardLock  # noqa: E402
    except ImportError:
        for c in cells:
            c["reason"] = "scripts/board_lock.py not importable — refusing to flash unlocked"
        return cells
    # tmp/.board.lock is resolved per CHECKOUT. Run from a linked worktree it
    # cannot mutex against an agent working in the primary checkout, so the
    # lock would be theatre. Refuse rather than flash a board someone else may
    # be halfway through programming.
    if (REPO / ".git").is_file() and not os.environ.get("VYR_BOARD_FORCE"):
        for c in cells:
            c["reason"] = (
                "declined: this harness is running in a linked git worktree, and "
                "tmp/.board.lock is per-checkout — it cannot mutex against an agent "
                "using the probe from the primary checkout. Run the board leg from the "
                "primary checkout, or set VYR_BOARD_FORCE=1 when the probe is known idle."
            )
        return cells
    out = TMP / "perf-harness-board.json"
    try:
        lock = BoardLock("perf-harness", log=log)
        lock.__enter__()
    except Exception as e:
        for c in cells:
            c["reason"] = f"board lock not acquired: {type(e).__name__}: {e}"
        return cells
    try:
        r = sh([sys.executable, str(runner), "--both", "--repeat", "3", "--out", str(out)],
               cwd=spec, deadline=3600)
    finally:
        lock.__exit__(None, None, None)
    if r.returncode != 0 or not out.exists():
        for c in cells:
            c["status"] = "failed"
            c["reason"] = f"board-run.py rc={r.returncode}: {(r.stderr or r.stdout)[-400:]}"
        return cells
    data = json.loads(out.read_text())
    for c in cells:
        t = (data.get("tiers") or {}).get(c["tier"].capitalize())
        if not t:
            c["reason"] = f"board-run.py did not report tier {c['tier']}"
            continue
        c["metrics"]["cycles_per_frame"] = (t.get("cycles_per_frame") or {}).get("median")
        c["metrics"]["heap_peak_b"] = t.get("heap_peak")
        c["metrics"]["frame_hash"] = t.get("reference_hash")
        c["provenance"]["instrument"] = {
            "board": data.get("board"), "probe": data.get("probe"),
            "timer": data.get("timer", "DWT_CYCCNT"), "runner": str(runner),
        }
        c["provenance"]["raw"] = t
        c["status"] = "measured"
    return cells


LVGL_RUNNER = REPO / "scripts" / "lvgl-m4-bench" / "run.py"
LVGL_MAIN = REPO / "scripts" / "lvgl-m4-bench" / "main.c"
LVGL_FOLD_NEEDLE = "for (uint32_t i = 0; i < n; i++) {"
LVGL_FOLD_PATCH = "for (uint32_t i = 0; i < 0; i++) { /* perf-harness fold differential */"


def measure_lvgl(repeat: int, cache: dict | None) -> dict:
    """The LVGL anchor, measured by the SAME instrument and with its fold priced
    the SAME way (differential rebuild) as vyr's.

    Deliberately NOT replayed over history: the anchor's own harness changed at
    `56da347` (it had been drawing a tick-marked scale, labels and a knob that
    vyr never drew), so earlier LVGL numbers measure a different SCENE, and the
    anchor tracks current upstream by design — a "series" would mix upstream
    drift with harness change and mean nothing. One anchor, current, per run.
    """
    cell = blank_cell("qemu-m4", "lvgl", "s (-Os, the anchor's own level)", firmware="lvgl")
    cell["fold_provenance"] = "not-measured"
    cell["build_type"] = ("perf+fold-in-window — the LVGL harness still folds inside its "
                          "flush_cb (#44's LVGL half is outstanding), so this row is NOT "
                          "like-for-like with a post-#44 vyr row unless render-only is used")
    cell["metric_notes"] = {
        "cycles_per_frame": "DWT_CYCCNT is real silicon only",
        "ns_per_frame": "emulated wall time is host TCG time",
        "ns_per_px": "emulated wall time is host TCG time",
        "stack_high_water_b": "the vyr stack probe does not exist in the LVGL vehicle",
    }
    if not LVGL_RUNNER.exists():
        cell["reason"] = "scripts/lvgl-m4-bench/run.py absent"
        return cell
    original = LVGL_MAIN.read_text()
    if LVGL_FOLD_NEEDLE not in original:
        cell["reason"] = "the LVGL fold loop is not present verbatim in main.c"
        return cell
    elf = TMP / "lvgl-m4.elf"
    keep = TMP / "lvgl-m4-anchor.elf"
    try:
        r = sh([sys.executable, str(LVGL_RUNNER)], cwd=REPO, deadline=3600)
        if r.returncode != 0 or not elf.exists():
            cell["status"] = "failed"
            cell["reason"] = f"lvgl-m4-bench/run.py rc={r.returncode}: {(r.stdout + r.stderr)[-500:]}"
            return cell
        shutil.copy2(elf, keep)
        total = count_insns(keep, "ph-lvgl-fold", repeat, cache)
        if not total["deterministic"]:
            cell["status"] = "failed"
            cell["reason"] = f"non-deterministic window {total['windows_seen']}"
            return cell
        cell["metrics"]["insns_per_frame_total"] = total["insns_per_frame"]
        facts = _console_facts(total["console"])
        cell["metrics"]["frame_hash"] = facts.get("frame_hash")
        fsz, notes = flash_size(keep)
        cell["metrics"]["flash_text_data_b"] = fsz
        cell["metric_notes"].update(notes)
        cell["provenance"]["total"] = {k: v for k, v in total.items() if k != "console"}
        cell["provenance"]["guest_console"] = total["console"]
        res = TMP / "lvgl-m4-result.json"
        if res.exists():
            d = json.loads(res.read_text())
            cell["provenance"]["upstream"] = d.get("lvgl")
            cell["provenance"]["build"] = d.get("build") or d.get("opt")
            hp = (d.get("mem") or {}).get("peak_b") or d.get("heap_peak_b")
            if hp:
                cell["metrics"]["heap_peak_b"] = hp

        if total.get("timed_passes") == 2:
            # #44 landed on the LVGL side too: the anchor measures its own split,
            # so the fold-neutered rebuild below is not run at all.
            cell["metrics"]["insns_per_frame_total"] = total["with_fold_per_frame"]
            cell["metrics"]["insns_per_frame_render_only"] = total["render_only_per_frame"]
            cell["metrics"]["harness_fold_insns"] = total["fold_per_frame"]
            cell["fold_provenance"] = "measured-in-cell (#44 two-pass)"
            cell["build_type"] = ("perf (flush_cb folds behind a flag; second timed "
                                  "pass prices the fold; untimed verification first)")
            cell["provenance"]["fold_share_of_total"] = (
                round(total["fold_per_frame"] / total["with_fold_per_frame"], 4)
                if total["with_fold_per_frame"] else None)

        else:
            # Pre-#44 anchor: the fold is inside the timed window, so render-only
            # can only be had by rebuilding with it neutered and differencing.
            LVGL_MAIN.write_text(original.replace(LVGL_FOLD_NEEDLE, LVGL_FOLD_PATCH))
            r = sh([sys.executable, str(LVGL_RUNNER)], cwd=REPO, deadline=3600)
            if r.returncode != 0:
                cell["metric_notes"]["harness_fold_insns"] = "fold-neutered LVGL build failed"
            else:
                nofold = count_insns(elf, "ph-lvgl-nofold", repeat, cache)
                if nofold["deterministic"]:
                    ro = nofold["insns_per_frame"]
                    cell["metrics"]["insns_per_frame_render_only"] = ro
                    cell["metrics"]["harness_fold_insns"] = total["insns_per_frame"] - ro
                    cell["fold_provenance"] = "measured-differential"
                    cell["provenance"]["fold_share_of_total"] = round(
                        (total["insns_per_frame"] - ro) / total["insns_per_frame"], 4)
                    cell["provenance"]["render_only"] = {k: v for k, v in nofold.items()
                                                         if k != "console"}
                else:
                    cell["metric_notes"]["harness_fold_insns"] = \
                        f"fold-neutered run non-deterministic {nofold['windows_seen']}"
    finally:
        LVGL_MAIN.write_text(original)
        if keep.exists():
            shutil.copy2(keep, elf)  # leave the PUBLISHED anchor ELF in place
    global QI
    QI = QI or _qemu_insn_module()
    cell["provenance"]["instrument"] = {
        "qemu": QI.qemu_identity(), "plugin": str(QI.plugin_path()),
        "plugin_args": "match=bkpt,trace=on", "machine": QI.MACHINE,
        "icount": "shift=0,sleep=off", "build": "scripts/lvgl-m4-bench/run.py (stock mirror)",
    }
    cell["status"] = "measured"
    return cell


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return _platform.processor() or "unknown"


# --- the run ----------------------------------------------------------------


def run(ref: str, platforms: list[str], tiers: list[str], opts: list[str], repeat: int,
        do_fold: bool, in_place: bool, do_host_bench: bool, cache: dict | None,
        do_stack: bool = False, do_lvgl: bool = False) -> dict:
    spec, ident = prepare_specimen(ref, in_place)
    cap = capabilities(spec)
    ident["build_key"] = build_key(spec)
    have, why = probe_platforms(spec, cap, platforms)

    log(f"specimen {ident['commit']} {ident['subject'][:60]}")
    log(f"  build key {ident['build_key']}  tiers available: "
        f"{[t for t, v in cap['tiers'].items() if v]}")
    for p in platforms:
        log(f"  platform {p:8s} {'AVAILABLE  ' + have[p] if p in have else 'unavailable — ' + why[p]}")

    bench = None
    if "host" in have and do_host_bench:
        bench = host_bench(spec)

    cells: list[dict] = []
    ref_hash: dict[str, str] = {}  # tier -> host frame hash, the cross-ISA reference
    # host first, always: its frame hash is the reference every other platform's
    # build is checked against before its timing is allowed to count.
    ordered = sorted(platforms, key=lambda p: 0 if p == "host" else 1)
    for tier in tiers:
        for plat in ordered:
            if plat in why:
                for opt in (opts if plat == "qemu-m4" else ["z"]):
                    c = blank_cell(plat, tier, opt)
                    c["reason"] = why[plat]
                    cells.append(c)
                continue
            if plat == "qemu-m4":
                for opt in opts:
                    t0 = time.monotonic()
                    c = measure_qemu_m4(spec, tier, opt, cap, repeat, do_fold, cache,
                                        do_stack, ref_hash.get(tier))
                    c["wall_s"] = round(time.monotonic() - t0, 1)
                    m = c["metrics"]
                    log(f"  [{c['status']}] qemu-m4/{tier}/O{opt}: "
                        f"total={m['insns_per_frame_total']} fold={m['harness_fold_insns']} "
                        f"render_only={m['insns_per_frame_render_only']} "
                        f"hash={m['frame_hash']} ({c['wall_s']}s) {c['reason'] or ''}")
                    cells.append(c)
            elif plat == "host":
                c = measure_host(spec, tier, cap, bench)
                if c["metrics"].get("frame_hash"):
                    # the cross-ISA reference: an M4 build whose hash differs from
                    # this renders the wrong pixels and must not report a timing
                    ref_hash[tier] = c["metrics"]["frame_hash"]
                log(f"  [{c['status']}] host/{tier}: hash={c['metrics']['frame_hash']} "
                    f"heap={c['metrics']['heap_peak_b']} ns/frame={c['metrics']['ns_per_frame']}")
                cells.append(c)
            elif plat == "arm32":
                c = measure_arm32(spec, tier, cap)
                log(f"  [{c['status']}] arm32/{tier}: hash={c['metrics']['frame_hash']}")
                cells.append(c)
    if do_lvgl and "qemu-m4" in have:
        c = measure_lvgl(repeat, cache)
        m = c["metrics"]
        log(f"  [{c['status']}] qemu-m4/LVGL anchor: total={m['insns_per_frame_total']} "
            f"fold={m['harness_fold_insns']} render_only={m['insns_per_frame_render_only']} "
            f"{c['reason'] or ''}")
        cells.append(c)

    if "board" in have:
        bcells = measure_board(spec, tiers, cap)
        for c in bcells:
            log(f"  [{c['status']}] board/{c['tier']}: cycles={c['metrics']['cycles_per_frame']}")
        cells += bcells

    return {
        "harness": {
            "name": "scripts/perf-harness.py",
            "version": HARNESS_VERSION,
            "sha256": HARNESS_SHA256,
            "instrument_repo_commit": git("rev-parse", "HEAD"),
            "argv": sys.argv[1:],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "host": _platform.node(),
            "cpu": _cpu_model(),
            "arch": _platform.machine(),
            "principle": "the harness is the instrument and is held constant; only "
                         "the renderer varies. Old commits are measured with TODAY's tools.",
        },
        "specimen": ident,
        "capabilities": cap,
        "axes": {
            "platform": platforms,
            "tier": tiers,
            "opt_level": opts,
            "note": "word_bits and float precision are PROPERTIES of the platform, not "
                    "free axes — they are recorded per cell. x86-64 has hardware f64 and "
                    "the M4F does not; that asymmetry is what the matrix exists to expose.",
        },
        "platforms_attempted": platforms,
        "platforms_available": have,
        "platforms_unavailable": why,
        "scene": {"w": 480, "h": 270, "px": SCENE_PX, "band_h": 16},
        "cells": cells,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--platforms", default="host,qemu-m4",
                    help="host,qemu-m4,arm32,board (default host,qemu-m4)")
    ap.add_argument("--all-platforms", action="store_true",
                    help="host,qemu-m4,arm32,board — board is opt-in because it flashes hardware")
    ap.add_argument("--tiers", default="exact,fast,draft")
    ap.add_argument("--opt-levels", default="z",
                    help="release-mcu opt-level sweep, e.g. z,s,2,3 (#33)")
    ap.add_argument("--repeat", type=int, default=2,
                    help="instruction measurements repeated N times; disagreement = FAILED cell")
    ap.add_argument("--no-fold", action="store_true",
                    help="skip the differential fold build (halves the M4 builds, "
                         "and gives up render-only — the headline number)")
    ap.add_argument("--no-host-bench", action="store_true")
    ap.add_argument("--lvgl", action="store_true",
                    help="also measure the LVGL anchor with the SAME instrument and the "
                         "SAME fold differential (current upstream mirror; never replayed "
                         "over history — its own scene changed at 56da347)")
    ap.add_argument("--stack", action="store_true",
                    help="also measure the stack high-water (#33 stack-probe) — its own "
                         "build per cell, so it is opt-in")
    ap.add_argument("--in-place", action="store_true",
                    help="measure THIS checkout instead of a detached specimen worktree")
    ap.add_argument("--cache", default=None,
                    help="JSON file mapping ELF sha256 -> counts; a byte-identical ELF is "
                         "provably redundant to re-measure")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    TMP.mkdir(parents=True, exist_ok=True)
    platforms = ["host", "qemu-m4", "arm32", "board"] if a.all_platforms else \
        [p.strip() for p in a.platforms.split(",") if p.strip()]
    tiers = [t.strip() for t in a.tiers.split(",") if t.strip()]
    opts = [o.strip() for o in a.opt_levels.split(",") if o.strip()]

    cache = None
    cache_path = Path(a.cache) if a.cache else None
    if cache_path:
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    log("=" * 72)
    log(f"{HARNESS_VERSION}: ref={a.ref} platforms={platforms} tiers={tiers} "
        f"opt={opts} repeat={a.repeat} fold={not a.no_fold}")
    rec = run(a.ref, platforms, tiers, opts, a.repeat, not a.no_fold, a.in_place,
              not a.no_host_bench, cache, a.stack, a.lvgl)
    if cache_path is not None:
        cache_path.write_text(json.dumps(cache))
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", a.ref)
    dest = Path(a.out) if a.out else TMP / f"perf-harness-{slug}.json"
    dest.write_text(json.dumps(rec, indent=2) + "\n")
    log(f"wrote {dest}")
    bad = [c for c in rec["cells"] if c["status"] == "failed"]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

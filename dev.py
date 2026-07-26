#!/usr/bin/env python3
"""dev.py — canonical entry point for vyr (awto dev.py convention).

AI agents: discover commands via `./dev.py describe` (JSON).
All output is timestamped and mirrored to ./tmp/dev.log.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
TMP = REPO / "tmp"

COMMANDS = {
    "describe": "list commands as JSON (for agents)",
    "test": "cargo test --workspace (--bless prints new golden hashes; --dump writes PNGs to ./tmp/)",
    "check": "cargo check --workspace",
    "check-mcu": "cargo check -p vyr-core -p vyr-scene --target thumbv7em-none-eabihf (the no_std gate, invariant I7)",
    "perf-suite": "#43: print the fingerprint of WHAT the perf ledger measures (vyr fixture + band height + frame counts + tiers + the LVGL scene/config/commit). --compare exits 1 if it differs from the ledger stamp — the tests changed, REPROCESS the ledger (never append across a suite change).",
    "check-f64": "#39: gate the unstripped release-mcu build against soft-f64 creep — the M4F FPU is single-precision, so a stray f64 is thousands of insns/call (#32). Compares the linked double-precision runtime helpers against vyr-size/f64-baseline.json; a new one fails. --bless re-records. Deeper hot-path check: scripts/m4-attribute.py.",
    "clippy": "cargo clippy --workspace --all-targets",
    "fmt-check": "cargo fmt --check",
    "selftest": "render the demo scene to ./tmp/selftest.png via vyr-cli (logs ns/px + counters)",
    "bench": "perf gate: release vyr-bench check vs committed baseline + scaling-law assertion",
    "bench-record": "re-record vyr-bench/baseline.json (a reviewed act — commit it separately)",
    "size-mcu": "F9 static numbers: build the vyr-size matrix for thumbv7em + arm-none-eabi-size table",
    "qemu-m4": "F9 dynamic half: boot vyr-size (run-qemu) on qemu-system-arm netduinoplus2 (M4), banded 480x270 + subset font; cross-ISA hash vs the host leg; icount virtual-time numbers. GATES insns/frame + frame hash vs vyr-size/m4-baseline.json (--draft = Draft tier, --fast = Fast tier #27; --bless re-records — commit separately)",
    "anim": "F18 rig acceptance: 600-frame run @480 — per-frame incremental==full + committed hash chain + PNG seq + ffmpeg lossless video (--bless re-bless; --arm qemu cross-ISA rung; --size/--frames override; --no-video)",
    "ci": "THE single comprehensive gate (run after every change): gate + M4 insn-count gate + bench + size-mcu + anim(+ARM) + ladder + track, run-all-collect-all, one summary. --quick (~1-2 min): Draft-only M4, 60 anim frames, no video/ARM. Full run = nightly unit.",
    "ladder": "F18 resolution ladder 120→4K: ns/px full+incremental, 60fps headroom, dirty %; gates vs vyr-rig/baseline.json when present (--record re-records — commit separately)",
    "track": "THE measurement ledger (#25, schema 3): append one row for the current commit to docs/perf/history.jsonl from whatever measurement artifacts are in ./tmp (the perf-harness MATRIX, ladder/anim/arm, size, qemu-m4, board silicon cycles) + committed bench medians, then regenerate docs/perf/index.html (flat sortable tables of every recorded value). Measures nothing itself; sections absent from ./tmp are simply not recorded. Instruction counts come ONLY from tmp/perf-harness-HEAD.json (scripts/perf-harness.py), which records render-only, the benchmark's own hash fold and the total as separate fields. --regen-only rebuilds the page without appending; --rebuild-from-replay <jsonl> rebuilds the whole file from a history replay.",
    "probe": "#37 painter geometry probe (DIAGNOSTIC, gates nothing): build vyr-size --features probe and price a sweep of synthetic scenes on the emulated M4 in which draws / 16-px pipeline chunks / painted pixels vary INDEPENDENTLY, then fit cost = a·draws + b·chunks + c·px. Separates per-fill_path setup from pipeline structure from irreducible per-pixel work — the decomposition no whole-frame number can produce. --tiers exact,fast,draft; --opt z|s|3; --band-h 8,16,32 (the working-set axis); --attribute (per-symbol + memory-traffic share); --host (same cases on x86-64 under callgrind). See docs/design/painter-simd-tax.md.",
    "insn-mix": "#37 exact instruction-CLASS x symbol attribution (hotblocks blocks x static disassembly, self-checking to 100.0000%): how much of each symbol is load/store (spill) vs arithmetic, plus an exact call census attributing memcpy and OUTLINED_FUNCTION_* to their CALLERS. Consumes the logs scripts/m4-attribute.py leaves in tmp/m4-attribute — no qemu time of its own.",
    "gate": "the full pre-commit gate: fmt-check + clippy + test + check-mcu",
}


def _log(msg: str) -> None:
    # awto convention: HH:MM:SS.ffffff UTC  LEVEL [origin] message,
    # mirrored to stderr and ./tmp/dev.log (append).
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1e6):06d} UTC"
    line = f"{stamp}  INFO  [dev.py] {msg}"
    print(line, file=sys.stderr)
    TMP.mkdir(exist_ok=True)
    with open(TMP / "dev.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _run(args: list[str], env_extra: dict[str, str] | None = None) -> int:
    # env vars are passed HERE (never as command-line prefixes) so invocations
    # stay plain-token commands that permission allowlists can match.
    import os

    env = None
    if env_extra:
        env = dict(os.environ, **env_extra)
        _log("env: " + " ".join(f"{k}={v}" for k, v in env_extra.items()))
    _log("run: " + " ".join(args))
    rc = subprocess.call(args, cwd=REPO, env=env)
    _log(f"rc={rc}: " + " ".join(args))
    return rc


def cmd_describe(_rest: list[str]) -> int:
    print(json.dumps({"project": "vyr", "commands": COMMANDS}, indent=2))
    return 0


def cmd_test(rest: list[str]) -> int:
    env: dict[str, str] = {}
    if "--bless" in rest:
        rest = [a for a in rest if a != "--bless"]
        env["VYR_BLESS"] = "1"
        rest = [*rest, "--", "--nocapture"] if "--" not in rest else rest
    if "--dump" in rest:
        rest = [a for a in rest if a != "--dump"]
        env["VYR_TEST_DUMP"] = "1"
    return _run(["cargo", "test", "--workspace", *rest], env_extra=env or None)


def cmd_check(rest: list[str]) -> int:
    return _run(["cargo", "check", "--workspace", *rest])


def cmd_check_mcu(rest: list[str]) -> int:
    # vyr-scene is gated here too: it is `no_std + alloc` for exactly one
    # reason — the emulated-M4 vehicle animates the SAME scene the host rig
    # does — so a std leak into it silently costs the cross-ISA comparison,
    # and that is the kind of breakage a gate should catch on the host.
    for pkg in ("vyr-core", "vyr-scene"):
        rc = _run(
            ["cargo", "check", "-p", pkg, "--target", "thumbv7em-none-eabihf", *rest]
        )
        if rc != 0:
            return rc
    return 0


def cmd_check_f64(rest: list[str]) -> int:
    # #39: the M4F FPU is single-precision, so any f64 op is a soft-float call
    # (#32: 11.4 M insns/frame, invisible on the host). check-mcu proves the
    # crate COMPILES; this proves it did not pull in a new double-precision
    # runtime helper vs vyr-size/f64-baseline.json. --bless re-records.
    return _run([sys.executable, str(REPO / "scripts" / "check-f64.py"), *rest])


def cmd_perf_suite(rest: list[str]) -> int:
    # #43: print the fingerprint of WHAT the perf ledger measures (the vyr
    # fixture, band height, frame counts, tiers, the LVGL scene + config +
    # upstream commit). --compare exits 1 if it differs from the ledger's stamp
    # — the tests changed, so the ledger must be REPROCESSED, not appended to.
    return _run([sys.executable, str(REPO / "scripts" / "perf-suite.py"), *rest])


def cmd_clippy(rest: list[str]) -> int:
    return _run(["cargo", "clippy", "--workspace", "--all-targets", *rest])


def cmd_fmt_check(rest: list[str]) -> int:
    return _run(["cargo", "fmt", "--check", *rest])


def cmd_selftest(rest: list[str]) -> int:
    out = str(TMP / "selftest.png")
    TMP.mkdir(exist_ok=True)
    return _run(["cargo", "run", "-p", "vyr-cli", "--", "selftest-png", out, *rest])


def cmd_bench(rest: list[str]) -> int:
    # Release ALWAYS: debug timings are not baselines. CARGO_INCREMENTAL=0 so
    # ns/px codegen is build-order-independent (same reason as qemu-m4).
    return _run(
        ["cargo", "run", "--release", "-p", "vyr-bench", "--", "check", *rest],
        env_extra={"CARGO_INCREMENTAL": "0"},
    )


def cmd_bench_record(rest: list[str]) -> int:
    return _run(["cargo", "run", "--release", "-p", "vyr-bench", "--", "record", *rest])


# --- F9 static size (issue #9) -------------------------------------------

SIZE_TARGET = "thumbv7em-none-eabihf"
# (config label, cargo profile, feature flags). Features select baked ASSETS,
# not code — the IR interpreter's full vocabulary is statically reachable
# from render() in every config (vyr-size/src/main.rs module docs).
SIZE_MATRIX = [
    ("code-only", "release", ["--no-default-features"]),
    ("code-only", "release-mcu", ["--no-default-features"]),
    ("font", "release", ["--no-default-features", "--features", "font"]),
    ("font", "release-mcu", ["--no-default-features", "--features", "font"]),
    ("font,image", "release", ["--no-default-features", "--features", "font,image"]),
    ("font,image", "release-mcu", ["--no-default-features", "--features", "font,image"]),
]
# STM32F427 budgets (vyr-size/link.ld carries the same map).
F427_FLASH = 2 * 1024 * 1024
F427_RAM = 192 * 1024  # SRAM1+2+3; the 64K CCM is extra, not modelled
# vyr-size's bump-allocator arena: measurement scaffolding in .bss, netted
# out in the "static RAM net" column (keep in sync with ARENA_BYTES in
# vyr-size/src/main.rs).
SIZE_ARENA = 64 * 1024


def cmd_size_mcu(_rest: list[str]) -> int:
    import shutil

    size_tool = shutil.which("arm-none-eabi-size")
    if size_tool is None:
        _log("ERROR: arm-none-eabi-size not on PATH (need the ARM GNU toolchain)")
        return 1
    rows: list[tuple[str, str, int, int, int]] = []
    for label, profile, flags in SIZE_MATRIX:
        rc = _run(
            [
                "cargo",
                "build",
                "-p",
                "vyr-size",
                "--target",
                SIZE_TARGET,
                "--profile",
                profile,
                *flags,
            ]
        )
        if rc != 0:
            _log(f"size-mcu FAILED building {label} / {profile}")
            return rc
        elf = REPO / "target" / SIZE_TARGET / profile / "vyr-size"
        out = subprocess.run(
            [size_tool, str(elf)], capture_output=True, text=True, cwd=REPO, check=False
        )
        if out.returncode != 0:
            _log(f"ERROR: {size_tool} rc={out.returncode}: {out.stderr.strip()}")
            return out.returncode
        # Berkeley format: header line, then "text data bss dec hex filename".
        text, data, bss = (int(v) for v in out.stdout.splitlines()[1].split()[:3])
        rows.append((label, profile, text, data, bss))
        _log(f"sized {label} / {profile}: text={text} data={data} bss={bss}")
    hdr = (
        f"{'config':<11} {'profile':<11} {'text':>9} {'data':>6} {'bss':>7} "
        f"{'flash':>9} {'%2MiB':>6} {'sRAM':>7} {'net':>6} {'%192K':>6}"
    )
    lines = [
        "F9 static size — vyr-size on thumbv7em-none-eabihf (STM32F427 map)",
        hdr,
        "-" * len(hdr),
    ]
    for label, profile, text, data, bss in rows:
        flash = text + data  # .text+.rodata (+ .data load image) — all in flash
        sram = data + bss  # static RAM footprint as linked
        net = sram - SIZE_ARENA  # minus the measurement arena (scaffolding)
        lines.append(
            f"{label:<11} {profile:<11} {text:>9} {data:>6} {bss:>7} "
            f"{flash:>9} {flash / F427_FLASH:>6.1%} {sram:>7} {net:>6} "
            f"{net / F427_RAM:>6.2%}"
        )
    lines.append(
        f"(flash = text+data; sRAM = data+bss; net = sRAM - {SIZE_ARENA} B bump arena; "
        f"%RAM is net of F427's 192 KiB — working RAM model: docs/measurements/f9-static.md)"
    )
    for line in lines:
        print(line)
        _log(line)
    # Sidecar for the ledger (#25): ./dev.py track INGESTS this rather than
    # re-running the build, so one ci run measures the size exactly once.
    flash_kib = {
        key: round((text + data) / 1024, 1)
        for label, key in (("code-only", "code"), ("font", "font"),
                           ("font,image", "font_image"))
        for lbl, profile, text, data, _bss in rows
        if lbl == label and profile == "release-mcu"
    }
    (TMP / "size-mcu.json").write_text(
        json.dumps(
            {
                "tool": "arm-none-eabi-size (Berkeley text+data)",
                "target": SIZE_TARGET,
                "profile": "release-mcu",
                "flash_kib": flash_kib,
                "configs": [
                    {"label": lbl, "profile": profile, "text": text, "data": data, "bss": bss}
                    for lbl, profile, text, data, bss in rows
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


# --- F9 dynamic half: the runnable M4 vehicle under qemu-system-arm (#9) ---

# Wall-clock guard for the emulated run. Reason: the guest renders 5 banded
# 480x270 frames ≈ low-billions of guest insns; TCG with icount manages
# >10^7 insn/s even on a slow host, so minutes suffice — on expiry the guest
# is wedged (boot fault, semihosting misconfig) and is killed + FAILED.
QEMU_M4_DEADLINE_S = 300
QEMU_M4_MACHINE = "netduinoplus2"  # STM32F405: M4F + 192 KiB SRAM (the F427 budget)
# icount shift=0 → the virtual clock advances exactly 1 ns per guest insn
# (deterministic); sleep=off decouples it from host wall time. SYS_CLOCK
# deltas are therefore insn counts: 1 cs = 10^7 insns.
QEMU_M4_INSNS_PER_CS = 10_000_000
QEMU_M4_TIMED_FRAMES = 20  # keep in sync with TIMED_FRAMES in vyr-size/src/workload.rs
# F16 (#16) gap-recovery anchors for `qemu-m4 --draft`. The Exact figure is
# THIS vehicle's own measured Quality::Exact insns/frame (docs/measurements/
# scripts/qemu-insn.py). BOTH are now EXACT instruction counts from a QEMU
# built with --enable-plugins + the libinsn TCG plugin, verified
# bit-identical across idle and host-loaded runs. They replace the previous
# SYS_CLOCK-derived guesses (75 M / 10 M), which were host WALL TIME and read
# 16.9% / 8.5% high. Both are anchors for the recovered-gap %, not measured by
# this run — Draft's own insns/frame IS what the run measures.
# Re-measured 2026-07-23 in one session (scripts/tier-insns.py + one
# qemu-insn.py run for LVGL). The LVGL anchor moved 9,220,422 -> 7,112,541
# because the harness stopped drawing content vyr never had (#27 Task B: a
# tick-marked lv_scale + value arc + knob where vyr draws a plain ring). The
# Exact anchor moved 64,178,227 -> 37,832,925 over the same period. Superseding
# both here rather than leaving a stale anchor to be quoted (performance.md §5).
# 2026-07-23, #32: 64,422,179 -> 51,349,644 when curve flattening stopped
# re-running libm's f32 trig (computed in software f64 on an M4F) once per
# vertex per BAND per frame — a pure memo, every tier's frame hash unchanged.
QEMU_M4_EXACT_INSNS = 51_349_644
QEMU_M4_LVGL_INSNS = 7_112_541
# The committed per-tier M4 baseline (insns/frame + workload frame hash + heap),
# the perf safety net: `qemu-m4` gates the measured number against it — a
# regression beyond QEMU_M4_TOL FAILS, an improvement beyond it prompts a
# `--bless` re-record, and the frame hash guards pixel drift across commits.
M4_BASELINE = REPO / "vyr-size" / "m4-baseline.json"
QEMU_M4_TOL = 0.02  # ±2% insns/frame — icount is exact, so this only absorbs
# code-shape noise; a real win/regress is always well outside it.


def cmd_qemu_m4(rest: list[str]) -> int:
    import re
    import shutil

    if shutil.which("qemu-system-arm") is None:
        _log("ERROR: qemu-system-arm not on PATH (dnf install qemu-system-arm)")
        return 1
    log_path = TMP / "qemu-m4.log"
    # F16 (#16): --draft renders the workload at Quality::Draft (integer no-AA
    # fast path) — a build-time feature (the M4 binary has no env). The
    # headline insns/frame then compares Draft vs the Exact 75 M / LVGL 10 M.
    draft = "--draft" in rest
    fast = "--fast" in rest and not draft
    tier = "Draft" if draft else ("Fast" if fast else "Exact")
    feats = "run-qemu,draft" if draft else ("run-qemu,fast" if fast else "run-qemu")
    flags = ["--no-default-features", "--features", feats]
    _log(f"qemu-m4 quality tier: {tier}")

    # 1) Host reference leg: same workload, counting allocator — the x86
    #    frame hash the M4 must reproduce (plus the band==full self-check).
    # CARGO_INCREMENTAL=0 on the measured builds: incremental codegen-unit
    # boundaries shift with build ORDER (a fresh build after `--workspace`
    # clippy/test inlines differently), which moved Draft 11.5M→17.0M between
    # otherwise-identical runs. Non-incremental = reproducible insns/frame, the
    # gate's whole point. (icount itself is exact; this fixes the BINARY.)
    # #45: hash from the VERIFY build, timing from the PERF build; one feature
    # apart, same commit and tier. _boot() runs an ELF behind the wall guard.
    noincr = {"CARGO_INCREMENTAL": "0"}
    feats_verify = feats + ",verify"

    def _boot(elf: Path, label: str):
        a = [
            "qemu-system-arm", "-machine", QEMU_M4_MACHINE, "-nographic",
            "-semihosting-config", "enable=on,target=native",
            "-icount", "shift=0,sleep=off", "-kernel", str(elf),
        ]
        try:
            g = subprocess.run(a, capture_output=True, text=True, cwd=REPO,
                               check=False, timeout=QEMU_M4_DEADLINE_S)
        except subprocess.TimeoutExpired as e:
            o = e.stdout or ""
            o = o.decode(errors="replace") if isinstance(o, bytes) else o
            _append_block(log_path, f"M4 {label} (KILLED at deadline)", o)
            _log(f"ERROR: qemu hit the {QEMU_M4_DEADLINE_S}s guard ({label}); see {log_path}")
            return None
        txt = g.stdout + g.stderr
        _append_block(log_path, f"M4 {label}", txt)
        if g.returncode != 0:
            _log(f"ERROR: {label} rc={g.returncode} (SYS_EXIT error path; see {log_path})")
            return None
        return txt

    # 1) Host VERIFY leg: the x86 reference hash + band==full self-check.
    rc = _run(["cargo", "build", "--release", "-p", "vyr-size",
               "--no-default-features", "--features", feats_verify], env_extra=noincr)
    if rc != 0:
        return rc
    host = subprocess.run(
        [str(REPO / "target" / "release" / "vyr-size")],
        capture_output=True, text=True, cwd=REPO, check=False,
    )
    _append_block(log_path, "host x86-64 verify leg", host.stdout + host.stderr)
    if host.returncode != 0:
        _log(f"ERROR: host verify leg rc={host.returncode} (see {log_path})")
        return 1
    m = re.search(r"frame fnv1a=(0x[0-9a-f]{16})", host.stdout)
    if not m:
        _log("ERROR: host verify leg printed no frame hash")
        return 1
    host_hash = m.group(1)
    host_peak = _re1(r"workload ok: heap peak=(\d+) B", host.stdout)

    # 2) M4 VERIFY build: the M4 frame hash for the cross-ISA + drift gate.
    t0 = time.monotonic()
    rc = _run(["cargo", "build", "-p", "vyr-size", "--target", SIZE_TARGET,
               "--profile", "release-mcu", "--no-default-features",
               "--features", feats_verify], env_extra=noincr)
    if rc != 0:
        return rc
    vout = _boot(REPO / "target" / SIZE_TARGET / "release-mcu" / "vyr-size",
                 "verify guest")
    if vout is None:
        return 1
    g_hash = _re1(r"frame fnv1a=(0x[0-9a-f]{16})", vout)

    # 3) M4 PERF build: render-only insns (SYS_CLOCK, indicative) + heap + cov.
    #    Compiles no fold, emits no hash (#45).
    rc = _run(["cargo", "build", "-p", "vyr-size", "--target", SIZE_TARGET,
               "--profile", "release-mcu", *flags], env_extra=noincr)
    if rc != 0:
        return rc
    elf = REPO / "target" / SIZE_TARGET / "release-mcu" / "vyr-size"
    gout = _boot(elf, "perf guest")
    if gout is None:
        return 1
    wall = time.monotonic() - t0

    # 4) Result block: hash from the verify guest, timing from the perf guest.
    g_peak = _re1(r"workload ok: heap peak=(\d+) B", gout)
    g_live = _re1(r"workload ok: heap peak=\d+ B live-end=(\d+) B", gout)
    g_cs = _re1(r"timed: \d+ warmed frames in (\d+) cs virtual", gout)
    g_cov = _re1(r"F16 fast-path: \d+ / \d+ delivered px \(([\d.]+)%\)", gout)
    lines = [
        f"qemu-m4 result ({QEMU_M4_MACHINE}, icount shift=0, wall {wall:.1f}s, quality={tier}):",
        f"  frame hash   M4 {g_hash} vs x86-64 {host_hash} → "
        + ("IDENTICAL (cross-ISA, banded==full)" if g_hash == host_hash else "MISMATCH"),
        f"  heap peak    M4 {g_peak} B (live-end {g_live} B) vs x86-64 {host_peak} B",
    ]
    if g_cov is not None:
        lines.append(f"  F16 fast-path coverage {g_cov}% of delivered px (integer no-AA spans)")
    for ph in re.finditer(r"phase ([\w-]+): heap live=(\d+) B peak=(\d+) B", gout):
        lines.append(f"  phase {ph.group(1):<11} live={ph.group(2):>7} B  peak={ph.group(3):>7} B")
    if g_cs is not None:
        insns = int(g_cs) * QEMU_M4_INSNS_PER_CS
        per_frame = insns // QEMU_M4_TIMED_FRAMES
        px = 480 * 270
        est_ms = per_frame / 180e6 * 1e3
        lines += [
            f"  est insns    {g_cs} cs for {QEMU_M4_TIMED_FRAMES} warmed frames "
            f"= {insns:,} insns ({per_frame:,}/frame, {per_frame / px:.0f} insn/px) — "
            "INDICATIVE: SYS_CLOCK is wall-influenced on this qemu build (no -plugin), "
            "NOT pure icount — ±1 cs quant + host-load jitter; the DETERMINISTIC perf "
            "gates are vyr-bench + ladder (host ns/px)",
            f"  @180 MHz     ~{est_ms:.0f} ms/frame ESTIMATE — assumes CPI=1.0; a real M4's "
            "CPI, flash wait states and caches differ (calibrate on the F9 board half)",
        ]
        # F16 (#16) headline: Draft insns/frame vs the Exact 75 M anchor and
        # the LVGL ~10 M anchor — "how much of the 7.4x gap does Draft recover".
        if draft or fast:
            exact_anchor = QEMU_M4_EXACT_INSNS
            lvgl_anchor = QEMU_M4_LVGL_INSNS
            saved = exact_anchor - per_frame
            gap = exact_anchor - lvgl_anchor
            recovered = 100.0 * saved / gap if gap else 0.0
            speedup = exact_anchor / per_frame if per_frame else 0.0
            lines += [
                f"  F16 {tier:<8} {per_frame:,} insns/frame vs Exact {exact_anchor:,} "
                f"= {speedup:.2f}x; vs LVGL anchor {lvgl_anchor:,}",
                f"  F16 GAP      {tier} recovers {recovered:.0f}% of the Exact->LVGL gap "
                f"({saved:,} of {gap:,} insns/frame)",
            ]
            if fast:
                lines += [
                    "  NOTE         the LVGL anchor is NOT quality-matched and NOT "
                    "content-matched yet (#27) — do not publish a ratio against it",
                ]
    for line in lines:
        print(line)
        _log(line)

    # Sidecar for the ledger (#25). DETERMINISTIC facts only: frame hash, heap
    # peak, Draft fast-path coverage. sys_clock_cs is recorded raw and named
    # for what it is — host wall time on a plugin-less qemu, NOT instructions
    # (docs/performance.md §5) — so it can never be mistaken for an insn count.
    (TMP / f"qemu-m4-{tier.lower()}.json").write_text(
        json.dumps(
            {
                "tier": tier,
                "machine": QEMU_M4_MACHINE,
                "frame_hash": g_hash,
                "cross_isa": "identical" if g_hash == host_hash else "MISMATCH",
                "heap_peak_b": int(g_peak) if g_peak else None,
                "heap_live_end_b": int(g_live) if g_live else None,
                "fastpath_coverage_pct": float(g_cov) if g_cov else None,
                "sys_clock_cs": int(g_cs) if g_cs else None,
                "sys_clock_note": (
                    "host wall time on a qemu without TCG plugins — NOT an "
                    "instruction count; exact counts come from scripts/qemu-insn.py"
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # --- baseline gate -------------------------------------------------------
    # HARD-gate only the DETERMINISTIC outputs: the frame hash (cross-ISA M4==x86
    # AND pixel-drift vs the committed baseline) and the allocator heap peak
    # (exact, clock-independent). The insns/frame is WARN-ONLY — SYS_CLOCK is
    # wall-influenced on this plugin-less qemu, so a hard gate would false-fail
    # under host load (it did: 11.5M idle → 17M under ci build load). The real
    # perf-regression gates are vyr-bench + ladder (host ns/px, proper stats).
    # `--bless` re-records this tier.
    bless = "--bless" in rest
    per_frame = (
        int(g_cs) * QEMU_M4_INSNS_PER_CS // QEMU_M4_TIMED_FRAMES if g_cs else None
    )
    g_peak_i = int(g_peak) if g_peak else None
    rc = 0 if g_hash == host_hash else 1
    if rc:
        _log("qemu-m4 GATE FAIL: M4 frame hash != x86-64 (cross-ISA divergence)")
    base = json.loads(M4_BASELINE.read_text()) if M4_BASELINE.exists() else {}
    if bless:
        base[tier] = {
            "insns_per_frame": per_frame,  # indicative (wall-influenced)
            "frame_fnv1a": g_hash,  # deterministic — hard-gated
            "heap_peak": g_peak_i,  # deterministic — hard-gated
        }
        M4_BASELINE.write_text(json.dumps(base, indent=2, sort_keys=True) + "\n")
        _log(
            f"qemu-m4 BLESSED {tier}: hash {g_hash}, heap {g_peak_i} B, "
            f"~{per_frame:,} insns/frame (indicative) → {M4_BASELINE.name} "
            "(a reviewed act — commit it separately)"
        )
        return rc
    ref = base.get(tier)
    if ref is None:
        _log(
            f"qemu-m4: no baseline for {tier} — run "
            f"`./dev.py qemu-m4 {'--draft ' if draft else ('--fast ' if fast else '')}--bless` to record it "
            "(commit it). Not gating this run."
        )
        return rc
    # HARD: pixel determinism across commits.
    if ref.get("frame_fnv1a") and g_hash != ref["frame_fnv1a"]:
        _log(
            f"qemu-m4 GATE FAIL: {tier} frame hash {g_hash} != baseline "
            f"{ref['frame_fnv1a']} — workload pixels changed; re-bless if intended "
            "(--bless)"
        )
        rc = 1
    # HARD: allocator heap peak is exact + clock-independent.
    if ref.get("heap_peak") and g_peak_i is not None and g_peak_i != ref["heap_peak"]:
        _log(
            f"qemu-m4 GATE FAIL: {tier} heap peak {g_peak_i} B != baseline "
            f"{ref['heap_peak']} B — memory footprint changed; re-bless if intended "
            "(--bless)"
        )
        rc = 1
    # WARN-ONLY: insns/frame is the wall-influenced indicator (tracked, not gated).
    if per_frame is not None and ref.get("insns_per_frame"):
        b = ref["insns_per_frame"]
        delta = (per_frame - b) / b
        tag = (
            "REGRESSION?" if delta > QEMU_M4_TOL
            else "improvement?" if delta < -QEMU_M4_TOL
            else "~flat"
        )
        _log(
            f"qemu-m4: {tier} ~{per_frame:,} insns/frame vs baseline {b:,} "
            f"(Δ{delta * 100:+.1f}%, {tag}) — INDICATIVE only (wall-influenced); "
            "trust vyr-bench/ladder for the perf verdict"
        )
    return rc


def _re1(pattern: str, text: str):
    import re

    m = re.search(pattern, text)
    return m.group(1) if m else None


def _append_block(path: Path, title: str, body: str) -> None:
    """Append a captured output block to the run log, timestamped."""
    TMP.mkdir(exist_ok=True)
    stamp = time.strftime("%H:%M:%S", time.gmtime()) + " UTC"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"--- {stamp} {title} ---\n{body}")
    _log(f"{title}: output appended to {path}")


# --- F18 rig: anim / ladder / perf-history (issue #18) ---------------------

# The rig's resolution ladder (mirrors vyr_rig::RUNGS — 16:9, top rung is 4K).
RIG_RUNG_H = {120: 68, 240: 135, 480: 270, 960: 540, 1920: 1080, 3840: 2160}
# The committed acceptance shape: 600 frames (10 s logical 60 fps) at 480.
RIG_ACCEPT = (480, 600)
ARM_TARGET = "armv7-unknown-linux-musleabihf"
QEMU_ARM_STATIC = "/usr/bin/qemu-arm-static"


def _opt(rest: list[str], key: str, default: str) -> str:
    """Pop `--key value` from rest, returning value (or default)."""
    if key in rest:
        i = rest.index(key)
        if i + 1 >= len(rest):
            raise SystemExit(f"{key} needs a value")
        val = rest[i + 1]
        del rest[i : i + 2]
        return val
    return default


def cmd_anim(rest: list[str]) -> int:
    import shutil

    rest = list(rest)
    bless = "--bless" in rest
    arm = "--arm" in rest
    no_video = "--no-video" in rest
    rest = [a for a in rest if a not in ("--bless", "--arm", "--no-video")]
    size = int(_opt(rest, "--size", str(RIG_ACCEPT[0])))
    frames = int(_opt(rest, "--frames", str(RIG_ACCEPT[1])))
    if rest:
        _log(f"ERROR: unknown anim args: {rest}")
        return 2
    h = RIG_RUNG_H.get(size)
    if h is None:
        _log(f"ERROR: --size {size} is not a ladder rung ({sorted(RIG_RUNG_H)})")
        return 2
    dump = TMP / "anim"
    if dump.exists():
        shutil.rmtree(dump)  # stale frames from a longer run would leak into the video
    dump.mkdir(parents=True)
    args = [
        "cargo", "run", "--release", "-p", "vyr-rig", "--", "anim",
        "--size", str(size), "--frames", str(frames),
        "--dump-dir", str(dump),
        "--hashes-out", str(TMP / "rig-anim.json"),
        "--stats-out", str(TMP / "rig-anim-stats.json"),
    ]
    chain = REPO / "vyr-rig" / "hashchain.json"
    acceptance = (size, frames) == RIG_ACCEPT
    if bless and not acceptance:
        _log(f"ERROR: --bless applies only to the acceptance shape {RIG_ACCEPT}")
        return 2
    if acceptance:
        args += ["--bless" if bless else "--check", str(chain)]
    else:
        _log("non-acceptance shape — the committed hash chain is not consulted")
    rc = _run(args)
    if rc != 0:
        return rc
    if not no_video:
        rc = _anim_video(dump, size, h)
        if rc != 0:
            return rc
    if arm:
        return _anim_arm(size, frames)
    return 0


def _anim_video(dump: Path, w: int, h: int) -> int:
    """Assemble the PNG sequence into a LOSSLESS video — and PROVE it.

    Dan's rule: rig videos are never lossy-compressed, and every artifact
    DECLARES its resolution + colour depth. So:
    - the filename carries the spec (…-rgb888-60fps-ffv1.mkv),
    - the pixel format is EXPLICIT (-pix_fmt bgr0: 8-bit/channel RGB, full
      range, NO chroma subsampling — never inferred, never yuv420),
    - the container embeds a metadata comment,
    - a sidecar .json states the spec machine-readably,
    - and losslessness is VERIFIED, not asserted: frame 0 is decoded back
      out of the video and byte-compared to the source PNG's pixels.
    The video is the eyeball/demo artifact (./tmp only, never committed —
    the committed regression form is the hash chain)."""
    spec = f"{w}x{h}-rgb888-60fps-ffv1"
    out = TMP / f"anim-{spec}.mkv"
    comment = (f"vyr rig: {w}x{h}, RGB 8-bit/channel (bgr0), full range, "
               f"no chroma subsampling, 60 fps, FFV1 (mathematically lossless)")
    args = [
        "ffmpeg", "-y", "-framerate", "60", "-i", str(dump / "frame-%04d.png"),
        "-c:v", "ffv1", "-level", "3", "-pix_fmt", "bgr0",
        "-metadata", f"comment={comment}", str(out),
    ]
    _log("ffmpeg exact command: " + " ".join(args))
    log_path = TMP / "ffmpeg-anim.log"
    with open(log_path, "ab") as lf:  # ffmpeg is noisy — stream to the log file
        rc = subprocess.call(args, cwd=REPO, stdout=lf, stderr=lf)
    if rc != 0:
        _log(f"ERROR: ffmpeg rc={rc} (see {log_path})")
        return rc
    # Roundtrip proof: decode frame 0 from the video, byte-compare pixels.
    check = TMP / "anim-roundtrip-frame0.png"
    rc = subprocess.call(
        ["ffmpeg", "-y", "-i", str(out), "-vframes", "1", str(check)],
        cwd=REPO, stdout=open(log_path, "ab"), stderr=subprocess.STDOUT)
    if rc != 0:
        _log(f"ERROR: roundtrip decode rc={rc}")
        return rc
    from PIL import Image

    with Image.open(dump / "frame-0000.png") as a, Image.open(check) as b:
        same = a.convert("RGB").tobytes() == b.convert("RGB").tobytes()
    if not same:
        _log(f"ERROR: LOSSLESS VIOLATION — {out.name} frame 0 != source PNG "
             "(pixel mismatch after decode; investigate the encode chain)")
        return 1
    _log("roundtrip VERIFIED: video frame 0 == source PNG, byte-for-byte")
    sidecar = {
        "width": w, "height": h, "fps": 60, "codec": "ffv1 level 3",
        "pix_fmt": "bgr0", "color_depth": "8-bit/channel RGB, full range",
        "chroma_subsampling": "none", "lossless": True,
        "roundtrip_verified": True,
        "frames": len(list(dump.glob("frame-*.png"))),
    }
    out.with_suffix(".json").write_text(json.dumps(sidecar, indent=2) + "\n")
    _log(f"lossless video → {out} ({out.stat().st_size:,} B; spec sidecar: "
         f"{out.with_suffix('.json').name}; ffmpeg log: {log_path})")
    return 0


def _probe_qemu_insn_plugin(binary: Path):
    """HONEST probe for TCG instruction-count plugins. Fedora's
    qemu-user-static is a STATIC binary — it cannot dlopen plugins; the
    dynamically linked qemu-arm (qemu-user pkg) can IF a plugin .so is
    shipped. Trial-runs the rig with no args (usage exit = 2): reaching rc 2
    means qemu accepted -plugin. Returns (qemu, plugin_so) or None."""
    import glob

    pats = [
        "/usr/lib64/qemu/*insn*.so",
        "/usr/lib/qemu/*insn*.so",
        "/usr/lib64/qemu-plugins/*insn*.so",
        "/usr/share/qemu/plugins/*insn*.so",
    ]
    sos = [p for pat in pats for p in glob.glob(pat)]
    if not sos:
        _log(
            "qemu TCG plugins: no libinsn .so on this system (Fedora ships none "
            "for user-mode) — exact insn counts UNAVAILABLE; emulated wall time "
            "is recorded as NON-target-indicative; phase-2 path: dnf install "
            "qemu-system-arm (M4 machine + icount + semihosting)"
        )
        return None
    for qemu in ("/usr/bin/qemu-arm", QEMU_ARM_STATIC):
        if not Path(qemu).exists():
            continue
        trial = subprocess.run(
            [qemu, "-plugin", sos[0], "-d", "plugin", str(binary)],
            capture_output=True, text=True, cwd=REPO, check=False,
        )
        if trial.returncode == 2:
            _log(f"qemu TCG plugin available: {qemu} -plugin {sos[0]}")
            return (qemu, sos[0])
        _log(
            f"{qemu}: -plugin rejected (rc={trial.returncode}: "
            f"{trial.stderr.strip()[:160]})"
        )
    return None


def _anim_arm(size: int, frames: int) -> int:
    """The F18 cross-ISA rung: cross-build the rig STATIC for ARMv7-musl,
    replay the same run under qemu-arm-static, and byte-compare every frame
    hash against the x86 run just performed — closing F1's "byte-identical
    on two machines" acceptance box across an entire ISA. Emulated wall time
    is logged but NON-target-indicative (QEMU user-mode measures the host's
    TCG, never an F427); exact insn counts are recorded only if TCG plugins
    exist (probed honestly)."""
    x86_path = TMP / "rig-anim.json"
    if not x86_path.exists():
        _log("ERROR: no x86 hash run to compare against (tmp/rig-anim.json missing)")
        return 1
    out = subprocess.run(
        ["rustup", "target", "list", "--installed"],
        capture_output=True, text=True, cwd=REPO, check=False,
    )
    if ARM_TARGET not in out.stdout.split():
        rc = _run(["rustup", "target", "add", ARM_TARGET])
        if rc != 0:
            return rc
    # rust-lld + the toolchain's self-contained musl runtime: no cross-gcc,
    # fully static. Env is set HERE (never as a command-line prefix).
    rc = _run(
        ["cargo", "build", "--release", "-p", "vyr-rig", "--target", ARM_TARGET],
        env_extra={"CARGO_TARGET_ARMV7_UNKNOWN_LINUX_MUSLEABIHF_LINKER": "rust-lld"},
    )
    if rc != 0:
        return rc
    binary = REPO / "target" / ARM_TARGET / "release" / "vyr-rig"
    plugin = _probe_qemu_insn_plugin(binary)
    arm_json = TMP / "rig-anim-arm.json"
    rig_args = [
        str(binary), "anim", "--size", str(size), "--frames", str(frames),
        "--hashes-out", str(arm_json),
        "--stats-out", str(TMP / "rig-anim-arm-stats.json"),
    ]
    insn_log = TMP / "qemu-insn.log"
    if plugin:
        qemu, plugin_so = plugin
        args = [qemu, "-plugin", plugin_so, "-d", "plugin", "-D", str(insn_log), *rig_args]
    else:
        args = [QEMU_ARM_STATIC, *rig_args]
    t0 = time.time()
    rc = _run(args)
    wall = time.time() - t0
    if rc != 0:
        _log(f"ERROR: ARM rig run failed rc={rc}")
        return rc
    _log(
        f"emulated wall time {wall:.1f} s for {frames} frames — "
        "NON-target-indicative (host TCG speed, not an MCU's)"
    )
    verdict: dict = {
        "size": size,
        "frames": frames,
        "wall_s_emulated": round(wall, 1),
        "wall_note": "NON-target-indicative: qemu user-mode measures the host TCG",
        "insn": None,
    }
    if plugin and insn_log.exists():
        text = insn_log.read_text(errors="replace")
        digits = [int(s) for s in text.replace(",", " ").split() if s.isdigit()]
        verdict["insn"] = max(digits) if digits else None
        _log(f"TCG insn plugin output ({insn_log}): {text.strip()[:200]}")
    x86 = json.loads(x86_path.read_text())
    armr = json.loads(arm_json.read_text())
    if (
        x86["frame_hashes"] == armr["frame_hashes"]
        and x86["run_hash"] == armr["run_hash"]
    ):
        verdict["cross_isa"] = "identical"
        verdict["arch_pair"] = [x86["arch"], armr["arch"]]
        _log(
            f"ALERT cross-ISA: all {frames} frame hashes + run hash byte-identical, "
            f"{x86['arch']} vs {armr['arch']} under qemu-arm-static — F1's last "
            "acceptance box (byte-identical across machines) closes across an ISA"
        )
        rc = 0
    else:
        first = "run_hash differs"
        for i, (a, b) in enumerate(zip(x86["frame_hashes"], armr["frame_hashes"])):
            if a != b:
                first = f"frame {i}: x86 {a} vs arm {b}"
                break
        verdict["cross_isa"] = f"MISMATCH: {first}"
        _log(f"ERROR cross-ISA MISMATCH: {first} — determinism broke across ISAs")
        rc = 1
    (TMP / "rig-arm.json").write_text(json.dumps(verdict) + "\n", encoding="utf-8")
    return rc


def cmd_ladder(rest: list[str]) -> int:
    record = "--record" in rest
    rest = [a for a in rest if a != "--record"]
    args = [
        "cargo", "run", "--release", "-p", "vyr-rig", "--", "ladder",
        "--json-out", str(TMP / "rig-ladder.json"),
    ]
    if record:
        args.append("--record")
    elif (REPO / "vyr-rig" / "baseline.json").exists():
        args.append("--check")
    else:
        _log(
            "no vyr-rig/baseline.json yet — measuring without the gate "
            "(record one: ./dev.py ladder --record, commit it separately)"
        )
    return _run(args + rest)


def cmd_probe(rest: list[str]) -> int:
    # #37: the painter geometry probe. Diagnostic, never a gate — it answers
    # "what shape is the cost", not "did the cost regress".
    return _run(["python3", "scripts/painter-probe.py", *rest])


def cmd_insn_mix(rest: list[str]) -> int:
    # #37: instruction CLASS x symbol attribution, over the hotblocks logs
    # scripts/m4-attribute.py leaves in tmp/m4-attribute (no qemu time).
    return _run(["python3", "scripts/insn-mix.py", *rest])


def cmd_track(rest: list[str]) -> int:
    # ONE writer, ONE ledger (#25): docs/perf/history.jsonl + docs/perf/index.html.
    return _run(["python3", "scripts/ledger.py", *rest])


def cmd_gate(_rest: list[str]) -> int:
    # check-f64 (#39) sits beside check-mcu: check-mcu proves the MCU build
    # COMPILES; check-f64 proves it did not pull in a new soft-f64 helper.
    for step in (cmd_fmt_check, cmd_clippy, cmd_test, cmd_check_mcu, cmd_check_f64):
        rc = step([])
        if rc != 0:
            _log(f"gate FAILED at {step.__name__}")
            return rc
    _log("gate ok")
    return 0


def cmd_ci(rest: list[str]) -> int:
    """THE single operation (Dan's ask): the whole verification + measurement
    suite, run-all-collect-all — a nightly wants the COMPLETE failure list,
    not the first stumble. One summary table at the end; rc != 0 if anything
    failed. `--quick` trims the anim run (60 frames, no video, no ARM) for a
    fast pre-push sweep; the full run is the nightly unit.
    """
    import shutil

    quick = "--quick" in rest
    anim_args = (
        ["--frames", "60", "--no-video"] if quick else ["--arm"]
    )
    steps: list[tuple[str, object, list[str]]] = [
        ("gate", cmd_gate, []),
    ]
    # The M4 instruction-count gate (baselined). Skipped — not failed — when
    # qemu-system-arm is absent, so the suite stays portable. --quick gates the
    # Draft tier (the one the perf campaign moves); the full run also re-checks
    # the Exact anchor.
    if shutil.which("qemu-system-arm") is not None:
        steps.append(("m4-draft", cmd_qemu_m4, ["--draft"]))
        if not quick:
            steps.append(("m4-exact", cmd_qemu_m4, []))
    else:
        _log("ci: qemu-system-arm absent — skipping the M4 insn-count gate")
    steps += [
        ("bench", cmd_bench, []),
        ("size-mcu", cmd_size_mcu, []),
        ("anim", cmd_anim, anim_args),
        ("ladder", cmd_ladder, []),
        # The ledger row goes last: it ingests the artifacts every step above
        # just wrote (#25). Pure ingest — it re-measures nothing, so it costs
        # the same in --quick as in a full run.
        ("track", cmd_track, []),
    ]
    results: list[tuple[str, int, float]] = []
    t_all = time.monotonic()
    for name, fn, args in steps:
        _log(f"ci ── step: {name} {' '.join(args)}")
        t0 = time.monotonic()
        rc = fn(args)  # type: ignore[operator]
        results.append((name, rc, time.monotonic() - t0))
        if rc != 0:
            _log(f"ci: {name} FAILED (rc={rc}) — continuing to collect")
    failed = [n for n, rc, _ in results if rc != 0]
    lines = [
        f"ci summary ({'quick' if quick else 'full'} — {time.monotonic() - t_all:.1f}s total)",
        f"{'step':<14} {'rc':>3} {'secs':>8}",
        "-" * 28,
    ]
    for name, rc, secs in results:
        lines.append(f"{name:<14} {rc:>3} {secs:>8.1f}")
    lines.append(
        "RESULT: " + ("OK — all steps green" if not failed else f"FAILED: {', '.join(failed)}")
    )
    for line in lines:
        print(line)
        _log(line)
    return 0 if not failed else 1


HANDLERS = {
    "ci": cmd_ci,
    "describe": cmd_describe,
    "test": cmd_test,
    "check": cmd_check,
    "check-mcu": cmd_check_mcu,
    "check-f64": cmd_check_f64,
    "perf-suite": cmd_perf_suite,
    "clippy": cmd_clippy,
    "fmt-check": cmd_fmt_check,
    "selftest": cmd_selftest,
    "bench": cmd_bench,
    "bench-record": cmd_bench_record,
    "size-mcu": cmd_size_mcu,
    "qemu-m4": cmd_qemu_m4,
    "anim": cmd_anim,
    "ladder": cmd_ladder,
    "probe": cmd_probe,
    "insn-mix": cmd_insn_mix,
    "track": cmd_track,
    "gate": cmd_gate,
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        for name, desc in COMMANDS.items():
            print(f"  ./dev.py {name:10s} {desc}")
        return 2
    cmd, *rest = argv
    handler = HANDLERS.get(cmd)
    if handler is None:
        print(f"unknown command: {cmd} (try ./dev.py describe)", file=sys.stderr)
        return 2
    return handler(rest)


if __name__ == "__main__":
    sys.exit(main())

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
    "check-mcu": "cargo check -p vyr-core --target thumbv7em-none-eabihf (the no_std gate, invariant I7)",
    "clippy": "cargo clippy --workspace --all-targets",
    "fmt-check": "cargo fmt --check",
    "selftest": "render the demo scene to ./tmp/selftest.png via vyr-cli (logs ns/px + counters)",
    "bench": "perf gate: release vyr-bench check vs committed baseline + scaling-law assertion",
    "bench-record": "re-record vyr-bench/baseline.json (a reviewed act — commit it separately)",
    "size-mcu": "F9 static numbers: build the vyr-size matrix for thumbv7em + arm-none-eabi-size table",
    "anim": "F18 rig acceptance: 600-frame run @480 — per-frame incremental==full + committed hash chain + PNG seq + ffmpeg lossless video (--bless re-bless; --arm qemu cross-ISA rung; --size/--frames override; --no-video)",
    "ci": "THE single operation: gate + bench + size-mcu + anim(+ARM) + ladder + perf-history, run-all-collect-all, one summary (--quick: 60 frames, no video/ARM)",
    "ladder": "F18 resolution ladder 120→4K: ns/px full+incremental, 60fps headroom, dirty %; gates vs vyr-rig/baseline.json when present (--record re-records — commit separately)",
    "perf-history": "append the latest rig run (tmp/rig-*.json) to docs/perf/history.jsonl + regenerate docs/perf/index.html SVG charts (--regen-only rebuilds pages without appending)",
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
    return _run(
        ["cargo", "check", "-p", "vyr-core", "--target", "thumbv7em-none-eabihf", *rest]
    )


def cmd_clippy(rest: list[str]) -> int:
    return _run(["cargo", "clippy", "--workspace", "--all-targets", *rest])


def cmd_fmt_check(rest: list[str]) -> int:
    return _run(["cargo", "fmt", "--check", *rest])


def cmd_selftest(rest: list[str]) -> int:
    out = str(TMP / "selftest.png")
    TMP.mkdir(exist_ok=True)
    return _run(["cargo", "run", "-p", "vyr-cli", "--", "selftest-png", out, *rest])


def cmd_bench(rest: list[str]) -> int:
    # Release ALWAYS: debug timings are not baselines.
    return _run(["cargo", "run", "--release", "-p", "vyr-bench", "--", "check", *rest])


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
    return 0


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


def cmd_perf_history(rest: list[str]) -> int:
    return _run(["python3", "scripts/perf-history.py", *rest])


def cmd_gate(_rest: list[str]) -> int:
    for step in (cmd_fmt_check, cmd_clippy, cmd_test, cmd_check_mcu):
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
    quick = "--quick" in rest
    anim_args = (
        ["--frames", "60", "--no-video"] if quick else ["--arm"]
    )
    steps: list[tuple[str, object, list[str]]] = [
        ("gate", cmd_gate, []),
        ("bench", cmd_bench, []),
        ("size-mcu", cmd_size_mcu, []),
        ("anim", cmd_anim, anim_args),
        ("ladder", cmd_ladder, []),
        ("perf-history", cmd_perf_history, []),
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
    "clippy": cmd_clippy,
    "fmt-check": cmd_fmt_check,
    "selftest": cmd_selftest,
    "bench": cmd_bench,
    "bench-record": cmd_bench_record,
    "size-mcu": cmd_size_mcu,
    "anim": cmd_anim,
    "ladder": cmd_ladder,
    "perf-history": cmd_perf_history,
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

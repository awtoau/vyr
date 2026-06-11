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


def cmd_gate(_rest: list[str]) -> int:
    for step in (cmd_fmt_check, cmd_clippy, cmd_test, cmd_check_mcu):
        rc = step([])
        if rc != 0:
            _log(f"gate FAILED at {step.__name__}")
            return rc
    _log("gate ok")
    return 0


HANDLERS = {
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

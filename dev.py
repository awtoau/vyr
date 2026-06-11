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

#!/usr/bin/env python3
"""Build QEMU out of tree with TCG plugin support (arm-softmmu only).

Why: Fedora's stock qemu-system-arm is built WITHOUT --enable-plugins, so
`-plugin help` fails and no libinsn.so exists. Without a plugin, the M4
benchmark can only read semihosting SYS_CLOCK, which on this build tracks
HOST WALL TIME (proven: 39 cs idle vs 58 cs under host load for an identical
workload). An exact guest instruction count needs the insn plugin.

Source tree is the read-only mirror at SRC (never modified — build is fully
out of tree into BUILD).

Output: ./tmp/qemu-plugins-build.log
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
LOG = TMP / "qemu-plugins-build.log"

SRC = Path("/mnt/2tb/git_mirror/qemu")
BUILD = Path("/mnt/2tb/git_debris/qemu-plugins-build")

CONFIGURE_ARGS = [
    "--target-list=arm-softmmu",
    "--enable-plugins",
    "--enable-debug-info",
    "--disable-docs",
    "--disable-werror",
    "--disable-gtk",
    "--disable-sdl",
    "--disable-vnc",
    "--disable-spice",
    "--disable-opengl",
    "--disable-virglrenderer",
    "--disable-libssh",
    "--disable-curl",
    "--disable-guest-agent",
    "--disable-tools",
    # Capstone is REQUIRED, not optional: qemu has no in-tree ARM
    # disassembler (disas/ has no arm.c), so without capstone
    # qemu_plugin_insn_disas() returns nothing and libinsn's `match=`
    # windowing silently matches zero instructions. We window the timed
    # frame loop on the semihosting `bkpt 0xab`, so we need real disas.
    "--enable-capstone",
    "--disable-slirp",
    "--disable-vde",
    "--disable-brlapi",
    "--disable-curses",
    "--disable-libudev",
    "--disable-bzip2",
    "--disable-lzo",
    "--disable-snappy",
    "--disable-vhost-net",
    "--disable-vhost-user",
]


def log(msg: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    with LOG.open("a") as fh:
        fh.write(line + "\n")


def run(args: list[str], cwd: Path) -> int:
    log(f"$ (cd {cwd} && {' '.join(args)})")
    env = dict(os.environ)
    env["PATH"] = "/home/dan/.local/bin:" + env.get("PATH", "")
    proc = subprocess.Popen(
        args, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    assert proc.stdout is not None
    with LOG.open("a") as fh:
        for line in proc.stdout:
            fh.write(line)
            sys.stdout.write(line)
            sys.stdout.flush()
    return proc.wait()


def main() -> int:
    TMP.mkdir(exist_ok=True)
    BUILD.mkdir(parents=True, exist_ok=True)
    if not (SRC / "configure").exists():
        log(f"ERROR: no qemu source at {SRC}")
        return 1

    head = subprocess.run(
        ["git", "-C", str(SRC), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    log(f"qemu source {SRC} @ {head}")

    if not (BUILD / "build.ninja").exists():
        rc = run([str(SRC / "configure"), *CONFIGURE_ARGS], BUILD)
        if rc != 0:
            log(f"ERROR: configure failed rc={rc}")
            return rc
    else:
        log("build.ninja present — skipping configure")

    t0 = time.monotonic()
    rc = run(["/home/dan/.local/bin/ninja"], BUILD)
    log(f"ninja rc={rc} in {time.monotonic() - t0:.0f}s")
    if rc != 0:
        return rc

    qemu = BUILD / "qemu-system-arm"
    # NOTE: upstream moved the instruction-counting plugin OUT of
    # contrib/plugins into tests/tcg/plugins (contrib/ now has bbv, cache,
    # execlog, ips, ... but no insn.c). Both dirs are built by --enable-plugins.
    insn = BUILD / "tests" / "tcg" / "plugins" / "libinsn.so"
    log(f"qemu-system-arm exists={qemu.exists()} {qemu}")
    log(f"libinsn.so     exists={insn.exists()} {insn}")
    return 0 if (qemu.exists() and insn.exists()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

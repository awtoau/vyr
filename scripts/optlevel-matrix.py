#!/usr/bin/env python3
"""optlevel-matrix.py — #33: what does `release-mcu`'s `opt-level` actually buy
and cost, at THIS commit?

`release-mcu` ships `opt-level="z"`. That is `-Oz` — size at all costs: no
inlining, machine outliner on. The LVGL comparison harness compiles `-Os`,
whose Rust analogue is `opt-level="s"`. Publishing a `z` number against a `-Os`
number is not a like-for-like comparison, and the fix is a decision about the
FLASH BUDGET, not a tuning pass. So this measures all three axes of that
decision in one run, at one commit, with one tool each:

  1. **insns/frame** — `scripts/tier-insns.py --opt <level>` (plugin QEMU +
     libinsn, exact architectural counts; see docs/performance.md §5). One
     invocation per (opt, tier) so the measured ELF can be sized before the
     next build overwrites it.
  2. **flash** — the size matrix's own method: `arm-none-eabi-size` Berkeley
     `text + data` over the three shipped configs (code-only / font /
     font,image) at `release-mcu`, exactly as `./dev.py size-mcu` does.
  3. **M4 heap peak, stack high-water and the frame hash** — from the guest's
     own console. Heap peak and hash come free with (1); the stack figure
     needs `--features stack-probe` (#33), which paints the dead CCM stack
     region at boot and scans it after the workload — a SEPARATE build, since
     the probe itself nudges code layout.

Nothing here mutates Cargo.toml: the level is applied per-build with
`--config profile.release-mcu.opt-level=...`, so the profile default stays
whatever the repo ships and every published number remains reproducible.

The frame hash is the correctness gate: optimisation level must not change a
pixel. Any hash that moves is a serious finding, reported as FAIL.

Output: tmp/optlevel-matrix.json + tmp/optlevel-matrix.md
Log:    tmp/optlevel-matrix.log
Usage:  python3 scripts/optlevel-matrix.py [--opts z,s,2,3]
                                           [--tiers exact,fast,draft]
                                           [--repeat 1]
                                           [--phases insns,flash,stack]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
LOG = TMP / "optlevel-matrix.log"
TARGET = "thumbv7em-none-eabihf"
ELF = REPO / "target" / TARGET / "release-mcu" / "vyr-size"
PIXELS = 480 * 270

# The hashes every tier must reproduce at EVERY opt-level (vyr-size/
# m4-baseline.json for Exact/Draft; Fast is byte-identical to Exact on this
# fixture — docs/performance.md §3's fidelity caveat).
EXPECT_HASH = {
    "exact": "0x24dcaff531c6eb01",
    "fast": "0x930d03610b07ea6f",
    "draft": "0xf98cbbdddd6da1ba",
}

FEATURES = {"exact": "run-qemu", "fast": "run-qemu,fast", "draft": "run-qemu,draft"}

# Same three configs, same profile, same tool as dev.py's SIZE_MATRIX — the
# published flash numbers must be comparable cell for cell.
SIZE_CONFIGS = [
    ("code-only", ["--no-default-features"]),
    ("font", ["--no-default-features", "--features", "font"]),
    ("font,image", ["--no-default-features", "--features", "font,image"]),
]
F427_FLASH = 2 * 1024 * 1024

# A wedged guest, not a slow one: the plugin-less Exact run is ~1.4e9 guest
# insns and finishes in tens of seconds; 600 s means it is stuck.
GUEST_DEADLINE_S = 600

_lines: list[str] = []


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line, flush=True)
    _lines.append(line)
    TMP.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(_lines) + "\n")


def toml_opt(level: str) -> str:
    """`--config` takes raw TOML: z/s are strings, 0..3 are integers."""
    lvl = level.strip().strip('"')
    return f'"{lvl}"' if lvl in ("z", "s") else lvl


def cfg_args(level: str) -> list[str]:
    return ["--config", f"profile.release-mcu.opt-level={toml_opt(level)}"]


def build(features: str, level: str, extra: list[str] | None = None) -> None:
    cmd = [
        "cargo", "build", "--profile", "release-mcu", "-p", "vyr-size",
        "--target", TARGET, "--no-default-features", "--features", features,
        *cfg_args(level), *(extra or []),
    ]
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                       env={**os.environ, "CARGO_INCREMENTAL": "0"})
    if r.returncode != 0:
        log("BUILD FAILED: " + " ".join(cmd) + "\n" + (r.stdout + r.stderr)[-3000:])
        raise SystemExit(1)


def elf_sections(elf: Path) -> dict[str, int]:
    """.text / .rodata sizes straight from the section headers."""
    r = subprocess.run(["readelf", "-SW", str(elf)], capture_output=True, text=True)
    out: dict[str, int] = {}
    for line in r.stdout.splitlines():
        m = re.search(r"\.(text|rodata|data|bss)\s+(?:PROGBITS|NOBITS)\s+\S+\s+\S+\s+([0-9a-f]+)",
                      line)
        if m:
            out[m.group(1)] = out.get(m.group(1), 0) + int(m.group(2), 16)
    out["text_rodata"] = out.get("text", 0) + out.get("rodata", 0)
    return out


def run_guest(elf: Path) -> str:
    """Boot the ELF on stock qemu-system-arm, return the semihosting console."""
    args = [
        "qemu-system-arm", "-machine", "netduinoplus2", "-nographic",
        "-semihosting-config", "enable=on,target=native",
        "-icount", "shift=0,sleep=off", "-kernel", str(elf),
    ]
    g = subprocess.run(args, cwd=REPO, capture_output=True, text=True,
                       timeout=GUEST_DEADLINE_S)
    out = g.stdout + g.stderr
    if g.returncode != 0:
        log(f"GUEST FAILED rc={g.returncode}: {out[-2000:]}")
        raise SystemExit(1)
    return out


def console_facts(console: str | list[str]) -> dict:
    text = "\n".join(console) if isinstance(console, list) else console
    def one(pat: str, cast=int):
        m = re.search(pat, text)
        return cast(m.group(1)) if m else None
    return {
        "frame_fnv1a": one(r"frame fnv1a=(0x[0-9a-f]{16})", str),
        "heap_peak_b": one(r"workload ok: heap peak=(\d+) B"),
        "heap_live_end_b": one(r"heap peak=\d+ B live-end=(\d+) B"),
        "stack_watermark_b": one(r"stack watermark=(\d+) B"),
        "stack_painted_b": one(r"stack watermark=\d+ B of (\d+) B painted"),
        "stack_saturated": "SATURATED" in text,
    }


# --- phase 1: insns/frame (+ hash, heap) through the canonical tool ---------


def phase_insns(opts: list[str], tiers: list[str], repeat: int, out: dict) -> None:
    for level in opts:
        for tier in tiers:
            key = f"{tier}@{level}"
            log(f"=== insns {key} ===")
            t0 = time.monotonic()
            r = subprocess.run(
                [sys.executable, "scripts/tier-insns.py", "--opt", level,
                 "--tiers", tier, "--repeat", str(repeat)],
                cwd=REPO, capture_output=True, text=True,
            )
            if r.returncode != 0:
                log(f"tier-insns FAILED for {key}:\n{(r.stdout + r.stderr)[-3000:]}")
                raise SystemExit(1)
            data = json.loads((TMP / f"qemu-insn-tier-{tier}-O{level}.json").read_text())
            facts = console_facts(data["runs"][0]["guest_console"])
            row = {
                "insns_per_frame": data["insns_per_frame"],
                "insn_per_px": round(data["insns_per_frame"] / PIXELS, 1),
                "elf_sha256": data["elf_sha256"],
                "run_qemu_elf": elf_sections(ELF),
                "wall_s": round(time.monotonic() - t0, 1),
                **facts,
            }
            row["hash_ok"] = row["frame_fnv1a"] == EXPECT_HASH[tier]
            out["insns"][key] = row
            log(f"  {key}: {row['insns_per_frame']:,} insns/frame "
                f"({row['insn_per_px']} insn/px)  heap peak {row['heap_peak_b']:,} B  "
                f"hash {row['frame_fnv1a']} "
                f"{'OK' if row['hash_ok'] else 'MISMATCH !!!'}  "
                f".text+.rodata {row['run_qemu_elf']['text_rodata']:,} B")


# --- phase 2: flash, by the size matrix's own method ------------------------


def phase_flash(opts: list[str], out: dict) -> None:
    size_tool = shutil.which("arm-none-eabi-size")
    if size_tool is None:
        log("ERROR: arm-none-eabi-size not on PATH — skipping flash phase")
        return
    for level in opts:
        for label, flags in SIZE_CONFIGS:
            cmd = ["cargo", "build", "-p", "vyr-size", "--target", TARGET,
                   "--profile", "release-mcu", *flags, *cfg_args(level)]
            r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
            if r.returncode != 0:
                log(f"BUILD FAILED {label}@{level}:\n{(r.stdout + r.stderr)[-2000:]}")
                raise SystemExit(1)
            s = subprocess.run([size_tool, str(ELF)], capture_output=True, text=True)
            text, data, bss = (int(v) for v in s.stdout.splitlines()[1].split()[:3])
            flash = text + data
            out["flash"][f"{label}@{level}"] = {
                "text": text, "data": data, "bss": bss, "flash_b": flash,
                "flash_kib": round(flash / 1024, 1),
                "pct_2mib": round(100 * flash / F427_FLASH, 1),
            }
            log(f"  FLASH {label:<11}@{level}: text={text:,} data={data} bss={bss:,} "
                f"flash={flash:,} B ({flash / 1024:.1f} KiB, "
                f"{100 * flash / F427_FLASH:.1f}% of 2 MiB)")


# --- phase 3: stack high-water (separate, instrumented build) --------------


def phase_stack(opts: list[str], tiers: list[str], out: dict) -> None:
    for level in opts:
        for tier in tiers:
            key = f"{tier}@{level}"
            build(FEATURES[tier] + ",stack-probe", level)
            facts = console_facts(run_guest(ELF))
            facts["hash_ok"] = facts["frame_fnv1a"] == EXPECT_HASH[tier]
            facts["elf"] = elf_sections(ELF)
            out["stack"][key] = facts
            log(f"  STACK {key:<14}: watermark {facts['stack_watermark_b']:,} B "
                f"of {facts['stack_painted_b']:,} B painted"
                f"{' SATURATED' if facts['stack_saturated'] else ''}  "
                f"heap peak {facts['heap_peak_b']:,} B  hash {facts['frame_fnv1a']} "
                f"{'OK' if facts['hash_ok'] else 'MISMATCH !!!'}")


# --- report ----------------------------------------------------------------


def report(opts: list[str], tiers: list[str], out: dict) -> list[str]:
    md: list[str] = []
    ins, fl, st = out["insns"], out["flash"], out["stack"]

    def cell(d: dict, key: str, field: str, fmt=lambda v: f"{v:,}"):
        v = d.get(key, {}).get(field)
        return fmt(v) if v is not None else "—"

    if ins:
        base = opts[0]
        md.append(f"| tier | " + " | ".join(f"`{o}`" for o in opts) + " |")
        md.append("|---|" + "--:|" * len(opts))
        for tier in tiers:
            cells = []
            for o in opts:
                v = ins.get(f"{tier}@{o}", {}).get("insns_per_frame")
                b = ins.get(f"{tier}@{base}", {}).get("insns_per_frame")
                if v is None:
                    cells.append("—")
                elif o == base or not b:
                    cells.append(f"{v:,}")
                else:
                    cells.append(f"{v:,} ({100 * (v - b) / b:+.0f} %)")
            md.append(f"| {tier.capitalize()} | " + " | ".join(cells) + " |")
        md.append("")
        md.append("| tier | " + " | ".join(f"heap `{o}`" for o in opts) + " |")
        md.append("|---|" + "--:|" * len(opts))
        for tier in tiers:
            md.append(f"| {tier.capitalize()} | " + " | ".join(
                cell(ins, f"{tier}@{o}", "heap_peak_b") for o in opts) + " |")
        md.append("")
    if fl:
        md.append("| config | " + " | ".join(f"`{o}`" for o in opts) + " |")
        md.append("|---|" + "--:|" * len(opts))
        for label, _ in SIZE_CONFIGS:
            md.append(f"| {label} | " + " | ".join(
                cell(fl, f"{label}@{o}", "flash_b",
                     lambda v: f"{v:,} B ({v / 1024:.1f} KiB)") for o in opts) + " |")
        md.append("")
    if st:
        md.append("| tier | " + " | ".join(f"stack `{o}`" for o in opts) + " |")
        md.append("|---|" + "--:|" * len(opts))
        for tier in tiers:
            md.append(f"| {tier.capitalize()} | " + " | ".join(
                cell(st, f"{tier}@{o}", "stack_watermark_b") for o in opts) + " |")
        md.append("")

    bad = [k for k, v in list(ins.items()) + list(st.items()) if v.get("hash_ok") is False]
    md.append("**HASH GATE: " + ("ALL OK" if not bad else "FAILED for " + ", ".join(bad)) + "**")
    return md


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--opts", default="z,s,2,3")
    ap.add_argument("--tiers", default="exact,fast,draft")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--phases", default="insns,flash,stack")
    a = ap.parse_args()

    opts = [o.strip() for o in a.opts.split(",") if o.strip()]
    tiers = [t.strip() for t in a.tiers.split(",") if t.strip()]
    phases = {p.strip() for p in a.phases.split(",")}
    out: dict = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO,
                                 capture_output=True, text=True).stdout.strip(),
        "opts": opts, "tiers": tiers,
        "expected_hashes": EXPECT_HASH,
        "insns": {}, "flash": {}, "stack": {},
    }
    log(f"#33 opt-level matrix at {out['commit'][:12]} — opts {opts} tiers {tiers}")
    try:
        if "insns" in phases:
            phase_insns(opts, tiers, a.repeat, out)
        if "flash" in phases:
            phase_flash(opts, out)
        if "stack" in phases:
            phase_stack(opts, tiers, out)
    finally:
        (TMP / "optlevel-matrix.json").write_text(json.dumps(out, indent=2) + "\n")

    md = report(opts, tiers, out)
    (TMP / "optlevel-matrix.md").write_text("\n".join(md) + "\n")
    for line in md:
        log(line)
    log(f"wrote {TMP / 'optlevel-matrix.json'} and {TMP / 'optlevel-matrix.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

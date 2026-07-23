#!/usr/bin/env python3
"""m4-attribute.py — WHERE do the M4 instructions go? Per-symbol attribution
for every vyr tier and for LVGL, on the same emulated Cortex-M4.

Two questions, one vehicle (plugin QEMU, `netduinoplus2`, exact architectural
counts — never SYS_CLOCK, see docs/performance.md §5):

 1. **Attribution.** `contrib/plugins/libhotblocks.so` with `limit=0` reports
    every translation block as `pc, tcount, icount, ecount`; block PC ->
    symbol via the ELF symbol table gives an EXACT per-function instruction
    count for the whole run. The run is 21 rendered frames (1 + 20 timed) plus
    boot, so >99% of it is render for every tier.

    vyr's `release-mcu` profile strips symbols, so the profiled build is made
    with `--config profile.release-mcu.strip=false`; the run is then
    cross-checked against `scripts/qemu-insn.py`'s published insns/frame for
    that tier — if the count matches to the instruction, the code is the same
    code and the symbols are trustworthy.

 2. **Is `opt-level="z"` part of the gap?** The LVGL harness compiles `-Os`.
    Rust's `"z"` is `-Oz`, NOT `-Os`: it also gives up inlining, which is much
    more expensive for tiny-skia's generic/SIMD-shim code than for LVGL's flat
    C. `--sweep` rebuilds each tier at opt-level z / s / 3 and reports
    insns/frame and .text size for each, so the flag's contribution to the
    published ratio is a measured number rather than an assumption.

Output: tmp/m4-attribute.json + tmp/m4-attribute.log
Usage:  python3 scripts/m4-attribute.py [--tiers exact,fast,draft]
                                        [--sweep] [--no-profile]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
WORK = TMP / "m4-attribute"
LOG = TMP / "m4-attribute.log"
QEMU_BUILD = Path(os.environ.get("VYR_QEMU_BUILD", "/mnt/2tb/git_debris/qemu-plugins-build"))
QEMU = QEMU_BUILD / "qemu-system-arm"
HOTBLOCKS = QEMU_BUILD / "contrib" / "plugins" / "libhotblocks.so"
INSN = QEMU_BUILD / "tests" / "tcg" / "plugins" / "libinsn.so"
ELF = REPO / "target" / "thumbv7em-none-eabihf" / "release-mcu" / "vyr-size"
LVGL_ELF = TMP / "lvgl-m4.elf"
MACHINE = "netduinoplus2"
PIXELS = 480 * 270
# A wedged guest, not a slow one: the heaviest run (Exact, ~1.35e9 guest insns)
# takes minutes under a per-TB plugin callback; 40 minutes means it is stuck.
DEADLINE_S = 2400

FEATURES = {"exact": "run-qemu", "fast": "run-qemu,fast", "draft": "run-qemu,draft"}

_lines: list[str] = []


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line, flush=True)
    _lines.append(line)
    WORK.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(_lines) + "\n")


def build(tier: str, opt: str | None, strip: bool) -> Path:
    cfg = [f"--config", f"profile.release-mcu.strip={'true' if strip else 'false'}"]
    if opt:
        cfg += ["--config", f'profile.release-mcu.opt-level={opt}']
    cmd = ["cargo", "build", "--profile", "release-mcu", "-p", "vyr-size",
           "--target", "thumbv7em-none-eabihf", "--no-default-features",
           "--features", FEATURES[tier]] + cfg
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                       env={**os.environ, "CARGO_INCREMENTAL": "0"})
    if r.returncode != 0:
        log("BUILD FAILED: " + " ".join(cmd) + "\n" + r.stdout[-3000:] + r.stderr[-3000:])
        raise SystemExit(1)
    dest = WORK / f"vyr-{tier}{'' if opt is None else '-O' + opt.strip(chr(34))}" \
                  f"{'-stripped' if strip else ''}.elf"
    dest.write_bytes(ELF.read_bytes())
    return dest


def run_qemu(elf: Path, plugin: str, tag: str) -> tuple[str, str]:
    plog = WORK / f"plugin-{tag}.log"
    plog.unlink(missing_ok=True)
    args = [str(QEMU), "-machine", MACHINE, "-nographic",
            "-semihosting-config", "enable=on,target=native",
            "-icount", "shift=0,sleep=off",
            "-plugin", plugin, "-d", "plugin", "-D", str(plog),
            "-kernel", str(elf)]
    t0 = time.monotonic()
    g = subprocess.run(args, capture_output=True, text=True, cwd=REPO,
                       timeout=DEADLINE_S)
    if g.returncode != 0:
        log(f"GUEST FAILED rc={g.returncode}: {(g.stdout + g.stderr)[-2000:]}")
        raise SystemExit(1)
    log(f"  qemu {tag}: {time.monotonic() - t0:.0f}s wall")
    return plog.read_text(), g.stdout + g.stderr


def insns_per_frame(elf: Path, tag: str) -> int | None:
    text, gout = run_qemu(elf, f"{INSN},match=bkpt,trace=on", f"insn-{tag}")
    hits = re.findall(r"Δ\+(\d+) since last match", text)
    if not hits:
        return None
    window = max(int(h) for h in hits)
    m = re.search(r"timed: (\d+) warmed frames", gout)
    frames = int(m.group(1)) if m else None
    return window // frames if frames else None


def symbols(elf: Path) -> list[tuple[int, int, str]]:
    """(addr, size, name) FUNC symbols, sorted by address. Thumb bit cleared."""
    r = subprocess.run(["readelf", "-sW", str(elf)], capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        p = line.split()
        if len(p) >= 8 and p[3] == "FUNC":
            try:
                addr = int(p[1], 16) & ~1
                size = int(p[2], 0)
            except ValueError:
                continue
            out.append((addr, size, p[7]))
    out.sort()
    return out


def demangle(names: list[str]) -> dict[str, str]:
    r = subprocess.run(["c++filt"], input="\n".join(names), capture_output=True, text=True)
    dem = r.stdout.splitlines()
    return dict(zip(names, dem)) if len(dem) == len(names) else {n: n for n in names}


HASH_RE = re.compile(r"::h[0-9a-f]{16}$")


def bucket(name: str) -> str:
    n = name
    if n.startswith("lv_draw_sw_blend") or "blend_to_" in n:
        return "lvgl:blend (specialised blitters)"
    if n.startswith("lv_draw_sw_mask"):
        return "lvgl:mask (edge coverage)"
    if n.startswith("lv_draw_sw_letter") or n.startswith("lv_font") or "_glyph" in n:
        return "lvgl:text"
    if n.startswith("lv_draw_sw_img") or n.startswith("lv_draw_image"):
        return "lvgl:image"
    if n.startswith("lv_draw") or n.startswith("lv_obj") or n.startswith("lv_"):
        return "lvgl:other"
    if "tiny_skia" in n:
        return "tiny-skia"
    if "painter" in n:
        return "vyr:painter"
    if "vyr_core..ir" in n or "vyr_core::ir" in n:
        return "vyr:ir/paint-walk"
    if "vyr_core..text" in n or "vyr_core::text" in n or "skrifa" in n or "read_fonts" in n:
        return "vyr:text/skrifa"
    if "vyr_size" in n or "workload" in n:
        return "vyr:harness"
    if "serde" in n or "json" in n:
        return "vyr:serde-json"
    if "linked_list_allocator" in n or "alloc.." in n or "__rust_alloc" in n:
        return "alloc"
    if n.startswith("__aeabi") or n.startswith("mem") or "compiler_builtins" in n or n.startswith("__"):
        return "compiler-builtins/libm"
    if "libm" in n:
        return "compiler-builtins/libm"
    if "core.." in n or n.startswith("core::"):
        return "core"
    return "other"


def attribute(elf: Path, tag: str, per_frame: int | None, frames: int) -> dict:
    text, _ = run_qemu(elf, f"{HOTBLOCKS},inline=true,limit=0", f"hb-{tag}")
    rows = re.findall(r"^0x([0-9a-f]{16}), (\d+), (\d+), (\d+)$", text, re.M)
    if not rows:
        log(f"  ERROR: hotblocks produced no rows for {tag}")
        return {}
    syms = symbols(elf)
    addrs = [s[0] for s in syms]
    import bisect
    per_sym: dict[str, int] = {}
    total = 0
    unmapped = 0
    for pc_s, _tc, ic, ec in rows:
        pc = int(pc_s, 16) & ~1
        n = int(ic) * int(ec)
        total += n
        i = bisect.bisect_right(addrs, pc) - 1
        if i >= 0 and (syms[i][1] == 0 or pc < syms[i][0] + syms[i][1]):
            per_sym[syms[i][2]] = per_sym.get(syms[i][2], 0) + n
        else:
            unmapped += n
    names = list(per_sym)
    dem = demangle(names)
    by_bucket: dict[str, int] = {}
    pretty: dict[str, int] = {}
    for k, v in per_sym.items():
        d = HASH_RE.sub("", dem.get(k, k))
        pretty[d] = pretty.get(d, 0) + v
        by_bucket[bucket(d)] = by_bucket.get(bucket(d), 0) + v
    if unmapped:
        by_bucket["<unmapped pc>"] = unmapped
    top = sorted(pretty.items(), key=lambda kv: -kv[1])
    log(f"  {tag}: hotblocks total {total:,} insns over ~{frames} frames "
        f"({total / frames:,.0f}/frame; libinsn window says "
        f"{per_frame:,}/frame)" if per_frame else f"  {tag}: total {total:,}")
    log(f"  {tag}: by bucket (share of whole run):")
    for b, v in sorted(by_bucket.items(), key=lambda kv: -kv[1]):
        log(f"      {100.0 * v / total:6.2f}%  {v / frames:>12,.0f} insn/frame  {b}")
    log(f"  {tag}: top symbols:")
    for name, v in top[:25]:
        log(f"      {100.0 * v / total:6.2f}%  {v / frames:>12,.0f} insn/frame  {name[:100]}")
    return {
        "total_insns_run": total,
        "frames": frames,
        "insns_per_frame_libinsn": per_frame,
        "by_bucket": by_bucket,
        "top_symbols": [{"fn": n, "insns_run": v, "per_frame": v / frames} for n, v in top[:80]],
    }


def text_size(elf: Path) -> int:
    r = subprocess.run(["readelf", "-SW", str(elf)], capture_output=True, text=True)
    tot = 0
    for line in r.stdout.splitlines():
        m = re.search(r"\.(text|rodata)\s+PROGBITS\s+\S+\s+\S+\s+([0-9a-f]+)", line)
        if m:
            tot += int(m.group(2), 16)
    return tot


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="exact,fast,draft")
    ap.add_argument("--sweep", action="store_true", help="opt-level z/s/3 sweep")
    ap.add_argument("--no-profile", action="store_true")
    a = ap.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    out: dict = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "profile": {}, "sweep": {}}

    tiers = [t.strip() for t in a.tiers.split(",") if t.strip()]

    if not a.no_profile:
        # LVGL first: its ELF already carries symbols and is the reference.
        if LVGL_ELF.is_file():
            log("=== LVGL (tmp/lvgl-m4.elf, content-corrected harness) ===")
            pf = insns_per_frame(LVGL_ELF, "lvgl")
            # The LVGL harness renders 1 + TIMED_FRAMES frames like vyr's.
            out["profile"]["lvgl"] = attribute(LVGL_ELF, "lvgl", pf, 41)
        for tier in tiers:
            log(f"=== vyr {tier} (unstripped rebuild of release-mcu) ===")
            elf = build(tier, None, strip=False)
            pf = insns_per_frame(elf, tier)
            log(f"  {tier}: libinsn says {pf:,} insns/frame" if pf else f"  {tier}: no window")
            out["profile"][tier] = attribute(elf, tier, pf, 21)
            out["profile"][tier]["elf"] = str(elf)

    if a.sweep:
        for tier in tiers:
            for opt in ['"z"', '"s"', "3"]:
                elf = build(tier, opt, strip=True)
                pf = insns_per_frame(elf, f"{tier}-{opt.strip(chr(34))}")
                ts = text_size(elf)
                key = f"{tier}@{opt.strip(chr(34))}"
                out["sweep"][key] = {"insns_per_frame": pf, "text_rodata_bytes": ts}
                log(f"  SWEEP {key:12s}: {pf:>12,} insns/frame "
                    f"({pf / PIXELS:6.1f} insn/px)  .text+.rodata {ts:,} B"
                    if pf else f"  SWEEP {key}: FAILED")

    (TMP / "m4-attribute.json").write_text(json.dumps(out, indent=2) + "\n")
    log(f"wrote {TMP / 'm4-attribute.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

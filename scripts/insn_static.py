#!/usr/bin/env python3
"""insn_static.py — the shared half of the #37 instruction-level harness.

One idea, reused by every script here: **a hotblocks log plus a static
disassembly is an exact dynamic instruction stream summary.**

`contrib/plugins/libhotblocks.so,limit=0` reports every translation block as
`pc, tcount, icount, ecount` — the block's start address, how many
instructions it contains and how many times it ran. Disassembling the ELF once
gives the mnemonic at every address. Walking `icount` instructions from each
block's `pc` and weighting by `ecount` therefore reproduces the run's
instruction mix EXACTLY — not sampled, not modelled. The reconstruction is
self-checking: it must sum to the same total the plugin reports (validated at
100.000 % on the committed Exact profile), and callers should refuse to
publish a number when it does not.

That buys the two things `docs/measurements/lvgl-gap.md` §8 lists as *not
attributable* today:

  * **memory traffic vs arithmetic, per symbol.** #37's second comment argues
    the tiny-skia bill is stage-boundary SPILL (a `u16x16` is 32 B = 8 core
    registers, so the pipeline's ~256 B of live state cannot stay in
    registers) rather than wasted lanes. Spill is loads and stores. This
    measures the load/store share of every symbol directly.
  * **caller attribution.** A `bl` to `memcpy` or to an `OUTLINED_FUNCTION_*`
    stub sits inside a block whose `ecount` is known, so the number of CALLS
    from each caller is exact, even though the callee's own cost is only
    attributable in aggregate.

Nothing here runs qemu; it consumes logs produced by `scripts/m4-attribute.py`
or `scripts/painter-probe.py`.
"""
from __future__ import annotations

import bisect
import re
import subprocess
from pathlib import Path

# The arm-none-eabi binutils that already build every MCU artifact in this
# repo. Plain `objdump` cannot be trusted to pick Thumb mode from mapping
# symbols for a bare-metal image.
OBJDUMP_CANDIDATES = [
    Path("/opt/arm-gnu-toolchain/arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi/bin/arm-none-eabi-objdump"),
    Path("/usr/bin/arm-none-eabi-objdump"),
]

BLOCK_RE = re.compile(r"^0x([0-9a-f]{16}), (\d+), (\d+), (\d+)$", re.M)
HASH_RE = re.compile(r"::h[0-9a-f]{16}$")


def objdump() -> Path:
    for p in OBJDUMP_CANDIDATES:
        if p.is_file():
            return p
    raise SystemExit("no arm-none-eabi-objdump found — install the ARM GNU toolchain")


# --- instruction classes -----------------------------------------------------
# Thumb-2 mnemonics, grouped by what they cost on an M4 with no SIMD. The
# split that matters for #37 is LOAD/STORE (spill traffic) vs ALU (the work
# the pixels actually need). `.w`/`.n`/condition suffixes are stripped first.
_LOAD = {"ldr", "ldrb", "ldrh", "ldrsb", "ldrsh", "ldrd", "ldm", "ldmia", "ldmdb", "pop"}
_STORE = {"str", "strb", "strh", "strd", "stm", "stmia", "stmdb", "push"}
_FLOAT_LOAD = {"vldr", "vldm", "vpop"}
_FLOAT_STORE = {"vstr", "vstm", "vpush"}
_BRANCH = {"b", "bx", "cbz", "cbnz", "tbb", "tbh", "it"}
_CALL = {"bl", "blx"}
_CMP = {"cmp", "cmn", "tst", "teq"}


def _base(mnemonic: str) -> str:
    """`str.w` → `str`, `beq.n` → `b`, `addseq` → `adds` → `add`."""
    m = mnemonic.split(".")[0]
    if m.startswith("v"):  # FPU ops keep their identity
        return m
    # Conditional forms: strip a trailing 2-letter condition code, but only
    # when what is left is still a known mnemonic (so `bls` → `b`, while
    # `lsls` is not mangled into `l`).
    for cond in ("eq", "ne", "cs", "cc", "mi", "pl", "vs", "vc",
                 "hi", "ls", "ge", "lt", "gt", "le", "hs", "lo"):
        if m.endswith(cond) and len(m) > len(cond):
            stem = m[: -len(cond)]
            if stem in _LOAD | _STORE | _BRANCH | _CALL | _CMP or stem.rstrip("s") in (
                "add", "sub", "mov", "and", "orr", "eor", "lsl", "lsr", "asr", "mul", "rsb", "bic",
            ):
                return stem
    return m


def classify(mnemonic: str) -> str:
    m = _base(mnemonic)
    if m in _LOAD:
        return "load"
    if m in _STORE:
        return "store"
    if m in _FLOAT_LOAD:
        return "fp-load"
    if m in _FLOAT_STORE:
        return "fp-store"
    if m in _CALL:
        return "call"
    if m in _BRANCH:
        return "branch"
    if m in _CMP:
        return "compare"
    if m.startswith("v"):
        return "fp-alu"
    if m.rstrip("s") in ("mov", "mvn", "movw", "movt"):
        return "move"
    return "alu"


MEMORY_CLASSES = ("load", "store", "fp-load", "fp-store")


class Image:
    """A disassembled ELF: address → mnemonic, plus the FUNC symbol table."""

    def __init__(self, elf: Path):
        self.elf = elf
        od = objdump()
        r = subprocess.run(
            [str(od), "-d", "--no-show-raw-insn", str(elf)],
            capture_output=True, text=True, check=True,
        )
        self.mnemonic: dict[int, str] = {}
        self.operands: dict[int, str] = {}
        for line in r.stdout.splitlines():
            m = re.match(r"^\s*([0-9a-f]+):\s+([a-z][a-z0-9.]*)\s*(.*)$", line)
            if m:
                a = int(m.group(1), 16)
                self.mnemonic[a] = m.group(2)
                self.operands[a] = m.group(3).strip()
        self.addrs = sorted(self.mnemonic)
        self.syms = self._symbols()
        self.sym_addrs = [s[0] for s in self.syms]

    def _symbols(self) -> list[tuple[int, int, str]]:
        r = subprocess.run(["readelf", "-sW", str(self.elf)],
                           capture_output=True, text=True, check=True)
        out: list[tuple[int, int, str]] = []
        for line in r.stdout.splitlines():
            p = line.split()
            if len(p) >= 8 and p[3] == "FUNC":
                try:
                    out.append((int(p[1], 16) & ~1, int(p[2], 0), p[7]))
                except ValueError:
                    continue
        out.sort()
        return out

    def symbol_at(self, pc: int) -> str:
        i = bisect.bisect_right(self.sym_addrs, pc) - 1
        if i >= 0 and (self.syms[i][1] == 0 or pc < self.syms[i][0] + self.syms[i][1]):
            return self.syms[i][2]
        return "<unmapped>"

    def walk(self, pc: int, count: int):
        """Yield (addr, mnemonic) for `count` instructions from `pc`."""
        i = bisect.bisect_left(self.addrs, pc)
        for k in range(count):
            if i + k >= len(self.addrs):
                return
            a = self.addrs[i + k]
            yield a, self.mnemonic[a]


def blocks(log: Path) -> list[tuple[int, int, int]]:
    """(pc, insns_in_block, exec_count) from a hotblocks `limit=0` log."""
    out = []
    for pc, _t, ic, ec in BLOCK_RE.findall(log.read_text()):
        out.append((int(pc, 16) & ~1, int(ic), int(ec)))
    return out


def demangle(names: list[str]) -> dict[str, str]:
    if not names:
        return {}
    r = subprocess.run(["c++filt"], input="\n".join(names),
                       capture_output=True, text=True)
    dem = r.stdout.splitlines()
    if len(dem) != len(names):
        return {n: n for n in names}
    return {n: HASH_RE.sub("", d) for n, d in zip(names, dem)}


# --- buckets -----------------------------------------------------------------
# Finer than scripts/m4-attribute.py's, because #37 turns on WHICH PART of
# tiny-skia is expensive: the wide-type shims, the pipeline stage bodies, the
# blitters, or the path/edge front end. Order matters — first match wins.
BUCKETS: list[tuple[str, tuple[str, ...]]] = [
    ("tiny-skia:wide (u16x16/f32x8 shims)", ("u16x16", "f32x8", "i32x8", "u32x8", "wide", "f32x4", "u16x8")),
    ("tiny-skia:pipeline (stage bodies)", ("pipeline", "RasterPipeline", "lowp", "highp", "stage")),
    ("tiny-skia:blitter", ("blitter", "Blitter", "blit_")),
    ("tiny-skia:path/edge", ("path", "Path", "edge", "Edge", "scan", "Scan")),
    ("tiny-skia:other", ("tiny_skia",)),
    ("vyr:painter", ("painter",)),
    ("vyr:ir/paint-walk", ("vyr_core..ir", "vyr_core::ir")),
    ("vyr:text/skrifa", ("vyr_core..text", "vyr_core::text", "skrifa", "read_fonts")),
    ("vyr:shapes/flatten", ("shapes", "Shapes")),
    ("vyr:harness", ("vyr_size", "workload", "probe")),
    ("memcpy/memset", ("memcpy", "memset", "memmove", "memclr", "__aeabi_mem")),
    ("outlined (-Oz stubs)", ("OUTLINED_FUNCTION",)),
    ("compiler-builtins/libm", ("compiler_builtins", "libm", "__aeabi", "__mul", "__add", "__div")),
    ("alloc", ("linked_list_allocator", "__rust_alloc", "alloc..")),
    ("core", ("core..", "core::")),
]


def bucket(name: str) -> str:
    for label, needles in BUCKETS:
        for n in needles:
            if n in name:
                return label
    return "other"

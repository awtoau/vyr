#!/usr/bin/env python3
"""insn-mix.py — WHAT KIND of instruction is vyr executing, and who calls whom.

#37 asks whether tiny-skia's ~21 M insns/frame on a SIMD-less M4 are
*structural to a vectorised painter* or *stage-boundary spill that a narrower
pipeline would remove*. Those two stories predict different instruction MIXES:

  * lane waste  → the extra work is arithmetic on lanes nobody needed;
  * spill       → the extra work is loads and stores moving 32-byte values
                  that have nowhere to live on an 8-register-deep core.

`scripts/m4-attribute.py` answers "which symbol", which cannot tell them
apart. This answers "which KIND of instruction, inside which symbol", exactly
(see scripts/insn_static.py for why the reconstruction is exact rather than
sampled), and additionally attributes `memcpy` and `OUTLINED_FUNCTION_*` to
their CALLERS — the two gaps `docs/measurements/lvgl-gap.md` §8 admits to.

It re-uses the hotblocks logs `m4-attribute.py` already writes (default), so
the common case costs no qemu time at all:

    python3 scripts/m4-attribute.py --tiers exact,fast,draft   # once, slow
    python3 scripts/insn-mix.py                                # fast, exact

Output: tmp/insn-mix.json + tmp/insn-mix.log (+ a markdown table in the log).
Usage:  python3 scripts/insn-mix.py [--tiers exact,fast,draft]
                                    [--work tmp/m4-attribute]
                                    [--top 30] [--callers memcpy,OUTLINED]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import insn_static as S  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
LOG = TMP / "insn-mix.log"

_lines: list[str] = []


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line, flush=True)
    _lines.append(line)
    TMP.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(_lines) + "\n")


def analyse(elf: Path, hblog: Path, top: int, call_targets: list[str]) -> dict:
    img = S.Image(elf)
    blks = S.blocks(hblog)
    if not blks:
        raise SystemExit(f"no blocks in {hblog}")

    plugin_total = sum(ic * ec for _pc, ic, ec in blks)
    reconstructed = 0
    per_sym: dict[str, dict[str, int]] = {}
    mnemonics: dict[str, int] = {}
    # callee -> caller -> call count. A `bl` executes exactly as often as the
    # block containing it, so this is an exact call census.
    calls: dict[str, dict[str, int]] = {}

    for pc, ic, ec in blks:
        sym = img.symbol_at(pc)
        row = per_sym.setdefault(sym, {})
        for addr, mn in img.walk(pc, ic):
            cls = S.classify(mn)
            row[cls] = row.get(cls, 0) + ec
            mnemonics[mn] = mnemonics.get(mn, 0) + ec
            reconstructed += ec
            if cls == "call":
                ops = img.operands.get(addr, "")
                # objdump renders `bl 8012345 <target>`; take the symbolic name.
                if "<" in ops and ">" in ops:
                    callee = ops[ops.index("<") + 1 : ops.rindex(">")]
                    if any(t in callee for t in call_targets):
                        calls.setdefault(callee, {})
                        calls[callee][sym] = calls[callee].get(sym, 0) + ec

    exact = reconstructed == plugin_total
    log(f"  reconstruction: {reconstructed:,} of {plugin_total:,} plugin insns "
        f"({100.0 * reconstructed / plugin_total:.4f} %) — "
        f"{'EXACT' if exact else 'NOT EXACT, numbers below are not publishable'}")

    names = list(per_sym)
    dem = S.demangle(names)
    by_bucket: dict[str, dict[str, int]] = {}
    pretty: dict[str, dict[str, int]] = {}
    for raw, row in per_sym.items():
        d = dem.get(raw, raw)
        for target in (pretty.setdefault(d, {}), by_bucket.setdefault(S.bucket(d), {})):
            for k, v in row.items():
                target[k] = target.get(k, 0) + v

    def total(row: dict[str, int]) -> int:
        return sum(row.values())

    def memshare(row: dict[str, int]) -> float:
        t = total(row)
        return 100.0 * sum(row.get(c, 0) for c in S.MEMORY_CLASSES) / t if t else 0.0

    log("  by bucket — share of run, and how much of it is memory traffic:")
    log(f"      {'share':>7}  {'insns':>14}  {'mem%':>6}  {'ld%':>5} {'st%':>5}  bucket")
    for b, row in sorted(by_bucket.items(), key=lambda kv: -total(kv[1])):
        t = total(row)
        log(f"      {100.0 * t / plugin_total:6.2f}%  {t:>14,}  {memshare(row):5.1f}%  "
            f"{100.0 * row.get('load', 0) / t:4.1f}% {100.0 * row.get('store', 0) / t:4.1f}%  {b}")

    log(f"  top {top} symbols by instructions:")
    for name, row in sorted(pretty.items(), key=lambda kv: -total(kv[1]))[:top]:
        t = total(row)
        log(f"      {100.0 * t / plugin_total:6.2f}%  {t:>14,}  mem {memshare(row):5.1f}%  {name[:96]}")

    if calls:
        # Only the callees that are actually part of the bill. `-Oz` emits
        # ~1,500 outlined stubs, most called a handful of times; listing them
        # all buries the finding. Cost per call is the run's mean, because a
        # `memcpy` call's own cost depends on its length — the call COUNT is
        # exact, the per-caller insn split is not, and saying so is the point.
        log("  call census (exact call counts — a `bl` runs as often as its block):")
        # Ranked by what the callee COSTS, not by how often it is called: one
        # memcpy call is worth ~50 outlined-stub calls, and #37 is a question
        # about instructions.
        def cost(callee: str) -> int:
            return total(pretty.get(dem.get(callee, callee), {}))

        ranked = sorted(calls.items(), key=lambda kv: (-cost(kv[0]), -sum(kv[1].values())))
        for callee, callers in ranked[:12]:
            tot = sum(callers.values())
            if tot < 1000:
                break
            body = pretty.get(dem.get(callee, callee), {})
            mean = f", {total(body):,} insns total, mean {total(body) / tot:,.1f}/call" if body else ""
            log(f"      {callee}: {tot:,} calls{mean}")
            for caller, n in sorted(callers.items(), key=lambda kv: -kv[1])[:6]:
                cname = dem.get(caller, caller)
                log(f"          {100.0 * n / tot:6.2f}%  {n:>12,}  from {cname[:88]}")
        dropped = sum(sum(c.values()) for _, c in ranked[12:])
        if dropped:
            log(f"      (+{len(ranked) - 12} further callees, {dropped:,} calls, not listed)")

    return {
        "elf": str(elf),
        "hotblocks_log": str(hblog),
        "plugin_total_insns": plugin_total,
        "reconstructed_insns": reconstructed,
        "reconstruction_exact": exact,
        "by_bucket": {b: row for b, row in by_bucket.items()},
        "by_bucket_memory_share_pct": {b: memshare(row) for b, row in by_bucket.items()},
        "top_symbols": [
            {"fn": n, "insns": total(r), "memory_share_pct": memshare(r), "by_class": r}
            for n, r in sorted(pretty.items(), key=lambda kv: -total(kv[1]))[:200]
        ],
        "mnemonics": dict(sorted(mnemonics.items(), key=lambda kv: -kv[1])[:200]),
        "calls": {c: {dem.get(k, k): v for k, v in callers.items()} for c, callers in calls.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="exact,fast,draft")
    ap.add_argument("--work", default="tmp/m4-attribute",
                    help="directory holding plugin-hb-<tier>.log + vyr-<tier>.elf")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--callers", default="mem,OUTLINED_FUNCTION",
                    help="substrings of callee names to run a call census for")
    a = ap.parse_args()

    work = (REPO / a.work) if not Path(a.work).is_absolute() else Path(a.work)
    targets = [t for t in a.callers.split(",") if t]
    out: dict = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tiers": {}}

    for tier in [t.strip() for t in a.tiers.split(",") if t.strip()]:
        elf = work / f"vyr-{tier}.elf"
        hblog = work / f"plugin-hb-{tier}.log"
        if not elf.is_file() or not hblog.is_file():
            log(f"=== {tier}: SKIPPED — need {elf.name} and {hblog.name} in {work} "
                f"(run scripts/m4-attribute.py first)")
            continue
        log(f"=== {tier}: {elf.name} x {hblog.name} ===")
        out["tiers"][tier] = analyse(elf, hblog, a.top, targets)

    dest = TMP / "insn-mix.json"
    dest.write_text(json.dumps(out, indent=2) + "\n")
    log(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

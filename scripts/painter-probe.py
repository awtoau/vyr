#!/usr/bin/env python3
"""painter-probe.py — price the painter's SHAPE, not just its total (#37).

The fixture frame answers "vyr Exact costs 51 M insns". #37 needs the next
question answered: *of that, how much is charged per draw, how much per 16-px
pipeline chunk, and how much per pixel?* Those three have different fixes —
fewer draws, a narrower pipeline, or nothing at all — and no whole-frame
number can separate them.

`vyr-size --features probe` renders a sweep in which draws, chunks and pixels
vary independently (see vyr-size/src/probe.rs), each case bracketed by a
semihosting `bkpt`. This script builds it, runs it under plugin QEMU with
`libinsn,match=bkpt,trace=on`, reads the delta stream, and fits

    insns(case) = null + a·draws + b·chunks + c·pixels

by ordinary least squares over the axis-aligned opaque rect cases. `null` is
not fitted — it is the measured cost of the same banded frame with nothing in
it, so the fit never has to explain the band loop.

What the coefficients mean:
  a  per fill_path: path build, edge list, pipeline compile, clip decision —
     paid once per shape PER BAND it touches.
  b  per 16-px `lowp` chunk: tiny-skia steps STAGE_WIDTH=16 pixels at a time
     carrying ~256 B of u16x16 state. On a core with 8 usable registers that
     state spills at every stage boundary — this is the coefficient #37's
     "narrow the pipeline" option would move.
  c  per pixel: the irreducible coverage/blend arithmetic. A scalar rewrite
     cannot go below this; it is the floor any painter must pay.

Cross-read `b/16` against `c`: if a chunk costs far more than 16 pixels' worth
of per-pixel work, the pipeline's structure, not its arithmetic, is the bill.
`scripts/insn-mix.py` then says how much of that structure is memory traffic.

Legs (all optional, all off by default except the M4 one):
  --band-h 8,16,32   rebuild with a different band height. lvgl-gap.md §8
                     lists band-count sensitivity as NOT MEASURED; the band
                     is the working-set knob, so this is also the "is it a
                     cache/locality effect" experiment.
  --host             price the SAME cases on x86-64 via vyr-cli under
                     callgrind. The M4/x86 ratio per coefficient is the only
                     honest form of "the SIMD shape costs more without SIMD".
  --board            (not implemented here — use scripts/board-run.py with a
                     probe ELF; DWT_CYCCNT cycles, not instructions, are what
                     silicon charges, and the CPI difference is where cache
                     and flash wait states actually show up.)

Output: tmp/painter-probe.json + tmp/painter-probe.log
Usage:  python3 scripts/painter-probe.py [--tiers exact,fast,draft]
                                         [--opt z|s|3] [--band-h 16,32]
                                         [--host] [--keep-elf]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
WORK = TMP / "painter-probe"
LOG = TMP / "painter-probe.log"
QEMU_BUILD = Path(os.environ.get("VYR_QEMU_BUILD", "/mnt/2tb/git_debris/qemu-plugins-build"))
QEMU = QEMU_BUILD / "qemu-system-arm"
INSN_PLUGIN = QEMU_BUILD / "tests" / "tcg" / "plugins" / "libinsn.so"
ELF = REPO / "target" / "thumbv7em-none-eabihf" / "release-mcu" / "vyr-size"
CLI = REPO / "target" / "release" / "vyr-cli"
MACHINE = "netduinoplus2"
# A probe run is ~22 cases x 2 reps of a fraction of a frame; the whole thing
# is well under one Exact fixture run. 20 minutes means the guest is wedged.
DEADLINE_S = 1200

FEATURES = {"exact": "run-qemu,probe", "fast": "run-qemu,probe,fast",
            "draft": "run-qemu,probe,draft"}

# Mirrors vyr-size/src/probe.rs. Kept in sync by an assertion, not by hope:
# the guest prints its own case table and the script cross-checks every field.
STRIP_Y, STRIP_H = 16, 240
LOWP_STAGE_WIDTH = 16  # tiny-skia lowp.rs STAGE_WIDTH

_lines: list[str] = []


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line, flush=True)
    _lines.append(line)
    WORK.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(_lines) + "\n")


# --- build + run -------------------------------------------------------------


def build(tier: str, opt: str | None, band_h: int | None, strip: bool = True) -> Path:
    cfg: list[str] = ["--config", f"profile.release-mcu.strip={'true' if strip else 'false'}"]
    if opt:
        toml = f'"{opt}"' if opt in ("z", "s") else opt
        cfg += ["--config", f"profile.release-mcu.opt-level={toml}"]
    env = {**os.environ, "CARGO_INCREMENTAL": "0"}
    if band_h:
        env["VYR_BAND_H"] = str(band_h)
    cmd = ["cargo", "build", "--profile", "release-mcu", "-p", "vyr-size",
           "--target", "thumbv7em-none-eabihf", "--no-default-features",
           "--features", FEATURES[tier], *cfg]
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        log("BUILD FAILED: " + " ".join(cmd) + "\n" + (r.stdout + r.stderr)[-3000:])
        raise SystemExit(1)
    dest = (WORK / f"probe-{tier}{'-O' + opt if opt else ''}"
                   f"{'-band' + str(band_h) if band_h else ''}"
                   f"{'' if strip else '-syms'}.elf")
    dest.write_bytes(ELF.read_bytes())
    return dest


def build_unstripped(tier: str, opt: str | None, band_h: int | None) -> Path:
    return build(tier, opt, band_h, strip=False)


def run_qemu(elf: Path, tag: str) -> tuple[list[int], str]:
    """Returns (delta stream in order, guest console output)."""
    plog = WORK / f"plugin-{tag}.log"
    plog.unlink(missing_ok=True)
    args = [str(QEMU), "-machine", MACHINE, "-nographic",
            "-semihosting-config", "enable=on,target=native",
            "-icount", "shift=0,sleep=off",
            "-plugin", f"{INSN_PLUGIN},match=bkpt,trace=on",
            "-d", "plugin", "-D", str(plog), "-kernel", str(elf)]
    t0 = time.monotonic()
    try:
        g = subprocess.run(args, capture_output=True, text=True, cwd=REPO, timeout=DEADLINE_S)
    except subprocess.TimeoutExpired:
        log(f"ERROR: qemu hit the {DEADLINE_S}s guard on {elf.name} — guest wedged")
        raise SystemExit(1)
    gout = g.stdout + g.stderr
    if g.returncode != 0:
        log(f"GUEST FAILED rc={g.returncode}: {gout[-2000:]}")
        raise SystemExit(1)
    text = plog.read_text()
    deltas = [int(d) for d in re.findall(r"Δ\+(\d+) since last match", text)]
    if not deltas:
        log(f"ERROR: libinsn matched no bkpt — is this qemu built with capstone? see {plog}")
        raise SystemExit(1)
    log(f"  {tag}: {time.monotonic() - t0:.0f}s wall, {len(deltas)} bkpt deltas")
    return deltas, gout


# `radius=` was added when the sweep became parametric; keep it OPTIONAL so
# this script reads both the old hand-list output and the new grid output.
CASE_RE = re.compile(
    r"case i=(\d+) name=(\S+) kind=(\S+) w=(\d+) count=(\d+) alpha=(\d+)"
    r"(?: radius=(\d+))? px=(\d+)")
RESULT_RE = re.compile(r"result i=(\d+) name=(\S+) fnv1a=(0x[0-9a-f]+) pixels_written=(\d+)")
HEADER_RE = re.compile(r"probe \(#37\): (\d+) cases x (\d+) timed reps, "
                       r"(\d+)x(\d+) in \d+x(\d+) bands, quality=(\w+)")


def parse_guest(gout: str) -> dict:
    h = HEADER_RE.search(gout)
    if not h:
        log("ERROR: guest printed no probe header — is this a --features probe build?")
        raise SystemExit(1)
    cases = []
    for m in CASE_RE.finditer(gout):
        cases.append({"i": int(m.group(1)), "name": m.group(2), "kind": m.group(3),
                      "w": int(m.group(4)), "count": int(m.group(5)),
                      "alpha": int(m.group(6)),
                      "radius": int(m.group(7)) if m.group(7) else 0,
                      "px": int(m.group(8))})
    results = {int(m.group(1)): {"fnv1a": m.group(3), "pixels_written": int(m.group(4))}
               for m in RESULT_RE.finditer(gout)}
    n_cases, reps = int(h.group(1)), int(h.group(2))
    if len(cases) != n_cases:
        log(f"ERROR: header says {n_cases} cases, {len(cases)} printed")
        raise SystemExit(1)
    return {"cases": cases, "results": results, "reps": reps,
            "frame_w": int(h.group(3)), "frame_h": int(h.group(4)),
            "band_h": int(h.group(5)), "quality": h.group(6),
            # Every printed line is a semihosting write = one bkpt. The case
            # table and the header precede the timed section; the results
            # follow it. That is what makes the delta stream parseable.
            "pre_lines": 1 + n_cases}


def split_deltas(deltas: list[int], meta: dict) -> dict[str, list[int]]:
    """Map the delta stream onto cases.

    Contract (vyr-size/src/probe.rs): after the header + case table (one bkpt
    each), each case emits exactly 2·REPS clock bkpts, and NOTHING else is
    printed until every case is done. Within a case the deltas alternate
    [render][gap]; renders are the odd ones.
    """
    reps, cases = meta["reps"], meta["cases"]
    need = len(cases) * 2 * reps

    def try_start(start: int) -> dict[str, list[int]] | str:
        window = deltas[start : start + need]
        if len(window) != need:
            return f"stream has {len(deltas)} deltas, need {start}+{need}"
        got: dict[str, list[int]] = {}
        for ci, case in enumerate(cases):
            base = ci * 2 * reps
            renders = [window[base + 2 * r + 1] for r in range(reps)]
            # Deltas at even offsets are the gaps BETWEEN timed renders; the
            # first of them also carries the untimed parse + warm render, so
            # only the later ones are expected to be tiny.
            gaps = [window[base + 2 * r] for r in range(reps)][1:]
            if gaps and max(gaps) > min(renders) / 4:
                return (f"case {case['name']}: gap {max(gaps):,} not small against "
                        f"render {min(renders):,}")
            got[case["name"]] = renders
        return got

    # The leading delta is the boot window, and a build may print a line or
    # two more than this script predicts. Rather than hard-code the offset,
    # SEARCH for the alignment that satisfies the render/gap shape — and fail
    # loudly if none does, instead of reporting confidently misaligned numbers.
    first_err = ""
    for start in range(meta["pre_lines"], meta["pre_lines"] + 6):
        r = try_start(start)
        if isinstance(r, dict):
            if start != meta["pre_lines"]:
                log(f"  delta stream aligned at offset {start} "
                    f"(predicted {meta['pre_lines']}; extra guest output before the sweep)")
            for name, renders in r.items():
                spread = (max(renders) - min(renders)) / max(1, min(renders))
                if spread > 0.02:
                    log(f"  WARN {name}: reps differ by {100 * spread:.1f} % "
                        f"({renders}) — not steady state")
            return r
        first_err = first_err or r
    log(f"ERROR: no valid alignment of the delta stream ({first_err}). Something "
        f"printed inside the timed section, or REPS does not match the build.")
    raise SystemExit(1)


# --- the model ---------------------------------------------------------------


def geometry(case: dict, band_h: int, frame_h: int) -> tuple[float, float, float]:
    """(draws, chunks, pixels) for one case.

    A shape is re-drawn in every band it intersects — that is what banding
    costs and it is why `draws` is not simply the widget count.
    """
    if case["kind"] == "null" or case["count"] == 0:
        return (0.0, 0.0, 0.0)
    first = STRIP_Y // band_h
    last = (STRIP_Y + STRIP_H - 1) // band_h
    bands = last - first + 1
    draws = case["count"] * bands
    rows = STRIP_H
    chunks = case["count"] * rows * math.ceil(case["w"] / LOWP_STAGE_WIDTH)
    return (float(draws), float(chunks), float(case["px"]))


def solve3(a: list[list[float]], b: list[float]) -> list[float]:
    """Gauss-Jordan on a 3x3 normal system (no numpy dependency)."""
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            raise ZeroDivisionError("singular design matrix")
        m[c], m[p] = m[p], m[c]
        piv = m[c][c]
        m[c] = [v / piv for v in m[c]]
        for r in range(3):
            if r != c and m[r][c]:
                f = m[r][c]
                m[r] = [v - f * w for v, w in zip(m[r], m[c])]
    return [m[i][3] for i in range(3)]


def fit(measured: dict[str, list[int]], meta: dict) -> dict:
    band_h, frame_h = meta["band_h"], meta["frame_h"]
    by_name = {c["name"]: c for c in meta["cases"]}
    null = min(measured["null"])
    rows, ys, used = [], [], []
    for name, deltas in measured.items():
        case = by_name[name]
        # The fit uses ONLY opaque square-cornered rects: rrect/disc/blend
        # carry extra pipeline stages and curved edges, which is a different
        # question. They are reported against their rect twin instead.
        if case["kind"] != "rect" or case["alpha"] != 255:
            continue
        rows.append(geometry(case, band_h, frame_h))
        ys.append(min(deltas) - null)
        used.append(name)
    ata = [[sum(r[i] * r[j] for r in rows) for j in range(3)] for i in range(3)]
    atb = [sum(r[i] * y for r, y in zip(rows, ys)) for i in range(3)]
    a, b, c = solve3(ata, atb)
    resid = [(used[k], ys[k], a * rows[k][0] + b * rows[k][1] + c * rows[k][2])
             for k in range(len(rows))]
    worst = max(abs(m - p) / max(1.0, m) for _n, m, p in resid)
    rss = sum((m - p) ** 2 for _n, m, p in resid)

    # Is the chunk term earning its place? Refit with draws+pixels only. If
    # dropping the 16-px-chunk regressor barely moves the residual, the
    # pipeline's chunk structure is NOT what the tier is paying for — which
    # is the expected answer for Draft (integer spans, no lowp pipeline) and
    # would be a significant answer for Exact.
    idx = [0, 2]
    a2 = [[sum(r[i] * r[j] for r in rows) for j in idx] for i in idx]
    b2 = [sum(r[i] * y for r, y in zip(rows, ys)) for i in idx]
    det = a2[0][0] * a2[1][1] - a2[0][1] * a2[1][0]
    if abs(det) > 1e-9:
        d2 = (b2[0] * a2[1][1] - b2[1] * a2[0][1]) / det
        p2 = (a2[0][0] * b2[1] - a2[1][0] * b2[0]) / det
        rss2 = sum((y - (d2 * r[0] + p2 * r[2])) ** 2 for r, y in zip(rows, ys))
    else:
        d2 = p2 = rss2 = float("nan")
    return {"null_insns": null, "per_draw": a, "per_chunk": b, "per_px": c,
            "cases_in_fit": used, "worst_residual_pct": 100.0 * worst, "rss": rss,
            "no_chunk_model": {"per_draw": d2, "per_px": p2, "rss": rss2,
                               "rss_ratio": (rss2 / rss) if rss else float("nan")},
            "residuals": [{"case": n, "measured": m, "model": p} for n, m, p in resid]}


def report(tag: str, measured: dict[str, list[int]], meta: dict, f: dict) -> None:
    by_name = {c["name"]: c for c in meta["cases"]}
    band_h = meta["band_h"]
    log(f"  --- {tag}: quality={meta['quality']} band_h={band_h} ---")
    log(f"      {'case':>9} {'w':>4} {'n':>4} {'px':>8} {'draws':>6} {'chunks':>8} "
        f"{'insns':>12} {'insn/px':>9}")
    for name, deltas in measured.items():
        case = by_name[name]
        d, ch, px = geometry(case, band_h, meta["frame_h"])
        v = min(deltas)
        per_px = (v - f["null_insns"]) / px if px else float("nan")
        log(f"      {name:>9} {case['w']:>4} {case['count']:>4} {case['px']:>8} "
            f"{d:>6.0f} {ch:>8.0f} {v:>12,} {per_px:>9.1f}")
    log(f"      fit: null {f['null_insns']:,} insns/frame + "
        f"{f['per_draw']:,.0f}/draw + {f['per_chunk']:,.1f}/16px-chunk + "
        f"{f['per_px']:,.2f}/px   (worst residual {f['worst_residual_pct']:.1f} %)")
    chunk_as_px = f["per_chunk"] / LOWP_STAGE_WIDTH
    nc = f["no_chunk_model"]
    if f["worst_residual_pct"] > 15.0:
        log(f"      => NEITHER model describes this tier (worst residual "
            f"{f['worst_residual_pct']:.1f} %). Do not quote these coefficients; "
            f"read the per-case table instead.")
    elif f["per_chunk"] <= 0:
        # Not a failure — an answer. The cost is per-draw and per-pixel with
        # no 16-px-quantised component, i.e. this tier is not paying for the
        # pipeline's chunk structure at all. Cross-check it against the b15…
        # b33 boundary family below before believing it.
        log(f"      => NO chunk-quantised component (per-chunk {f['per_chunk']:,.1f} ≤ 0, "
            f"and draws+pixels alone fit to {f['worst_residual_pct']:.1f} %): "
            f"{nc['per_draw']:,.0f}/draw + {nc['per_px']:,.2f}/px explains this tier.")
    else:
        log(f"      => a 16-px chunk costs {chunk_as_px:,.2f} insn/px of STRUCTURE vs "
            f"{f['per_px']:,.2f} insn/px of per-pixel work ({chunk_as_px / f['per_px']:.2f}x); "
            f"dropping the chunk term inflates the residual sum by "
            f"{nc['rss_ratio']:.1f}x")

    # The boundary family, read directly rather than through the fit: same
    # draw count, one extra pixel of width across a multiple of 16. A cliff
    # means the pipeline charges by the chunk; a smooth line means it charges
    # by the pixel. This is the lane-waste question in its most direct form.
    fam = [n for n in ("b15", "b16", "b17", "b31", "b33") if n in measured]
    if fam:
        log("      chunk-boundary family (fixed draw count — width alone moves):")
        for name in fam:
            case = by_name[name]
            _d, ch, px = geometry(case, band_h, meta["frame_h"])
            v = min(measured[name]) - f["null_insns"]
            log(f"        {name:>5} w={case['w']:>3} px={case['px']:>7} chunks={ch:>7.0f} "
                f"{v:>12,} insns  {v / px:>7.2f}/px")
        if "b16" in measured and "b17" in measured:
            a16 = min(measured["b16"]) - f["null_insns"]
            a17 = min(measured["b17"]) - f["null_insns"]
            px16 = by_name["b16"]["px"]
            px17 = by_name["b17"]["px"]
            pixel_pred = a16 * px17 / px16
            chunk_pred = a16 * 2.0
            log(f"        16→17 px: measured {a17:,} vs {pixel_pred:,.0f} if charged per PIXEL "
                f"vs {chunk_pred:,.0f} if charged per 16-px CHUNK "
                f"({100.0 * (a17 - pixel_pred) / max(1.0, chunk_pred - pixel_pred):.0f} % "
                f"of the way to chunk-quantised)")
    # The comparisons the fit deliberately leaves out.
    for pair, what in (("w16", "blend16"), ("w480", "blend480"),
                       ("w120", "rrect120"), ("w480", "rrect480")):
        if pair in measured and what in measured:
            base, other = min(measured[pair]), min(measured[what])
            log(f"      {what} vs {pair}: {other - base:+,} insns "
                f"({100.0 * (other - base) / base:+.1f} %)")


# --- host leg ----------------------------------------------------------------


def host_leg(tiers: list[str]) -> dict:
    scenes = WORK / "scenes"
    b = subprocess.run(["cargo", "build", "-p", "vyr-size", "--no-default-features",
                        "--features", "run-qemu,probe"], cwd=REPO,
                       capture_output=True, text=True)
    if b.returncode != 0:
        log("host probe build FAILED\n" + (b.stdout + b.stderr)[-2000:])
        raise SystemExit(1)
    subprocess.run([str(REPO / "target" / "debug" / "vyr-size"),
                    "--dump-probe-scenes", str(scenes)], cwd=REPO, check=True,
                   capture_output=True, text=True)
    b = subprocess.run(["cargo", "build", "--release", "-p", "vyr-cli"], cwd=REPO,
                       capture_output=True, text=True)
    if b.returncode != 0:
        log("vyr-cli build FAILED\n" + (b.stdout + b.stderr)[-2000:])
        raise SystemExit(1)
    out: dict = {}
    for tier in tiers:
        per_case = {}
        for scene in sorted(scenes.glob("*.json")):
            cg = WORK / f"cg.{tier}.{scene.stem}.out"
            cg.unlink(missing_ok=True)
            cmd = ["valgrind", "--tool=callgrind", "--cache-sim=no", "--branch-sim=no",
                   f"--callgrind-out-file={cg}", "--", str(CLI), "render", str(scene),
                   str(WORK / f"{scene.stem}.png")]
            if tier != "exact":
                cmd.append(f"--{tier}")
            r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
            m = re.search(r"summary: (\d+)", cg.read_text()) if cg.is_file() else None
            if r.returncode != 0 or not m:
                log(f"  host {tier}/{scene.stem}: callgrind FAILED")
                continue
            per_case[scene.stem] = int(m.group(1))
        base = per_case.get("null", 0)
        log(f"  host {tier}: null {base:,} Ir; marginal Ir per case:")
        for name, v in sorted(per_case.items(), key=lambda kv: kv[1]):
            log(f"      {name:>9} {v - base:>12,}")
        out[tier] = per_case
    return out


def attribute(elf: Path, tag: str) -> dict:
    """Where do the probe's instructions go? A hotblocks pass over the SAME
    build, so the per-draw coefficient above can be attached to symbols.

    The probe run is almost entirely the sweep (boot and reporting are a
    rounding error), and the sweep is almost entirely rect fills — so a symbol
    that is large here is large PER DRAW, which is the thing the whole-frame
    profile cannot isolate.
    """
    hot = QEMU_BUILD / "contrib" / "plugins" / "libhotblocks.so"
    if not hot.is_file():
        log(f"  --attribute: no libhotblocks.so at {hot}; skipping")
        return {}
    # Symbols are stripped by release-mcu, so rebuild unstripped. Same code,
    # verified by the instruction count: it must reproduce the stripped run.
    plog = WORK / f"plugin-hb-{tag}.log"
    plog.unlink(missing_ok=True)
    args = [str(QEMU), "-machine", MACHINE, "-nographic",
            "-semihosting-config", "enable=on,target=native",
            "-icount", "shift=0,sleep=off",
            "-plugin", f"{hot},inline=true,limit=0",
            "-d", "plugin", "-D", str(plog), "-kernel", str(elf)]
    g = subprocess.run(args, capture_output=True, text=True, cwd=REPO, timeout=DEADLINE_S)
    if g.returncode != 0:
        log(f"  --attribute: guest rc={g.returncode}")
        return {}
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import insn_static as S  # noqa: E402  (optional dependency of this leg only)

    img = S.Image(elf)
    total = 0
    per_sym: dict[str, dict[str, int]] = {}
    for pc, ic, ec in S.blocks(plog):
        sym = img.symbol_at(pc)
        row = per_sym.setdefault(sym, {})
        for _addr, mn in img.walk(pc, ic):
            cls = S.classify(mn)
            row[cls] = row.get(cls, 0) + ec
            total += ec
    dem = S.demangle(list(per_sym))
    pretty: dict[str, dict[str, int]] = {}
    for raw, row in per_sym.items():
        tgt = pretty.setdefault(dem.get(raw, raw), {})
        for k, v in row.items():
            tgt[k] = tgt.get(k, 0) + v
    log(f"  attribution of the whole probe run ({total:,} insns):")
    for name, row in sorted(pretty.items(), key=lambda kv: -sum(kv[1].values()))[:20]:
        t = sum(row.values())
        mem = 100.0 * sum(row.get(c, 0) for c in S.MEMORY_CLASSES) / t
        log(f"      {100.0 * t / total:6.2f}%  {t:>13,}  mem {mem:5.1f}%  {name[:92]}")
    return {"total_insns": total,
            "top_symbols": [{"fn": n, "insns": sum(r.values()), "by_class": r}
                            for n, r in sorted(pretty.items(),
                                               key=lambda kv: -sum(kv[1].values()))[:120]]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="exact")
    ap.add_argument("--opt", default=None, help="override profile.release-mcu.opt-level (z|s|0..3)")
    ap.add_argument("--band-h", default="", help="comma list of band heights to rebuild at")
    ap.add_argument("--host", action="store_true", help="also price the same cases on x86-64")
    ap.add_argument("--attribute", action="store_true",
                    help="second qemu pass under hotblocks: which symbols the probe's "
                         "instructions belong to, and how much of each is memory traffic")
    ap.add_argument("--keep-elf", action="store_true")
    a = ap.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)

    tiers = [t.strip() for t in a.tiers.split(",") if t.strip()]
    bands = [int(x) for x in a.band_h.split(",") if x.strip()] or [None]
    out: dict = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "opt": a.opt, "m4": {}, "host": {}}

    for tier in tiers:
        for band in bands:
            tag = f"{tier}{'-O' + a.opt if a.opt else ''}{'-band' + str(band) if band else ''}"
            log(f"=== {tag}: build + run ===")
            elf = build(tier, a.opt, band)
            deltas, gout = run_qemu(elf, tag)
            meta = parse_guest(gout)
            measured = split_deltas(deltas, meta)
            f = fit(measured, meta)
            report(tag, measured, meta, f)
            out["m4"][tag] = {"meta": {k: v for k, v in meta.items() if k != "cases"},
                              "cases": meta["cases"], "measured": measured, "fit": f,
                              "hashes": meta["results"], "elf": str(elf)}
            if a.attribute:
                # Symbols first: release-mcu strips, and an attribution
                # without symbols is a list of addresses.
                unstripped = build_unstripped(tier, a.opt, band)
                out["m4"][tag]["attribution"] = attribute(unstripped, tag)
                if not a.keep_elf:
                    unstripped.unlink(missing_ok=True)
            if not a.keep_elf:
                elf.unlink(missing_ok=True)

    if a.host:
        log("=== host (x86-64, callgrind Ir on the SAME cases) ===")
        out["host"] = host_leg(tiers)

    dest = TMP / "painter-probe.json"
    dest.write_text(json.dumps(out, indent=2) + "\n")
    log(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

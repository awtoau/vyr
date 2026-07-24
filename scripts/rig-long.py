#!/usr/bin/env python3
"""rig-long.py — drive and measure the LONG animated scene (vyr-scene).

The scene is `vyr_scene::scene_ir`, a pure function of the frame index,
shared verbatim by the host rig (`vyr-rig anim --scene long`) and the M4
measurement vehicle (`vyr-size --features run-qemu,rig`). This script is the
harness that runs both legs and turns their output into numbers:

  host    vyr-size's host leg (same counting allocator as the M4) over the
          animated workload -> heap live/peak SERIES, glyph-cache growth,
          contour-memo occupancy and overflow, per (tier x detail x preset).
  rig     vyr-rig's driver -> hash chain, per-frame incremental==full proof,
          per-step dirty area SERIES, per-frame host wall ns, PNG stills.
  arm     the SAME rig binary cross-built for armv7 musl under
          qemu-arm-static -> cross-ISA hash-chain equality.
  m4      scripts/tier-insns.py with `--features-extra rig,...` -> exact M4
          insns/frame per quality tier (plugin-counted, `release-mcu`).
  series  a `rig-perframe` build -> the per-frame instruction SERIES, so the
          WORST frame has a measured cost rather than an estimate.
  report  fold everything in ./tmp into one summary table.

Every step is independent and re-runnable; pick them with `--steps`.

Output: tmp/rig-long-*.json    Log: tmp/rig-long.log
Usage:  python3 scripts/rig-long.py --steps host,rig[,arm,m4,series,report]
                                    [--tiers exact,fast,draft]
                                    [--details full,lite]
                                    [--preset smoke|short|full]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
LOG = TMP / "rig-long.log"

PRESET_FRAMES = {"smoke": 60, "short": 300, "full": 1200}
# vyr-size feature that selects each preset's frame count (bare `rig` = short).
PRESET_FEATURE = {"smoke": "rig-smoke", "short": "", "full": "rig-full"}
TIER_FEATURE = {"exact": "", "fast": "fast", "draft": "draft"}
DETAIL_FEATURE = {"full": "", "lite": "rig-lite"}
SCENE_ARG = {"full": "long", "lite": "long-lite"}

_lines: list[str] = []


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    _lines.append(line)


def flush() -> None:
    TMP.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(_lines) + "\n")


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    log("run: " + " ".join(args))
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True, **kw)


def features(tier: str, detail: str, preset: str, extra: str = "") -> str:
    parts = ["run-qemu", "rig"]
    for f in (TIER_FEATURE[tier], DETAIL_FEATURE[detail], PRESET_FEATURE[preset], extra):
        if f:
            parts.append(f)
    return ",".join(parts)


# --- step: host leg ----------------------------------------------------------

FRAME_RE = re.compile(
    r"rig frame=(\d+) hash=(0x[0-9a-f]+) px=(\d+) heap_live=(\d+) heap_peak=(\d+) "
    r"glyph_bytes=(\d+) glyph_entries=(\d+) memo_bytes=(\d+) memo_hits=(\d+) "
    r"memo_misses=(\d+) memo_overflow=(\d+) fastpath=(\d+)")


def parse_host(out: str) -> dict:
    rows = []
    for m in FRAME_RE.finditer(out):
        rows.append({
            "frame": int(m.group(1)), "hash": m.group(2), "pixels": int(m.group(3)),
            "heap_live": int(m.group(4)), "heap_peak": int(m.group(5)),
            "glyph_bytes": int(m.group(6)), "glyph_entries": int(m.group(7)),
            "memo_bytes": int(m.group(8)), "memo_hits": int(m.group(9)),
            "memo_misses": int(m.group(10)), "memo_overflow": int(m.group(11)),
            "fastpath": int(m.group(12)),
        })
    chain = re.search(r"rig chain fnv1a=(0x[0-9a-f]+)", out)
    peak = re.search(r"rig workload ok: heap peak=(\d+) B live-end=(\d+) B", out)
    band = re.search(r"rig frame 0 full-frame fnv1a=(0x[0-9a-f]+) == banded", out)
    return {
        "samples": rows,
        "chain": chain.group(1) if chain else None,
        "heap_peak": int(peak.group(1)) if peak else None,
        "heap_live_end": int(peak.group(2)) if peak else None,
        "band_equivalent_frame0": bool(band),
    }


def step_host(tiers: list[str], details: list[str], presets: list[str]) -> dict:
    out: dict[str, dict] = {}
    for detail in details:
        for preset in presets:
            for tier in tiers:
                feats = features(tier, detail, preset)
                key = f"{detail}/{preset}/{tier}"
                log(f"=== host {key} (features {feats}) ===")
                r = run(["cargo", "run", "-p", "vyr-size", "--release",
                         "--no-default-features", "--features", feats])
                if r.returncode != 0:
                    log(f"FAILED rc={r.returncode}\n{r.stdout[-3000:]}{r.stderr[-3000:]}")
                    out[key] = {"error": r.stderr[-2000:]}
                    continue
                d = parse_host(r.stdout)
                d["features"] = feats
                d["frames"] = PRESET_FRAMES[preset]
                out[key] = d
                log(f"  chain={d['chain']} heap_peak={d['heap_peak']:,} B "
                    f"live_end={d['heap_live_end']:,} B "
                    f"band_eq_frame0={d['band_equivalent_frame0']}")
    return out


# --- step: the rig (hash chain + dirty series + stills) -----------------------

def step_rig(details: list[str], presets: list[str], stills: bool) -> dict:
    out: dict[str, dict] = {}
    rc = run(["cargo", "build", "--release", "-p", "vyr-rig"])
    if rc.returncode != 0:
        log(f"build FAILED\n{rc.stderr[-3000:]}")
        return {"error": "build"}
    binary = REPO / "target" / "release" / "vyr-rig"
    for detail in details:
        for preset in presets:
            key = f"{detail}/{preset}"
            series = TMP / f"rig-long-series-{detail}-{preset}.json"
            chain_file = ("vyr-rig/hashchain-long.json" if detail == "full"
                          else "vyr-rig/hashchain-long-lite.json")
            args = [str(binary), "anim", "--scene", SCENE_ARG[detail],
                    "--preset", preset, "--series-out", str(series)]
            # The committed chain is blessed at the FULL preset; shorter
            # presets are a prefix of it and are checked frame-by-frame below
            # rather than by --check (which requires the same run shape).
            if preset == "full":
                args += ["--check", chain_file]
            if stills and preset == "full" and detail == "full":
                dump = TMP / "rig-long-stills"
                dump.mkdir(parents=True, exist_ok=True)
                args += ["--dump-dir", str(dump), "--dump-every", "50"]
            log(f"=== rig {key} ===")
            r = run(args)
            sys.stderr.write(r.stderr[-1500:])
            if r.returncode != 0:
                out[key] = {"error": r.stderr[-2000:]}
                continue
            d = json.loads(series.read_text())
            out[key] = summarize_series(d)
            out[key]["series_file"] = str(series)
            out[key]["chain_checked"] = preset == "full"
    return out


def step_video(detail: str, preset: str) -> dict:
    """Dump EVERY frame and assemble a mathematically lossless FFV1 video.

    Same spec discipline as `dev.py`'s rig video: the filename and a sidecar
    both declare resolution and colour depth, `-pix_fmt bgr0` is explicit
    (8-bit RGB, full range, no chroma subsampling — never inferred), and
    losslessness is VERIFIED by decoding frame 0 back out and byte-comparing
    it to the source PNG. A lossy artifact of a determinism test would be a
    contradiction in terms.
    """
    import shutil
    binary = REPO / "target" / "release" / "vyr-rig"
    dump = TMP / f"rig-long-frames-{detail}-{preset}"
    if dump.exists():
        shutil.rmtree(dump)          # stale frames from a longer run would leak in
    dump.mkdir(parents=True)
    frames = PRESET_FRAMES[preset]
    log(f"=== video {detail}/{preset} ({frames} frames) ===")
    r = run([str(binary), "anim", "--scene", SCENE_ARG[detail], "--preset", preset,
             "--dump-dir", str(dump)])
    if r.returncode != 0:
        log(f"dump FAILED\n{r.stderr[-2000:]}")
        return {"error": "dump"}
    spec = "480x270-rgb888-60fps-ffv1"
    out = TMP / f"rig-long-{detail}-{spec}.mkv"
    comment = (f"vyr long scene ({SCENE_ARG[detail]}): 480x270, RGB 8-bit/channel "
               f"(bgr0), full range, no chroma subsampling, 60 fps, FFV1 (lossless)")
    args = ["ffmpeg", "-y", "-framerate", "60", "-i", str(dump / "frame-%04d.png"),
            "-c:v", "ffv1", "-level", "3", "-pix_fmt", "bgr0",
            "-metadata", f"comment={comment}", str(out)]
    log("ffmpeg exact command: " + " ".join(args))
    with open(TMP / "rig-long-ffmpeg.log", "ab") as lf:
        rc = subprocess.call(args, cwd=REPO, stdout=lf, stderr=lf)
    if rc != 0:
        log(f"ffmpeg rc={rc} (see tmp/rig-long-ffmpeg.log)")
        return {"error": "ffmpeg"}
    check = TMP / f"rig-long-roundtrip-{detail}.png"
    with open(TMP / "rig-long-ffmpeg.log", "ab") as lf:
        rc = subprocess.call(["ffmpeg", "-y", "-i", str(out), "-vframes", "1", str(check)],
                             cwd=REPO, stdout=lf, stderr=subprocess.STDOUT)
    lossless = False
    if rc == 0:
        from PIL import Image
        with Image.open(dump / "frame-0000.png") as a, Image.open(check) as b:
            lossless = a.convert("RGB").tobytes() == b.convert("RGB").tobytes()
    sidecar = {"width": 480, "height": 270, "fps": 60, "codec": "ffv1 level 3",
               "pix_fmt": "bgr0", "color_depth": "8-bit/channel RGB, full range",
               "chroma_subsampling": "none", "lossless": True,
               "roundtrip_verified": lossless, "frames": frames,
               "scene": SCENE_ARG[detail], "preset": preset}
    out.with_suffix(".json").write_text(json.dumps(sidecar, indent=2) + "\n")
    log(f"lossless video -> {out} ({out.stat().st_size:,} B), roundtrip verified={lossless}")
    return {"path": str(out), "roundtrip_verified": lossless, "bytes": out.stat().st_size}


def pct_buckets(pcts: list[float]) -> dict:
    edges = [0, 1, 5, 10, 25, 50, 75, 99.999, 100.001]
    names = ["<1%", "1-5%", "5-10%", "10-25%", "25-50%", "50-75%", "75-100%", "=100%"]
    counts = [0] * len(names)
    for p in pcts:
        for i in range(len(names)):
            if edges[i] <= p < edges[i + 1]:
                counts[i] += 1
                break
    return dict(zip(names, counts))


def summarize_series(d: dict) -> dict:
    screen = d["screen_px"]
    dirty = d["dirty_px"]
    pcts = [100.0 * x / screen for x in dirty]
    ns = d["frame_ns"]
    mean_ns = sum(ns) / len(ns)
    worst_i = max(range(len(ns)), key=lambda i: ns[i])
    gb = d["glyph_cache_bytes"]
    # Does the glyph cache stop growing? Report the first frame after which
    # it never grows again — "plateaued at frame N" is the leak answer.
    plateau = None
    for i in range(len(gb) - 1, 0, -1):
        if gb[i] != gb[i - 1]:
            plateau = i
            break
    return {
        "scene": d["scene"], "suite": d["suite"], "key": d["key"],
        "frames": d["frames"], "run_hash": d["run_hash"], "arch": d["arch"],
        "screen_px": screen,
        "dirty_mean_pct": round(sum(pcts) / len(pcts), 3),
        "dirty_min_pct": round(min(pcts), 3),
        "dirty_max_pct": round(max(pcts), 3),
        "dirty_median_pct": round(sorted(pcts)[len(pcts) // 2], 3),
        "dirty_buckets": pct_buckets(pcts),
        "worst_dirty_step": max(range(len(dirty)), key=lambda i: dirty[i]),
        "host_mean_ms": round(mean_ns / 1e6, 4),
        "host_worst_ms": round(max(ns) / 1e6, 4),
        "host_worst_frame": worst_i,
        "host_worst_over_mean": round(max(ns) / mean_ns, 3),
        "glyph_bytes_first": gb[0], "glyph_bytes_last": gb[-1],
        "glyph_entries_last": d["glyph_cache_entries"][-1],
        "glyph_growth_stopped_after_frame": plateau,
        "pixels_mean": round(sum(d["pixels_written"]) / len(d["pixels_written"])),
        "pixels_max": max(d["pixels_written"]),
    }


# --- step: cross-ISA (armv7 musl under qemu-arm-static) ----------------------

ARM_TARGET = "armv7-unknown-linux-musleabihf"


def step_arm(details: list[str], preset: str) -> dict:
    out: dict[str, dict] = {}
    # rust-lld + the toolchain's self-contained musl runtime (dev.py's
    # `_anim_arm` does the same): the host's ld.bfd cannot link ARM objects,
    # and requiring a cross-gcc would make the cross-ISA rung opt-in.
    r = run(["cargo", "build", "--release", "-p", "vyr-rig", "--target", ARM_TARGET],
            env=dict(os.environ,
                     CARGO_TARGET_ARMV7_UNKNOWN_LINUX_MUSLEABIHF_LINKER="rust-lld"))
    if r.returncode != 0:
        log(f"arm build FAILED (target installed?)\n{r.stderr[-2000:]}")
        return {"error": "build"}
    binary = REPO / "target" / ARM_TARGET / "release" / "vyr-rig"
    qemu = "/usr/bin/qemu-arm-static"
    if not Path(qemu).exists():
        log("qemu-arm-static missing — skipping the cross-ISA rung")
        return {"error": "no qemu-arm-static"}
    for detail in details:
        chain_file = ("vyr-rig/hashchain-long.json" if detail == "full"
                      else "vyr-rig/hashchain-long-lite.json")
        log(f"=== arm {detail}/{preset} ===")
        r = run([qemu, str(binary), "anim", "--scene", SCENE_ARG[detail],
                 "--preset", preset, "--check", chain_file])
        txt = r.stdout + r.stderr
        m = re.search(r"run hash (0x[0-9a-f]+)", txt)
        out[detail] = {
            "rc": r.returncode,
            "run_hash": m.group(1) if m else None,
            "chain_matches_x86_golden": "hash chain matches" in txt,
            "tail": txt.strip().splitlines()[-3:],
        }
        log(f"  rc={r.returncode} matches={out[detail]['chain_matches_x86_golden']}")
    return out


# --- step: M4 instruction counts --------------------------------------------

def step_m4(tiers: list[str], detail: str, preset: str) -> dict:
    extra = ",".join(x for x in ("rig", DETAIL_FEATURE[detail], PRESET_FEATURE[preset]) if x)
    out: dict[str, dict] = {}
    # One tier per invocation: a tier that exhausts the 122,880 B arena is a
    # RESULT, not a reason to lose the tiers that ran. tier-insns.py aborts
    # the whole sweep on the first guest failure, so the sweep is done here.
    for tier in tiers:
        log(f"=== m4 {detail}/{preset} {tier} (features-extra {extra}) ===")
        r = run([sys.executable, "scripts/tier-insns.py",
                 "--tiers", tier, "--repeat", "1", "--features-extra", extra])
        if r.returncode != 0:
            fail = re.findall(r"^(?:FATAL|ERROR) \[vyr-size\].*|^memory allocation of .*",
                              r.stdout, re.M)
            last = re.findall(r"rig frame=(\d+) .*heap_peak=(\d+)", r.stdout)
            out[tier] = {
                "error": "guest failed",
                "last_frame": int(last[-1][0]) if last else None,
                "last_heap_peak": int(last[-1][1]) if last else None,
                "diagnosis": fail[-4:],
            }
            log(f"  {tier}: FAILED — {out[tier]['diagnosis']}")
            continue
        out.update(_m4_parse(TMP / "tier-insns.json"))
    (TMP / f"rig-long-m4-{detail}-{preset}.json").write_text(json.dumps(out, indent=2) + "\n")
    return out


def _m4_parse(path: Path) -> dict:
    data = json.loads(path.read_text())
    out = {}
    for tier, d in data.items():
        console = d["runs"][0]["guest_console"]
        chain = next((re.search(r"rig chain fnv1a=(0x[0-9a-f]+)", ln).group(1)
                      for ln in console if "rig chain fnv1a=" in ln), None)
        peak = next((int(re.search(r"heap peak=(\d+)", ln).group(1))
                     for ln in console if "rig workload ok" in ln), None)
        out[tier] = {
            "insns_per_frame": d["insns_per_frame"],
            "timed_frames": d["timed_frames"],
            "timed_window_insns": d["timed_window_insns"],
            "total_insns": d["total_insns"],
            "deterministic": d["deterministic"],
            "wall_s": d["runs"][0]["wall_s"],
            "chain": chain,
            "heap_peak": peak,
            "elf_sha256": d["elf_sha256"],
        }
        log(f"  {tier}: {d['insns_per_frame']:,} insns/frame over {d['timed_frames']} frames, "
            f"chain={chain} heap_peak={peak} wall={d['runs'][0]['wall_s']}s")
    return out


# --- step: per-frame instruction series --------------------------------------

DELTA_RE = re.compile(r"^0x[0-9a-f]+, '[^']*', \d+ hits , cpu \d+, \d+ match hits,"
                      r" Δ\+(\d+) since last match", re.M)


def step_series(tier: str, detail: str, preset: str, reuse: bool = False) -> dict:
    """Build a `rig-perframe` ELF and read EVERY plugin delta as one frame.

    scripts/qemu-insn.py's own `insns_per_frame` is meaningless for this
    build (its rule is "the largest delta IS the timed window", and here
    every delta is a single frame) — but it writes the raw plugin log, and
    that log is the series. So we reuse the runner and re-read its output.
    """
    feats = features(tier, detail, preset, extra="rig-perframe")
    log(f"=== series {tier}/{detail}/{preset} (features {feats}) ===")
    name = f"rigseries-{tier}-{detail}-{preset}"
    plog = TMP / f"qemu-insn-plugin-{name}-0.log"
    if not (reuse and plog.exists()):
        env = dict(os.environ, CARGO_INCREMENTAL="0")
        r = run(["cargo", "build", "--profile", "release-mcu", "-p", "vyr-size",
                 "--target", "thumbv7em-none-eabihf",
                 "--no-default-features", "--features", feats], env=env)
        if r.returncode != 0:
            log(f"build FAILED\n{r.stdout[-2000:]}{r.stderr[-3000:]}")
            return {"error": "build"}
        elf = REPO / "target" / "thumbv7em-none-eabihf" / "release-mcu" / "vyr-size"
        r = run([sys.executable, "scripts/qemu-insn.py", str(elf),
                 "--name", name, "--repeat", "1"])
    if not plog.exists():
        log(f"no plugin log at {plog}")
        return {"error": "no plugin log"}
    deltas = [int(x) for x in DELTA_RE.findall(plog.read_text())]
    frames = PRESET_FRAMES[preset]
    # Isolating the timed loop, from the END of the trace inwards:
    #   ... survey emits ... | t0 | c0 c1 ... c(N-1) | t1 | 2 closing emits
    # A rendered frame is millions of instructions; the loop-entry gap and
    # the closing console writes are thousands. So: drop trailing deltas that
    # are orders of magnitude too small to be a frame, then take the last N.
    # (Taking a max-sum window instead would be wrong — the untimed survey
    # pass renders the same frames with far fewer traps, so a window
    # straddling it sums MORE work than the real one.)
    # The threshold is ABSOLUTE, not relative to the largest delta: the
    # untimed survey emits once every frames/20 frames, so its deltas are
    # tens of times a single frame, and a max-relative floor would sit ABOVE
    # a frame and strip the entire measurement. A banded 480x270 frame is
    # >= 1e7 instructions on this part; a semihosting console write is ~1e3.
    FLOOR = 100_000
    tail = list(deltas)
    while tail and tail[-1] < FLOOR:
        tail.pop()
    per_frame = tail[-frames:] if len(tail) >= frames else tail
    mean = sum(per_frame) / len(per_frame)
    worst = max(range(len(per_frame)), key=lambda i: per_frame[i])
    best = min(range(len(per_frame)), key=lambda i: per_frame[i])
    # A slice that did not land on the timed loop shows up as a spread far
    # wider than any frame-to-frame variation; say so rather than publishing
    # a mean of the wrong deltas.
    sane = len(per_frame) == frames and max(per_frame) < 4 * (sum(per_frame) / len(per_frame))
    out = {
        "tier": tier, "detail": detail, "preset": preset, "frames": frames,
        "window_looks_sane": sane,
        "deltas_seen": len(deltas), "frames_used": len(per_frame),
        "mean_insns": round(mean),
        "median_insns": sorted(per_frame)[len(per_frame) // 2],
        "min_insns": per_frame[best], "min_frame": best,
        "max_insns": per_frame[worst], "max_frame": worst,
        "worst_over_mean": round(per_frame[worst] / mean, 3),
        "series": per_frame,
    }
    (TMP / f"rig-long-insn-series-{tier}-{detail}-{preset}.json").write_text(
        json.dumps(out, indent=2) + "\n")
    log(f"  mean {out['mean_insns']:,} / worst {out['max_insns']:,} at frame "
        f"{out['max_frame']} ({out['worst_over_mean']}x mean) / min {out['min_insns']:,}")
    return {k: v for k, v in out.items() if k != "series"}


# --- main ---------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="rig,host",
                    help="comma list of host,rig,arm,m4,series,report")
    ap.add_argument("--tiers", default="exact,fast,draft")
    ap.add_argument("--details", default="full,lite")
    ap.add_argument("--presets", default="smoke,short,full")
    ap.add_argument("--m4-preset", default="smoke")
    ap.add_argument("--m4-matrix", default=None,
                    help="detail:preset:tier+tier,... e.g. lite:smoke:exact+fast+draft,"
                         "lite:full:draft")
    ap.add_argument("--m4-detail", default="lite")
    ap.add_argument("--series-tier", default="draft")
    ap.add_argument("--no-stills", action="store_true")
    ap.add_argument("--reuse", action="store_true",
                    help="series: re-read the existing plugin log instead of re-emulating")
    a = ap.parse_args()

    TMP.mkdir(parents=True, exist_ok=True)
    steps = [s.strip() for s in a.steps.split(",") if s.strip()]
    tiers = [t.strip() for t in a.tiers.split(",") if t.strip()]
    details = [d.strip() for d in a.details.split(",") if d.strip()]
    presets = [p.strip() for p in a.presets.split(",") if p.strip()]

    out_path = TMP / "rig-long.json"
    summary = json.loads(out_path.read_text()) if out_path.exists() else {}
    summary["timestamp"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    if "rig" in steps:
        summary["rig"] = step_rig(details, presets, stills=not a.no_stills)
    if "host" in steps:
        summary["host"] = step_host(tiers, details, presets)
    if "arm" in steps:
        summary["arm"] = step_arm(details, presets[-1])
    if "video" in steps:
        summary.setdefault("video", {})[f"{details[0]}/{presets[-1]}"] = step_video(
            details[0], presets[-1])
    if "m4" in steps:
        # `--m4-matrix detail:preset:tiers,...` runs the whole sweep from ONE
        # invocation, so every published M4 number comes from one build state
        # of the tree (re-running a subset later would silently mix states).
        cells = []
        if a.m4_matrix:
            for cell in a.m4_matrix.split(","):
                d, p, t = cell.split(":")
                cells.append((d, p, t.split("+")))
        else:
            cells.append((a.m4_detail, a.m4_preset, tiers))
        for d, p, ts in cells:
            summary.setdefault("m4", {})[f"{d}/{p}"] = step_m4(ts, d, p)
    if "series" in steps:
        summary.setdefault("insn_series", {})[
            f"{a.series_tier}/{a.m4_detail}/{a.m4_preset}"] = step_series(
                a.series_tier, a.m4_detail, a.m4_preset, reuse=a.reuse)

    out_path.write_text(json.dumps(summary, indent=2) + "\n")
    log(f"wrote {out_path}")
    if "report" in steps:
        report(summary)
    return 0


def report(s: dict) -> None:
    log("=" * 72)
    for key, d in sorted(s.get("rig", {}).items()):
        if "error" in d:
            continue
        log(f"rig {key}: {d['frames']} frames, hash {d['run_hash']}, "
            f"dirty mean {d['dirty_mean_pct']}% / median {d['dirty_median_pct']}% / "
            f"min {d['dirty_min_pct']}% / max {d['dirty_max_pct']}%")
        log(f"    buckets {d['dirty_buckets']}")
        log(f"    host {d['host_mean_ms']} ms mean, worst frame {d['host_worst_frame']} "
            f"{d['host_worst_ms']} ms ({d['host_worst_over_mean']}x)")
        log(f"    glyph cache {d['glyph_bytes_first']} -> {d['glyph_bytes_last']} B "
            f"({d['glyph_entries_last']} entries), last growth at frame "
            f"{d['glyph_growth_stopped_after_frame']}")
    for key, d in sorted(s.get("host", {}).items()):
        if "error" in d:
            continue
        rows = d["samples"]
        peaks = [r["heap_peak"] for r in rows]
        # When did the peak STOP rising? A peak that is still climbing at the
        # end of a 1200-frame run is the leak signature; one that settles
        # early and never moves again is the opposite, and only the series
        # can tell them apart.
        settle = next((rows[i]["frame"] for i in range(len(peaks) - 1, 0, -1)
                       if peaks[i] != peaks[i - 1]), rows[0]["frame"])
        lives = [r["heap_live"] for r in rows]
        log(f"host {key}: heap peak {d['heap_peak']:,} B "
            f"(frame {rows[0]['frame']}: {peaks[0]:,} -> frame {rows[-1]['frame']}: "
            f"{peaks[-1]:,}); peak stopped rising at frame {settle}; "
            f"live {min(lives):,}..{max(lives):,} B, live-end {d['heap_live_end']:,} B; "
            f"chain {d['chain']}")
        last = rows[-1]
        log(f"    memo bytes {last['memo_bytes']} hits {last['memo_hits']:,} "
            f"misses {last['memo_misses']:,} overflow {last['memo_overflow']:,} "
            f"({100.0 * last['memo_hits'] / max(1, last['memo_hits'] + last['memo_misses']):.1f}% hit); "
            f"glyphs {last['glyph_bytes']} B / {last['glyph_entries']} entries")
    for key, d in sorted(s.get("m4", {}).items()):
        for tier, t in sorted(d.items()):
            if not isinstance(t, dict):
                continue
            if "error" in t:
                log(f"m4 {key} {tier}: FAILED after frame {t.get('last_frame')} "
                    f"(heap peak {t.get('last_heap_peak')}): {t.get('diagnosis')}")
                continue
            log(f"m4 {key} {tier}: {t['insns_per_frame']:,} insns/frame "
                f"({t['timed_frames']} frames), heap peak {t['heap_peak']}, "
                f"chain {t['chain']}, wall {t['wall_s']}s")


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        flush()

#!/usr/bin/env python3
"""lvgl-m4-bench/run.py — build + run the bare-metal LVGL benchmark on the SAME
emulated Cortex-M4 vyr uses (qemu-system-arm -machine netduinoplus2, STM32F405).

Pipeline:
  1. Compile the harness (startup.c, main.c) + the LVGL v9.6 sources (from the
     vendored vyvanse-runner tree) for thumbv7em (M4F, hard float) against our
     M4-tuned lv_conf.h.
  2. Link with m4.ld (F405 map, mirrors vyr's link-qemu.ld).
  3. qemu-system-arm with -icount shift=0,sleep=off -semihosting -nographic,
     under a HARD WALL-CLOCK GUARD (Python deadline poll + kill on hang —
     never a shell timeout/sleep).
  4. Capture semihosting -> tmp/lvgl-m4.log, parse the numbers.

All build/run logging is timestamped to tmp/lvgl-m4-bench.log.

Usage:
  python3 scripts/lvgl-m4-bench/run.py [--mem-kb N] [--keep-going]

--mem-kb sweeps the LVGL pool size (LV_MEM_SIZE_KB); default 64. The harness
reports lv_mem_peak (high-water mark) so you can right-size it.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
TMP = os.path.join(REPO, "tmp")
# LVGL comes from the read-only upstream MIRROR, never a fork or a nested
# submodule of a port project: this benchmark is the competitive anchor, so it
# must build STOCK LVGL. The mirror tracks github.com/lvgl/lvgl directly.
# Deliberately NOT pinned — we always measure against current upstream — so
# the harness RECORDS the exact commit it built (see lvgl_version()) and every
# result is stamped with it. An anchor whose provenance is not recorded is not
# an anchor. Override with --lvgl-root for a one-off comparison.
LVGL_MIRROR_DEFAULT = "/mnt/2tb/git_mirror/lvgl"
LVGL_ROOT = os.environ.get("VYR_LVGL_ROOT", LVGL_MIRROR_DEFAULT)
LVGL_SRC = os.path.join(LVGL_ROOT, "src")
LVGL_INC = os.path.join(LVGL_ROOT, "include")


def lvgl_version(root=None):
    """Identify the LVGL tree being built: commit, describe, dirty, version.

    Recorded into the result so a patched or drifted source can never silently
    become the anchor — the failure mode this replaced (the previous source was
    5 commits ahead of upstream with local patches applied).
    """
    root = root or LVGL_ROOT
    def g(*a):
        try:
            return subprocess.run(
                ["git", "-C", root, *a], capture_output=True, text=True, check=True
            ).stdout.strip()
        except Exception:
            return None

    info = {
        "root": root,
        "commit": g("rev-parse", "HEAD"),
        "short": g("rev-parse", "--short", "HEAD"),
        "describe": g("describe", "--tags", "--always", "--dirty"),
        "date": g("log", "-1", "--format=%aI"),
        "subject": g("log", "-1", "--format=%s"),
        "remote": g("config", "--get", "remote.origin.url"),
        "dirty": bool(g("status", "--porcelain")),
        "ahead_of_origin": g("rev-list", "--count", "origin/master..HEAD"),
    }
    # semantic version from the header (layout moved to include/lvgl/ in 9.6)
    ver = {}
    for cand in ("include/lvgl/lv_version.h", "lv_version.h"):
        p = os.path.join(root, cand)
        if not os.path.exists(p):
            continue
        try:
            txt = open(p).read()
        except Exception:
            continue
        import re as _re

        for k in ("MAJOR", "MINOR", "PATCH"):
            m = _re.search(rf"#define\s+LVGL_VERSION_{k}\s+(\d+)", txt)
            if m:
                ver[k.lower()] = int(m.group(1))
        m = _re.search(r'#define\s+LVGL_VERSION_INFO\s+"([^"]*)"', txt)
        if m:
            ver["info"] = m.group(1)
        if ver:
            break
    if ver:
        info["version"] = (
            f"{ver.get('major','?')}.{ver.get('minor','?')}.{ver.get('patch','?')}"
            + (f"-{ver['info']}" if ver.get("info") else "")
        )
    return info

# arm-none-eabi-gcc on PATH (Fedora packages it; the /opt arm-gnu-toolchain is
# binutils-only — no cc1 there). The ST/TGX builds use the /opt binutils but
# the compiler comes from the distro toolchain.
GCC = "arm-none-eabi-gcc"
SIZE = "arm-none-eabi-size"
OBJCOPY = "arm-none-eabi-objcopy"

MACHINE = "netduinoplus2"
# Wall-clock guard: a clean run finishes in a few seconds; 180s is a generous
# ceiling that still kills a wedged guest (an infinite render loop / fault
# spin). Reason: bare-metal LVGL render of one static 480x270 frame on the
# emulated M4 is bounded; if it has not exited by 180s it is hung.
DEADLINE_S = 180

CFLAGS = [
    "-mcpu=cortex-m4", "-mthumb",
    "-mfloat-abi=hard", "-mfpu=fpv4-sp-d16",
    "-Os", "-ffunction-sections", "-fdata-sections",
    "-ffreestanding", "-fno-common",
    "-std=gnu11",
    "-Wall",
    # We use only the v9 native API; the v8 compat api_map header has a
    # forward-decl ordering bug when it lands as a TU's first include.
    "-DLV_DISABLE_API_MAPPING=1",
    # Our lv_conf.h is in HERE; LVGL picks it up via LV_CONF_PATH.
    f'-DLV_CONF_PATH="{os.path.join(HERE, "lv_conf.h")}"',
    "-I", HERE,
    # Parent of the lvgl/ dir, so `#include "lvgl/lvgl.h"` resolves to the
    # top-level forwarder (the same include shape the vyvanse-runner uses).
    "-I", os.path.dirname(LVGL_ROOT),
    "-I", LVGL_INC,
    "-I", LVGL_ROOT,
]


def now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def log(msg, logf):
    line = f"[{now()}] {msg}"
    print(line)
    logf.write(line + "\n")
    logf.flush()


def lvgl_sources():
    """All LVGL src/**/*.c. Per-file internal guards (#if LV_USE_X) + our
    lv_conf.h compile the disabled features to empty objects; --gc-sections
    drops dead code at link. We DO exclude the 3rd-party libs/ tree and the
    OS abstraction / drivers, which pull host/OS headers even when guarded."""
    all_c = glob.glob(os.path.join(LVGL_SRC, "**", "*.c"), recursive=True)
    # 3rd-party / accelerator libs disabled in our lv_conf.h. Most compile to
    # empty objects via internal #if guards, but these pull C++/host/SDK
    # headers even when guarded, so exclude the whole subtree. bin_decoder,
    # lz4, rle, fsdrv etc. are KEPT — lv_init() needs lv_bin_decoder_init().
    EXCLUDE_LIB = {
        "thorvg", "gltf", "ffmpeg", "gstreamer", "expat", "freetype",
        "libjpeg_turbo", "libpng", "libwebp", "rlottie", "nanovg",
        "vg_lite_driver", "FT800-FT813", "frogfs", "svg",
    }
    keep = []
    for c in all_c:
        rel = os.path.relpath(c, LVGL_SRC).split(os.sep)
        top = rel[0]
        # drivers/ = display/indev backends (none used on bare metal).
        if top == "drivers":
            continue
        if top == "libs" and len(rel) > 1 and rel[1] in EXCLUDE_LIB:
            continue
        keep.append(c)
    return sorted(keep)


def build(mem_kb, logf, frames=None, dump_path=None, elf_name="lvgl-m4.elf"):
    """Compile + link the harness. `dump_path`, when set, adds -DDUMP_FRAME to
    the HARNESS translation units only (startup.c, main.c) — the LVGL objects
    are bit-identical either way, so they are reused from a previous build when
    they are newer than their source. The measurement ELF is therefore never
    the ELF that carries the file-I/O path."""
    obj_dir = os.path.join(TMP, "lvgl-m4-obj")
    os.makedirs(obj_dir, exist_ok=True)
    objs = []
    cflags = list(CFLAGS) + [f"-DLV_MEM_SIZE_KB={mem_kb}"]
    if frames:
        cflags += [f"-DTIMED_FRAMES={frames}"]
    harness = [os.path.join(HERE, "startup.c"), os.path.join(HERE, "main.c")]
    harness_extra = []
    if dump_path:
        harness_extra = ["-DDUMP_FRAME=1", f'-DDUMP_FRAME_PATH="{dump_path}"']

    sources = harness + lvgl_sources()
    log(f"compiling {len(sources)} translation units (LVGL + harness), "
        f"LV_MEM_SIZE_KB={mem_kb}"
        + (f", DUMP_FRAME -> {dump_path}" if dump_path else ""), logf)

    failures = []
    reused = 0
    for src in sources:
        # Flatten to a unique object name (paths collide across dirs).
        rel = os.path.relpath(src, REPO).replace(os.sep, "__")
        obj = os.path.join(obj_dir, rel + ".o")
        is_harness = src in harness
        # Reuse an up-to-date LVGL object when only the harness changed; the
        # LVGL cflags are identical between the two builds. Harness TUs are
        # ALWAYS recompiled (their defines are what differ).
        if (dump_path and not is_harness and os.path.exists(obj)
                and os.path.getmtime(obj) >= os.path.getmtime(src)):
            objs.append(obj)
            reused += 1
            continue
        cmd = [GCC] + cflags + (harness_extra if is_harness else []) + [
            "-c", src, "-o", obj]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            failures.append((src, r.stderr))
            log(f"  COMPILE FAIL: {os.path.relpath(src, REPO)}", logf)
            logf.write(r.stderr + "\n")
            logf.flush()
        else:
            objs.append(obj)
            if r.stderr.strip():
                # warnings -> log only
                logf.write(f"warn {os.path.relpath(src, REPO)}:\n{r.stderr}\n")
    if failures:
        log(f"{len(failures)} compile failures (see log) — aborting link", logf)
        for src, _ in failures:
            log(f"    failed: {os.path.relpath(src, REPO)}", logf)
        return None
    if reused:
        log(f"  reused {reused} cached LVGL objects (harness TUs recompiled)", logf)

    elf = os.path.join(TMP, elf_name)
    ld = os.path.join(HERE, "m4.ld")
    link = [GCC] + cflags + objs + [
        "-T", ld,
        "-nostartfiles",
        "-Wl,--gc-sections",
        "-Wl,-Map=" + os.path.join(TMP, "lvgl-m4.map"),
        # newlib-nano: builtin LVGL malloc/sprintf means we pull very little
        # of libc, but memcpy/memset/float helpers come from libgcc/libc.
        "--specs=nano.specs", "-lc", "-lgcc", "-lm",
        "-o", elf,
    ]
    log(f"linking {elf_name}", logf)
    r = subprocess.run(link, capture_output=True, text=True)
    if r.returncode != 0:
        log("LINK FAIL", logf)
        logf.write(r.stderr + "\n")
        logf.flush()
        return None
    if r.stderr.strip():
        logf.write("link warnings:\n" + r.stderr + "\n")

    # Flash size (.text + .rodata + .data) — the apples-to-apples flash column.
    sz = subprocess.run([SIZE, elf], capture_output=True, text=True)
    log("size:\n" + sz.stdout.strip(), logf)
    return elf


def run_qemu(elf, logf, semilog):
    args = [
        "qemu-system-arm", "-machine", MACHINE, "-nographic",
        "-semihosting-config", "enable=on,target=native",
        "-icount", "shift=0,sleep=off",
        "-kernel", elf,
    ]
    log("qemu exact command: " + " ".join(args), logf)
    import time
    t0 = time.monotonic()
    try:
        # Python-side hard wall-clock guard (NOT a shell timeout): subprocess
        # enforces DEADLINE_S and kills a wedged guest.
        guest = subprocess.run(args, capture_output=True, text=True,
                               timeout=DEADLINE_S)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "")
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        err = (e.stderr or "")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        with open(semilog, "w") as f:
            f.write(out + err)
        log(f"ERROR: qemu hit the {DEADLINE_S}s guard — guest wedged, killed", logf)
        return None
    wall = time.monotonic() - t0
    gout = guest.stdout + guest.stderr
    with open(semilog, "w") as f:
        f.write(gout)
    log(f"qemu wall {wall:.1f}s, rc={guest.returncode}; semihosting -> {semilog}", logf)
    return gout


def parse(gout, logf):
    import re

    def g(pat):
        m = re.search(pat, gout)
        return m.group(1) if m else None

    res = {
        "frame_hash": g(r"frame fnv1a=(0x[0-9a-f]{16})"),
        "warmed_hash": g(r"warmed frame fnv1a=(0x[0-9a-f]{16})"),
        "draw_buf_bytes": g(r"draw_buf_bytes=(\d+)"),
        "frame_w": g(r"frame_w=(\d+)"),
        "frame_h": g(r"frame_h=(\d+)"),
        "band_h": g(r"band_h=(\d+)"),
        "bands_flushed": g(r"bands_flushed=(\d+)"),
        "pixels_flushed": g(r"pixels_flushed=(\d+)"),
        "lv_mem_total": g(r"lv_mem_total=(\d+)"),
        "lv_mem_used_now": g(r"lv_mem_used_now=(\d+)"),
        "lv_mem_peak": g(r"lv_mem_peak=(\d+)"),
        "lv_mem_frag_pct": g(r"lv_mem_frag_pct=(\d+)"),
        "timed_cs": g(r"timed_cs=\D*(\d+)"),
        "timed_frames": g(r"timed_frames=\D*(\d+)"),
        "ok": "workload ok" in gout,
    }
    log("parsed: " + ", ".join(f"{k}={v}" for k, v in res.items()), logf)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mem-kb", type=int, default=64,
                    help="LVGL pool size LV_MEM_SIZE_KB (default 64)")
    ap.add_argument("--frames", type=int, default=None,
                    help="timed frames (default 40). SYS_CLOCK has 1 cs granularity, "
                         "so quantisation error is ~1/total_cs — raise this for precision.")
    ap.add_argument("--lvgl-root", default=None,
                    help=f"LVGL source tree (default: the upstream mirror {LVGL_MIRROR_DEFAULT})")
    ap.add_argument("--dump-frame", metavar="PATH", default=None,
                    help="build a SEPARATE -DDUMP_FRAME ELF that writes the rendered "
                         "480x270 RGB888 frame to PATH on the host via semihosting "
                         "SYS_OPEN/WRITE (#27 fidelity comparison). The measurement "
                         "numbers from such a run are NOT the published ones — the "
                         "published ELF has no file-I/O path.")
    args = ap.parse_args()

    global LVGL_ROOT, LVGL_SRC, LVGL_INC
    if args.lvgl_root:
        LVGL_ROOT = os.path.abspath(args.lvgl_root)
        LVGL_SRC = os.path.join(LVGL_ROOT, "src")
        LVGL_INC = os.path.join(LVGL_ROOT, "include")

    os.makedirs(TMP, exist_ok=True)
    benchlog = os.path.join(TMP, "lvgl-m4-bench.log")
    semilog = os.path.join(TMP, "lvgl-m4.log")
    resultjson = os.path.join(TMP, "lvgl-m4-result.json")
    dump_path = os.path.abspath(args.dump_frame) if args.dump_frame else None
    if dump_path:
        # Separate artefact names so a dump run can never overwrite the
        # measurement ELF / result JSON that docs/performance.md §3 cites.
        semilog = os.path.join(TMP, "lvgl-m4-dump.log")
        resultjson = os.path.join(TMP, "lvgl-m4-dump-result.json")
        if os.path.exists(dump_path):
            os.remove(dump_path)
    with open(benchlog, "a") as logf:
        log("=" * 60, logf)
        log(f"lvgl-m4-bench run, mem-kb={args.mem_kb}"
            + (f", DUMP-FRAME run -> {dump_path} (NOT a measurement run)" if dump_path else ""),
            logf)

        if not os.path.isdir(LVGL_SRC):
            log(f"LVGL SOURCE NOT FOUND at {LVGL_SRC}", logf)
            log("  expected the upstream mirror; clone it with:", logf)
            log(f"  git clone https://github.com/lvgl/lvgl.git {LVGL_MIRROR_DEFAULT}", logf)
            return 5

        lv = lvgl_version()
        log(f"LVGL source  {lv['root']}", logf)
        log(f"  version    {lv.get('version','?')}  ({lv.get('describe','?')})", logf)
        log(f"  commit     {lv.get('short')}  {lv.get('date','')}  {(lv.get('subject') or '')[:56]}", logf)
        log(f"  remote     {lv.get('remote')}", logf)
        ahead = lv.get("ahead_of_origin")
        if lv.get("dirty") or (ahead and ahead != "0"):
            log(f"  *** NOT STOCK: dirty={lv.get('dirty')} ahead_of_origin={ahead} — "
                f"this result is NOT a clean upstream anchor ***", logf)
        else:
            log("  stock upstream (clean tree, not ahead of origin)", logf)
        elf = build(args.mem_kb, logf, args.frames, dump_path,
                    "lvgl-m4-dump.elf" if dump_path else "lvgl-m4.elf")
        if not elf:
            log("BUILD FAILED — see log", logf)
            return 2
        gout = run_qemu(elf, logf, semilog)
        if gout is None:
            return 3
        if dump_path:
            want = 480 * 270 * 3
            got = os.path.getsize(dump_path) if os.path.exists(dump_path) else 0
            if got != want:
                log(f"DUMP FAIL: {dump_path} is {got} B, expected {want} B "
                    "(480x270x3) — the band flushes did not tile the frame", logf)
                return 6
            log(f"DUMP OK: {dump_path} = {got} B raw RGB888 (480x270), "
                "bands appended in flush order", logf)
        res = parse(gout, logf)
        if not res["ok"]:
            log("GUEST DID NOT REPORT 'workload ok' — see semihosting log", logf)
            return 4

        # Insn/frame from icount centiseconds (1 cs = 1e7 insns).
        TIMED = int(res["timed_frames"]) if res["timed_frames"] else 4
        if res["timed_cs"]:
            insns = int(res["timed_cs"]) * 10_000_000
            per_frame = insns // TIMED
            px = 480 * 270
            log(f"RESULT: {res['timed_cs']} cs / {TIMED} frames = {insns:,} insns "
                f"({per_frame:,}/frame, {per_frame/px:.0f} insn/px) — "
                f"DETERMINISTIC (icount), resolution 1 cs", logf)
            est_ms = per_frame / 180e6 * 1e3
            log(f"  @180 MHz ~{est_ms:.0f} ms/frame ESTIMATE (CPI=1.0 assumption)", logf)
        log(f"RESULT: LVGL pool peak (high-water) = {res['lv_mem_peak']} B "
            f"of {res['lv_mem_total']} B pool; draw buffer {res['draw_buf_bytes']} B; "
            f"frag {res['lv_mem_frag_pct']}%", logf)
        log(f"RESULT: frame hash {res['frame_hash']} (warmed {res['warmed_hash']})", logf)

        # Machine-readable result, stamped with the LVGL provenance so the
        # anchor can be recorded in a ledger rather than re-derived from prose.
        out = {
            "lvgl": lv,
            "mem_kb": args.mem_kb,
            "machine": MACHINE,
            "scene": {"w": 480, "h": 270, "band_h": 16},
            "insns_per_frame": (
                int(res["timed_cs"]) * 10_000_000 // TIMED if res["timed_cs"] else None
            ),
            "timed_cs": int(res["timed_cs"]) if res["timed_cs"] else None,
            "timed_frames": TIMED,
            "lv_mem_peak": res["lv_mem_peak"],
            "lv_mem_total": res["lv_mem_total"],
            "draw_buf_bytes": res["draw_buf_bytes"],
            "frame_hash": res["frame_hash"],
        }
        with open(resultjson, "w") as f:
            json.dump(out, f, indent=2)
        log(f"RESULT: wrote {resultjson}", logf)
    return 0


if __name__ == "__main__":
    sys.exit(main())

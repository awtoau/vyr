#!/usr/bin/env python3
"""board-testcard.py — put the LABELLED COLOUR TEST CARD on the real
STM32F429I-DISC1's panel, through every presentation path, and leave the one
under question displaying it.

THE QUESTION
------------
`--features present` renders RGB888 straight into the SDRAM framebuffer. RM0090
says LTDC's RGB888 layer reads B at the LOWEST address while vyr-core writes
R,G,B, so a correcting R/B swap pass was added -- and it costs 59.7 ms against
the 12.59 ms blit it replaced, which REVERSES the verdict for direct rendering.
The premise has never been checked on glass, because the only thing ever
displayed was a dashboard mock-up where "does that look right?" is a judgement
call.

THE EXPERIMENT
--------------
Three flashes, same card, same renderer, same IR:

  lcd      SPI5 -> ILI9341 GRAM.   RGB888->RGB565 by vyr's own software fold.
           No LTDC layer involved at all.                      CONTROL
  ltdc     banded render + blit into an RGB565 SDRAM framebuffer, LTDC scanning
           it as L1PFCR=2. Software fold again.                CONTROL
  present  DIRECT render of RGB888 into SDRAM, LTDC scanning it as L1PFCR=1,
           and NO R/B SWAP APPLIED.                            THE EXPERIMENT

If the controls read correctly and `present` shows the block labelled RED as
BLUE, LTDC's RGB888 layer is B-first and the swap is required. If `present`
shows RED as RED, the swap is unnecessary and direct rendering wins outright.
Every swatch is labelled with the colour it is SUPPOSED to be, so this is a
reading, not an opinion, and a photograph of the glass is evidence.

WHAT THIS SCRIPT CAN AND CANNOT DECIDE
--------------------------------------
It can prove: each path rendered the card; the card body's FNV-1a hash is
identical across all three paths and equal to the host x86-64 / emulated-M4
reference; the framebuffer holds the bytes the renderer wrote; LTDC reports no
underrun. It CANNOT see the panel. The colour verdict is the human's, and the
script says so rather than implying otherwise.

SHARED HARDWARE: another agent may be driving the same board. Every probe-rs
invocation is wrapped in the exclusive lock at the PRIMARY checkout's
tmp/.board.lock (atomic mkdir), held for exactly one flash+run, released in a
finally.

--card orient: THE OTHER QUESTION
--------------------------------
The same three flashes, the same mechanism, a different asset: the CORNER AND
ORIENTATION CARD (`--features ...,orientcard`). It asks which PHYSICAL corner is
the renderer's (0,0), which way +X and +Y run on the glass, and whether the scan
is mirrored — none of which the colour card can answer. Four numbered corner
badges, two direction wedges, edge rulers and an origin block make all EIGHT
orientations (four rotations x mirrored/not) look different.

One difference matters: the colour card's `present` leg deliberately applies NO
R/B correction, because whether it is needed is the question. That has since
been settled on the glass — LTDC's RGB888 layer IS B-first — so the orientation
card's `present` leg DOES apply `swap_rb_in_place`, and a wrong-colour card
cannot muddle an orientation reading. The `lcd` and `ltdc` legs convert to
RGB565 in vyr's own software and were never affected either way.

Usage:  python3 scripts/board-testcard.py [--card colour|orient]
                                          [--paths lcd,ltdc,present]
Output: tmp/board-<card>card.json, per-path semihosting in
        tmp/board-<card>card-<path>.log, log tmp/board-<card>card.log.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TMP = REPO / "tmp"

CHIP = "STM32F429ZI"
# THIS probe only. A second F42x board (0483:374e:...) is attached to the same
# workstation and must never be driven from this repo.
PROBE = "0483:3752:0671FF484971754867174427"
TARGET = "thumbv7em-none-eabihf"
PROFILE = "release-mcu"
ELF = REPO / "target" / TARGET / PROFILE / "vyr-size"

# Wall-clock guard per flash+run (Python-side poll, never a shell timeout).
# The lcd leg pushes two full 240x320 frames over a 5.625 MHz SPI (~0.44 s of
# wire) plus the ~13 s measurement workload; the present leg adds the whole
# three-tier presentation comparison (~0.4 s per Exact full-frame repetition).
DEADLINE_S = {"lcd": 300, "ltdc": 300, "present": 900}

# Order matters: the EXPERIMENT is flashed LAST so it is what the board is left
# showing, per the standing instruction that the configuration in question is
# the one on the glass at the end.
PATHS = {
    "lcd": ("board,lcd,{card}",
            "SPI5 -> ILI9341 GRAM, RGB565 on the wire (CONTROL)"),
    "ltdc": ("board,ltdc,{card}",
             "banded render + blit -> RGB565 SDRAM fb, LTDC L1PFCR=2 (CONTROL)"),
    "present": ("board,ltdc,present,{card}",
                "DIRECT RGB888 -> SDRAM, LTDC L1PFCR=1 (colour card: NO R/B "
                "swap, the experiment; orientation card: R/B swap APPLIED)"),
}

# `--card X` picks the cargo feature, the output stem and the sentence the human
# is asked to answer. Everything else — build, lock, flash, parse, hash
# comparison — is identical, because it is the same fixture.
CARDS = {
    "colour": {
        "feature": "testcard",
        "stem": "board-testcard",
        "question": "does LTDC's RGB888 layer read B at the lowest address? "
                    "measured by reading labelled swatches off the glass",
        "confirm": (
            "NOT MACHINE-VERIFIABLE. Look at the panel, which is left showing "
            "the `present` build: the card rendered DIRECT as RGB888 with NO "
            "R/B swap. Read the labels. RED block blue (and BLUE red, YELLOW "
            "cyan, CYAN yellow, MAGENTA unchanged) => LTDC's RGB888 layer is "
            "B-first and the correcting swap IS required. RED block red => the "
            "swap is unnecessary and direct rendering wins outright. Compare "
            "against tmp/testcard-host.png, which is the same card rendered on "
            "x86-64 and is what it is supposed to look like."),
    },
    "orient": {
        "feature": "orientcard",
        "stem": "board-orientcard",
        "question": "where is the renderer's (0,0) on the physical panel, which "
                    "way do +X and +Y run, and is the scan mirrored? measured "
                    "by reading numbered corners and direction wedges off the "
                    "glass",
        "confirm": (
            "NOT MACHINE-VERIFIABLE. Look at the panel and answer two "
            "questions. (1) Which PHYSICAL corner shows the SOLID WHITE badge "
            "numbered 1 — top-left, top-right, bottom-right or bottom-left as "
            "you are holding the board? That fixes the rotation. (2) Which "
            "physical direction does the YELLOW 'X+ ->' wedge thicken in? That "
            "confirms it independently of any glyph. Two free cross-checks: "
            "badges 1>2>3>4 must run CLOCKWISE on the glass, and all text must "
            "read forwards — either one reversed means the scan is MIRRORED, "
            "not merely rotated. Compare against tmp/orientcard-host.png, the "
            "same card rendered on x86-64, which is what the renderer thinks "
            "it drew."),
    },
}

# The 480x270 measurement frame's cross-ISA reference hashes -- untouched by any
# of this, and asserted so a display feature cannot quietly move them.
REFERENCE_FRAME_HASH = "0x24dcaff531c6eb01"

sys.path.insert(0, str(HERE))
from board_lock import BoardLock, LOCK  # noqa: E402

AGENT = "board-testcard"

CARD_RE = re.compile(
    r"testcard: path=(?P<path>.+?) body=(?P<w>\d+)x(?P<h>\d+) "
    r"fnv1a=(?P<hash>0x[0-9a-f]{16}) quality=(?P<quality>\w+)")
# The firmware's framebuffer probe line names its own sample points, because
# they differ per card (colour: the RGB swatch centres; orientation: the origin
# block and the two wedges). Parsing NAME(x,y)=[bb bb bb] generically means the
# runner does not have to know which card it flashed to report what the memory
# held.
PROBE_RE = re.compile(r"([A-Z]+\(\d+,\d+\))=\[([0-9a-f]{2}(?: [0-9a-f]{2}){2})\]")
# Present-leg R/B correction, orientation card only: `correct=true` is the
# machine's own check that the swap produced the byte order it computed as the
# expectation from the pre-swap contents.
SWAP_RE = re.compile(r"rb_swap APPLIED cycles=(\d+) .*correct=(\w+)")

_lines: list[str] = []


def now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg: str, logf=None) -> None:
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    _lines.append(line)
    if logf:
        logf.write(line + "\n")
        logf.flush()


def build(feats: str, tag: str, logf) -> tuple[str, str] | None:
    """Build a feature set and SNAPSHOT the ELF: cargo writes every feature
    combination of this bin to the same path, so a concurrent build could
    otherwise swap the image between build and flash."""
    cmd = ["cargo", "build", "-p", "vyr-size", "--target", TARGET,
           "--profile", PROFILE, "--no-default-features", "--features", feats]
    log(f"build [{tag}]: {' '.join(cmd)}", logf)
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, check=False)
    logf.write(r.stdout + r.stderr + "\n")
    logf.flush()
    if r.returncode != 0:
        log(f"*** BUILD FAILED [{tag}] — see log ***", logf)
        return None
    snap = TMP / f"board-testcard-{tag}.elf"
    shutil.copyfile(ELF, snap)
    digest = hashlib.sha256(snap.read_bytes()).hexdigest()
    log(f"snapshot {snap} (sha256:{digest[:16]}, {snap.stat().st_size:,} B)", logf)
    return str(snap), digest


def run_once(elf: str, semilog: Path, deadline: int, logf) -> str:
    """One flash+run. THE ONLY place the board is touched — the lock is held
    here and nowhere else, for exactly as long as probe-rs runs."""
    args = ["probe-rs", "run", "--chip", CHIP, "--probe", PROBE,
            "--catch-hardfault", "--non-interactive",
            "--disable-progressbars", elf]
    log("probe-rs: " + " ".join(args), logf)
    t0 = time.monotonic()
    lock = BoardLock(AGENT, lambda m: log(m, logf))
    try:
        lock.__enter__()
        try:
            p = subprocess.run(args, cwd=REPO, capture_output=True, text=True,
                               timeout=deadline, check=False)
            out, rc = p.stdout + p.stderr, p.returncode
        except subprocess.TimeoutExpired as e:
            def dec(x):
                x = x or b""
                return x.decode(errors="replace") if isinstance(x, bytes) else x
            out, rc = dec(e.stdout) + dec(e.stderr), None
            log(f"*** target did not finish inside the {deadline}s guard — "
                f"killed; partial output in {semilog} ***", logf)
    finally:
        lock.__exit__(None, None, None)
    semilog.write_text(out)
    log(f"probe-rs wall {time.monotonic() - t0:.1f}s rc={rc}; "
        f"semihosting -> {semilog}", logf)
    return out


def parse(text: str) -> dict:
    def g(pat, cast=str):
        m = re.search(pat, text)
        return cast(m.group(1)) if m else None

    card = CARD_RE.search(text)
    swap = SWAP_RE.search(text)
    return {
        "is_board_image": "REAL SILICON" in text,
        "sysclk_hz": g(r"sysclk_hz=(\d+)", int),
        "card_path_label": card.group("path") if card else None,
        "card_hash": card.group("hash") if card else None,
        "card_body": (f"{card.group('w')}x{card.group('h')}" if card else None),
        "card_quality": card.group("quality") if card else None,
        # The three display legs each print one ALERT naming the card; matching
        # the shapes rather than the card name keeps this card-agnostic.
        "card_on_glass": any(s in text for s in (
            "is on the glass via SPI",
            "is in the RGB565 framebuffer",
            "the panel is showing the")),
        "fb_probes": dict(PROBE_RE.findall(text)) or None,
        "rb_swap_applied": bool(swap),
        "rb_swap_cycles": int(swap.group(1)) if swap else None,
        "rb_swap_correct": (swap.group(2) == "true") if swap else None,
        "sdram_ok": g(r"sdram test .*ok=(\w+)") == "true",
        "scanout_live": "scan-out LIVE" in text,
        "rgb888_live": "present: now scanning RGB888" in text,
        "rgb888_underrun": g(r"now scanning RGB888 .*fifo_underrun=(\w+)") == "true",
        "rgb888_l1pfcr": g(r"now scanning RGB888 .*L1PFCR=(0x[0-9a-f]+)"),
        "panel_probe_answered": g(r"lcd probe .*answered=(\w+)") == "true",
        "frame_hash": g(r"\[vyr-size\] frame fnv1a=(0x[0-9a-f]{16})"),
        "workload_ok": "workload ok" in text,
        "hardfault": "cpu exception" in text or "HardFault" in text,
        "panic": "FATAL [vyr-size] panic" in text,
        "render_error": g(r"ERROR \[vyr-size\] (.*(?:failed|FAILED).*)"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", choices=sorted(CARDS), default="colour",
                    help="which card to flash: colour (channel order) or "
                         "orient (corner/axis/mirror)")
    ap.add_argument("--paths", default="lcd,ltdc,present",
                    help="comma-separated subset of lcd,ltdc,present "
                         "(order is forced: the experiment flashes last)")
    ap.add_argument("--reference-hash", default=None,
                    help="the host/emulated card hash to assert against "
                         "(from scripts/testcard-host.py)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    card = CARDS[args.card]
    stem = card["stem"]
    outfile = args.out or str(TMP / f"{stem}.json")
    want = [p for p in ("lcd", "ltdc", "present")
            if p in {s.strip() for s in args.paths.split(",")}]

    TMP.mkdir(exist_ok=True)
    out: dict = {
        "when": now(),
        "board": "STM32F429I-DISC1 (STM32F429ZI, Cortex-M4F)",
        "probe": PROBE,
        "lock": str(LOCK),
        "card": args.card,
        "card_feature": card["feature"],
        "question": card["question"],
        "reference_card_hash": args.reference_hash,
        "paths": {},
    }
    rc = 0
    with open(TMP / f"{stem}.log", "a") as logf:
        log("=" * 78, logf)
        log(f"board-testcard --card {args.card}: {CHIP} via ST-LINK {PROBE}; "
            f"paths={want}", logf)

        for path in want:
            feats, what = PATHS[path]
            feats = feats.format(card=card["feature"])
            log("", logf)
            log(f"--- {path}: {what}", logf)
            built = build(feats, f"{args.card}-{path}", logf)
            if not built:
                rc = rc or 2
                continue
            elf, sha = built
            text = run_once(elf, TMP / f"{stem}-{path}.log",
                            DEADLINE_S[path], logf)
            r = parse(text)
            r["elf"], r["elf_sha256"], r["features"] = elf, sha, feats
            out["paths"][path] = r
            log(f"  card hash={r['card_hash']} ({r['card_body']}, "
                f"{r['card_quality']}) drawn_by={r['card_path_label']}", logf)
            log(f"  on_glass={r['card_on_glass']} sdram_ok={r['sdram_ok']} "
                f"scanout_live={r['scanout_live']} "
                f"workload_frame={r['frame_hash']} ok={r['workload_ok']}", logf)
            if r["fb_probes"]:
                log(f"  framebuffer bytes in ADDRESS order (pre-correction): "
                    + " ".join(f"{k}=[{v}]" for k, v in r["fb_probes"].items()),
                    logf)
            if r["rb_swap_applied"]:
                log(f"  R/B correction applied: {r['rb_swap_cycles']} cycles, "
                    f"byte order verified correct={r['rb_swap_correct']}", logf)
                if not r["rb_swap_correct"]:
                    rc = rc or 8
            if r["panic"] or r["hardfault"]:
                log(f"  *** TARGET FAULTED (panic={r['panic']} "
                    f"hardfault={r['hardfault']}) ***", logf)
                rc = rc or 3
            if r["card_hash"] is None:
                log("  *** NO CARD HASH — the card did not render ***", logf)
                rc = rc or 4
            if r["frame_hash"] not in (None, REFERENCE_FRAME_HASH):
                log(f"  *** MEASUREMENT FRAME HASH MOVED: {r['frame_hash']} != "
                    f"{REFERENCE_FRAME_HASH} ***", logf)
                rc = rc or 5

        # --- what the machine can settle -----------------------------------
        hashes = {p: r["card_hash"] for p, r in out["paths"].items()}
        uniq = {h for h in hashes.values() if h}
        out["card_hash_by_path"] = hashes
        out["all_paths_same_pixels"] = (len(uniq) == 1 and len(hashes) == len(want))
        if args.reference_hash:
            out["matches_host_reference"] = (uniq == {args.reference_hash})
        log("", logf)
        log(f"CARD HASH BY PATH: {json.dumps(hashes)}", logf)
        log(f"all paths identical: {out['all_paths_same_pixels']}"
            + (f"; matches host/emulated reference {args.reference_hash}: "
               f"{out.get('matches_host_reference')}" if args.reference_hash else ""),
            logf)
        if not out["all_paths_same_pixels"]:
            log("*** PATHS DISAGREE ON PIXELS — that is a finding, not a nit ***",
                logf)
            rc = rc or 6
        if args.reference_hash and not out.get("matches_host_reference"):
            log("*** BOARD CARD HASH != HOST/EMULATED REFERENCE ***", logf)
            rc = rc or 7

        out["machine_verified"] = {
            "every_path_rendered_the_card": all(
                r["card_hash"] for r in out["paths"].values()),
            "all_paths_byte_identical": out["all_paths_same_pixels"],
            "measurement_frame_unchanged": all(
                r["frame_hash"] in (None, REFERENCE_FRAME_HASH)
                for r in out["paths"].values()),
            "rgb888_layer_live_no_underrun": bool(
                out["paths"].get("present", {}).get("rgb888_live")
                and not out["paths"].get("present", {}).get("rgb888_underrun")),
            "rb_correction_verified": out["paths"].get("present", {}).get(
                "rb_swap_correct"),
        }
        out["human_must_confirm"] = card["confirm"]
        Path(outfile).write_text(json.dumps(out, indent=2) + "\n")
        log(f"wrote {outfile}", logf)
        if "present" in out["paths"]:
            swapped = out["paths"]["present"].get("rb_swap_applied")
            log("THE BOARD IS LEFT SHOWING: present (direct RGB888, "
                + ("R/B correction applied)" if swapped else "no swap)"), logf)
        log("HUMAN MUST CONFIRM: " + card["confirm"], logf)
    return rc


if __name__ == "__main__":
    sys.exit(main())

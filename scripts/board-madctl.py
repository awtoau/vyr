#!/usr/bin/env python3
"""board-madctl.py — sweep the ILI9341's MADCTL register on the real panel, one
flash and one QUESTION at a time, and record what a human read off the glass.

THE TWO SYMPTOMS
----------------
1. The panel is rotated 180 degrees. Read on the glass from the orientation
   card: the solid white badge numbered 1, which the renderer draws at the
   TOP-LEFT, appears at the physical BOTTOM-RIGHT; badges 1>2>3>4 still run
   CLOCKWISE and the text is not mirrored. Clockwise-preserved plus unmirrored
   text is a pure rotation, and badge 1 at bottom-right is uniquely 180 degrees.
2. Red and blue are swapped. Read on the glass from the labelled colour card
   rendered through the DIRECT RGB888 path with no correcting pass.

THE HYPOTHESIS
--------------
MADCTL is currently ST's 0xC8:

    0x80 MY   row address order      SET
    0x40 MX   column address order   SET
    0x20 MV   row/column exchange    clear
    0x08 BGR  colour order           SET

MY|MX together IS a 180 degree rotation, and BGR is the obvious candidate for
the channel swap. If both are true then MADCTL = 0x00 fixes orientation AND
colour with a single register write, replacing a 59.7 ms software byte-swap
pass that costs 4.7x the blit it was meant to replace.

That is a HYPOTHESIS. ST ships 0xC8 presumably for a reason, and MADCTL's
interaction with the LTDC RGB888 layer format is not obvious. This script does
not reason about it: it flashes candidates and asks.

WHY ALL THREE PATHS
-------------------
The paths are the discriminator, not a formality:

  lcd      SPI5 -> ILI9341 GRAM, RGB888->RGB565 by vyr's OWN software fold.
           No LTDC layer format involved.                       CONTROL
  ltdc     banded render + blit into an RGB565 framebuffer, LTDC scanning it as
           L1PFCR=2. Software fold again.                       CONTROL
  present  DIRECT RGB888 into SDRAM, LTDC scanning it as L1PFCR=1.
                                                                THE SUBJECT

If a MADCTL change moves all three together, MADCTL owns the mapping. If it
moves only the LTDC-fed paths, the LTDC layer geometry or L1PFCR owns part of
it. If it moves the two software-fold paths one way and the RGB888 path the
other, then MADCTL's BGR bit and LTDC's byte order are two separate faults that
happen to share a symptom, and one register cannot fix both.

NO DOUBLE SWAPS
---------------
MADCTL's BGR bit and `present::swap_rb_in_place` swap the same two channels.
Both active CANCEL, and a correct-looking panel would be a wrong configuration
misread as a right one. So every sweep flash sets VYR_RB_SWAP=0 by default, the
firmware logs which corrections were live, and the card's own on-glass identity
strip prints `MADCTL 0xNN ... / RB sw none|SWAPPED` in the bottom three rows.
Read that strip before believing the picture.

RESUMABLE BY DESIGN
-------------------
One flash, one question, one recorded answer, then the next -- never a marathon
reading session. State lives in tmp/madctl-sweep.json and every run is either
`observed: null` (waiting on a human) or filled in.

    python3 scripts/board-madctl.py --plan
    python3 scripts/board-madctl.py --flash --path present --madctl 0x00
    python3 scripts/board-madctl.py --record present-0x00-rb0 \\
        --badge1 bottom-right --edge-wedge yellow --text forwards
    python3 scripts/board-madctl.py --status

SHARED HARDWARE: every probe-rs invocation is wrapped in the exclusive lock at
the PRIMARY checkout's tmp/.board.lock (atomic mkdir), held for exactly one
flash+run and released in a finally.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TMP = REPO / "tmp"
STATE = TMP / "madctl-sweep.json"

CHIP = "STM32F429ZI"
# THIS probe only. A second F42x board (0483:374e:...) is attached to the same
# workstation and must never be driven from this repo.
PROBE = "0483:3752:0671FF484971754867174427"
TARGET = "thumbv7em-none-eabihf"
PROFILE = "release-mcu"
ELF = REPO / "target" / TARGET / PROFILE / "vyr-size"

# Wall-clock guard per flash+run (Python-side poll, never a shell timeout).
DEADLINE_S = {"lcd": 300, "ltdc": 300, "present": 900}

PATHS = {
    "lcd": ("board,lcd,{card}",
            "SPI5 -> ILI9341 GRAM, vyr's own RGB888->RGB565 fold (CONTROL: no "
            "LTDC layer format is involved at all)"),
    "ltdc": ("board,ltdc,{card}",
             "banded render + blit -> RGB565 SDRAM fb, LTDC L1PFCR=2 (CONTROL: "
             "LTDC scan-out, but not LTDC's RGB888 pixel mapping)"),
    "present": ("board,ltdc,present,{card}",
                "DIRECT RGB888 -> SDRAM, LTDC L1PFCR=1 (THE SUBJECT: the path "
                "the 59.7 ms software swap exists for)"),
}

CARDS = {
    "orient": {"feature": "orientcard", "hash": "0x50bf6c0f146d4d4b",
               "ref_png": "tmp/orientcard-host.png"},
    "colour": {"feature": "testcard", "hash": "0x65b88925c9a2ba19",
               "ref_png": "tmp/testcard-host.png"},
}

# The 480x270 measurement frame's cross-ISA reference hash -- untouched by any
# of this, and asserted so a display parameter cannot quietly move it.
REFERENCE_FRAME_HASH = "0x24dcaff531c6eb01"

# MADCTL bits, most significant first (ILI9341 datasheet 8.2.29).
BITS = [(0x80, "MY"), (0x40, "MX"), (0x20, "MV"),
        (0x10, "ML"), (0x08, "BGR"), (0x04, "MH")]

# What is already known, and is therefore the baseline every prediction is
# stated RELATIVE to. Both were read off the glass by a human.
BASELINE = {
    "madctl": 0xC8,
    "present_rotation": "180",
    "present_colour_raw": "rb-swapped",
    "lcd_rotation": None,     # never read
    "lcd_colour": None,       # never read
    "ltdc_rotation": None,    # never read
    "ltdc_colour": None,      # never read
}

# Read off the glass at MADCTL=0x00 with NO software correction (run
# orient-present-0x00-rb0): badge 1 top-left, the X+ wedge YELLOW, text
# forwards. So on the SUBJECT path, MY|MX owned the rotation and BGR owned the
# channel order, and one register write fixes both.
#
# It is not yet a conclusion, and the reason is stated rather than assumed: if
# this panel is physically BGR-wired then BGR=1 is CORRECT for the two paths
# that fold RGB565 in vyr's own software, and 0x00 would be fixing `present` by
# BREAKING `lcd` and `ltdc`. That would mean no single MADCTL serves all three,
# the present fault belongs to LTDC's memory byte order, and the free fix is a
# BGR888 output format rather than a register bit. The control readings are the
# only thing that can tell those two worlds apart.
CONFIRMED = {
    "present-0x00": {"rotation": "upright", "channels": "true",
                     "mirrored": False},
}

# The recommended order. Decisive information first; the separating candidates
# (0x08 / 0xC0) are only needed if a bracket reading is surprising.
PLAN = [
    ("present", 0xC8, "BASELINE on this card: re-confirm BOTH symptoms in one "
                      "reading, with NO software correction active, so every "
                      "later run is a comparison against a reading and not "
                      "against a memory."),
    ("present", 0x00, "THE HYPOTHESIS: MY|MX and BGR all cleared. If this reads "
                      "upright AND true-coloured, one register write replaces "
                      "the 59.7 ms pass."),
    ("lcd", 0x00, "THE CONTROL that can refute it: the SPI path's channel order "
                  "comes from the panel alone. If 0x00 makes THIS path's "
                  "colours wrong, BGR was correct for the panel and 0x00 fixes "
                  "the RGB888 path by breaking the other two."),
    ("ltdc", 0x00, "The third path. Two software-fold paths agreeing pins the "
                   "reading to MADCTL rather than to one leg's blit."),
    ("lcd", 0xC8, "Only if the lcd@0x00 reading is ambiguous: the same path at "
                  "ST's value, to see which way its colours moved."),
    ("present", 0x08, "SEPARATOR: clears MY|MX, keeps BGR. Isolates the rotation "
                      "bits from the colour bit on the subject path."),
    ("present", 0xC0, "SEPARATOR: keeps MY|MX, clears BGR. The other half of the "
                      "same isolation."),
]

sys.path.insert(0, str(HERE))
from board_lock import BoardLock, LOCK  # noqa: E402

AGENT = "board-madctl"

CARD_RE = re.compile(
    r"testcard: path=(?P<path>.+?) body=(?P<w>\d+)x(?P<h>\d+) "
    r"fnv1a=(?P<hash>0x[0-9a-f]{16}) quality=(?P<quality>\w+)")
PROBE_RE = re.compile(r"([A-Z]+\(\d+,\d+\))=\[([0-9a-f]{2}(?: [0-9a-f]{2}){2})\]")

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


def flags(v: int) -> str:
    s = "|".join(n for b, n in BITS if v & b)
    return s or "none"


def run_id(path: str, madctl: int, rb: int, card: str) -> str:
    return f"{card}-{path}-{madctl:#04x}-rb{rb}"


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"created": now(), "baseline": BASELINE, "runs": {}}


def save_state(st: dict) -> None:
    st["updated"] = now()
    STATE.write_text(json.dumps(st, indent=2) + "\n")


# --- what each candidate predicts -------------------------------------------

def predict(path: str, madctl: int, rb: int) -> dict:
    """What the reading SHOULD be under each hypothesis, stated before the
    flash so the reading can refute it rather than be fitted to it."""
    base = BASELINE["madctl"]
    rot_changed = bool((madctl ^ base) & 0xC0)
    mv = bool(madctl & 0x20)
    bgr_changed = bool((madctl ^ base) & 0x08)

    if mv:
        rot = ("TRANSPOSED (MV set): the card should be landscape on a portrait "
               "panel — expect it clipped or stretched, and report what you see")
    elif rot_changed:
        rot = ("UPRIGHT — badge 1 at the physical TOP-LEFT — IF MY|MX own the "
               "180 degrees on this path")
    else:
        rot = ("ROTATED 180 — badge 1 at the physical BOTTOM-RIGHT — because "
               "MY|MX are unchanged from the 0xC8 baseline")

    # Colour, expressed as the ONE thing to look at: the long edge wedge, drawn
    # YELLOW by the renderer. R and B swapped turns yellow into cyan; the other
    # wedge is magenta, which is invariant under an R/B swap and so says
    # nothing.
    if path == "present":
        raw = BASELINE["present_colour_raw"] == "rb-swapped"
        # Software swap flips it back; a BGR change flips it again.
        wrong = raw ^ bool(rb) ^ bgr_changed
        col = (f"the long edge wedge should read "
               f"{'CYAN (channels still swapped)' if wrong else 'YELLOW (true colours)'} "
               f"IF MADCTL's BGR bit governs this path's channel order "
               f"(baseline: 0xC8 with no software swap reads CYAN)")
    elif bgr_changed:
        # BGR cleared relative to ST's 0xC8, on a path that folds RGB565 in
        # vyr's own software. THIS is the reading that can refute the
        # hypothesis, so both outcomes are spelt out before the flash.
        col = ("the X+ wedge is drawn YELLOW, and this is THE DECIDING READING. "
               "YELLOW => MADCTL owns the channel mapping for every path, "
               "0x00 is right everywhere, and the 59.7 ms software pass can go. "
               "CYAN => BGR=1 was CORRECT for this path all along, 0x00 fixes "
               "the direct RGB888 path by BREAKING this one, no single MADCTL "
               "serves all three, and the direct path's fault is LTDC's memory "
               "byte order — fixable free by emitting BGR888 from the renderer "
               "(#22), not by a register bit.")
    else:
        col = ("the X+ wedge is drawn YELLOW; whether it READS yellow or cyan "
               "at ST's 0xC8 on this path has never been recorded, so this "
               "reading establishes it. If the same path with BGR cleared reads "
               "the other colour, MADCTL's BGR bit governs this path.")
    return {
        "rotation": rot,
        "colour": col,
        "mirroring": ("badges 1>2>3>4 must still run CLOCKWISE and the text must "
                      "read forwards; either one reversed means the scan is "
                      "MIRRORED, which no MY/MX/MV combination alone produces "
                      "and would be a finding in itself"),
    }


QUESTION = """\
LOOK AT THE PANEL. Three things, in one glance:

  (1) WHICH PHYSICAL CORNER shows the SOLID WHITE badge numbered 1 --
      top-left, top-right, bottom-right or bottom-left, as you are holding
      the board? (That fixes the rotation. The renderer draws it top-left.)
  (2) FIND THE WEDGE LABELLED `X+ ->` -- it is the one with the arrow and
      the letter X, running beside the ruler marked x=0 ... x=239. Is that
      wedge YELLOW or CYAN? (The renderer draws it YELLOW. Cyan means red
      and blue are swapped.) IGNORE the OTHER wedge, the one labelled `Y+`:
      it is drawn magenta, and magenta looks identical whether or not red
      and blue are swapped, so it answers nothing. Identify the wedge by its
      `X+` LABEL, not by which edge it is on or how long it is -- the panel's
      rotation is exactly what is in question, so "the top edge" is not a
      reliable way to point at it.
  (3) DOES THE TEXT READ FORWARDS? (Upside-down is fine and expected under a
      180 rotation; MIRRORED is a different finding.)

CROSS-CHECK, free: badges 1>2>3>4 must run CLOCKWISE on the glass.
CONFIGURATION, on the glass: the bottom three rows of the card print the
MADCTL actually written and whether any software R/B pass ran. Read them --
a correct-looking panel from two corrections that cancel is not a correct
configuration.
REFERENCE: {ref} is the same card rendered on x86-64, i.e. what the renderer
believes it drew.
"""


def build(feats: str, madctl: int, rb: int, tag: str, logf):
    """Build with the sweep parameters in the environment, then SNAPSHOT the
    ELF: cargo writes every feature combination of this bin to the same path,
    so a concurrent build could otherwise swap the image between build and
    flash."""
    env = dict(os.environ)
    env["VYR_MADCTL"] = f"{madctl:#04x}"
    env["VYR_RB_SWAP"] = str(rb)
    cmd = ["cargo", "build", "-p", "vyr-size", "--target", TARGET,
           "--profile", PROFILE, "--no-default-features", "--features", feats]
    log(f"build [{tag}]: VYR_MADCTL={env['VYR_MADCTL']} "
        f"VYR_RB_SWAP={env['VYR_RB_SWAP']} {' '.join(cmd)}", logf)
    r = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True,
                       check=False)
    logf.write(r.stdout + r.stderr + "\n")
    logf.flush()
    if r.returncode != 0:
        log(f"*** BUILD FAILED [{tag}] — see log ***", logf)
        return None
    snap = TMP / f"board-madctl-{tag}.elf"
    shutil.copyfile(ELF, snap)
    digest = hashlib.sha256(snap.read_bytes()).hexdigest()
    log(f"snapshot {snap} (sha256:{digest[:16]}, {snap.stat().st_size:,} B)", logf)
    return str(snap), digest


def run_once(elf: str, semilog: Path, deadline: int, logf) -> str:
    """One flash+run. THE ONLY place the board is touched -- the lock is held
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
    return {
        "is_board_image": "REAL SILICON" in text,
        "card_path_label": card.group("path") if card else None,
        "card_hash": card.group("hash") if card else None,
        "card_body": (f"{card.group('w')}x{card.group('h')}" if card else None),
        # What the RUNNING TARGET says it wrote -- the parameter verified on the
        # flashed image, not on the build command line.
        "madctl_written": g(r"madctl_written=(0x[0-9a-f]{2})"),
        "madctl_in_alert": g(r"CORRECTIONS ACTIVE: MADCTL=(0x[0-9a-f]{2})"),
        "rb_swap_in_alert": g(r"software R/B swap=(APPLIED|NOT applied)"),
        # Raw RDDMADCTL read back over the bit-banged SPI. Dummy bits are NOT
        # stripped, so it is corroboration, not an equality test.
        "rddmadctl_raw": g(r"RDDMADCTL\(0B\)=\[([0-9a-f ]+)\]"),
        "rb_swap_applied": "rb_swap APPLIED" in text,
        "rb_swap_skipped": ("rb_swap NOT applied" in text
                            or "rb_swap SKIPPED" in text),
        "rb_swap_correct": (g(r"rb_swap APPLIED .*correct=(\w+)") == "true"
                            if "rb_swap APPLIED" in text else None),
        "fb_probes": dict(PROBE_RE.findall(text)) or None,
        "sdram_ok": g(r"sdram test .*ok=(\w+)") == "true",
        "scanout_live": "scan-out LIVE" in text,
        "rgb888_live": "present: now scanning RGB888" in text,
        "rgb888_underrun": g(r"now scanning RGB888 .*fifo_underrun=(\w+)") == "true",
        "panel_probe_answered": g(r"lcd probe .*answered=(\w+)") == "true",
        "frame_hash": g(r"\[vyr-size\] frame fnv1a=(0x[0-9a-f]{16})"),
        "workload_ok": "workload ok" in text,
        "hardfault": "cpu exception" in text or "HardFault" in text,
        "panic": "FATAL [vyr-size] panic" in text,
        "render_error": g(r"ERROR \[vyr-size\] (.*(?:failed|FAILED).*)"),
    }


def do_flash(args) -> int:
    card = CARDS[args.card]
    madctl = int(args.madctl, 16) if isinstance(args.madctl, str) else args.madctl
    rb = args.rb_swap
    rid = run_id(args.path, madctl, rb, args.card)
    feats = PATHS[args.path][0].format(card=card["feature"])
    TMP.mkdir(exist_ok=True)
    st = load_state()

    rc = 0
    with open(TMP / "board-madctl.log", "a") as logf:
        log("=" * 78, logf)
        log(f"board-madctl {rid}: MADCTL={madctl:#04x} ({flags(madctl)}) "
            f"sw_rb_swap={rb} path={args.path} card={args.card}", logf)
        log(f"  {PATHS[args.path][1]}", logf)
        built = build(feats, madctl, rb, rid, logf)
        if not built:
            return 2
        elf, sha = built
        text = run_once(elf, TMP / f"board-madctl-{rid}.log",
                        DEADLINE_S[args.path], logf)
        r = parse(text)
        r.update({"elf": elf, "elf_sha256": sha, "features": feats})

        # The image that RAN must be the image that was asked for. A stale
        # rebuild would give a real reading of the wrong configuration, which
        # is worse than no reading.
        want = f"{madctl:#04x}"
        got = r["madctl_written"] or r["madctl_in_alert"]
        r["madctl_verified"] = (got == want)
        if not r["madctl_verified"]:
            log(f"  *** THE TARGET REPORTS MADCTL={got}, NOT {want} — the image "
                f"is not the one this run asked for. DO NOT READ THE PANEL. ***",
                logf)
            rc = rc or 9
        if r["card_hash"] != card["hash"]:
            log(f"  *** CARD BODY HASH MOVED: {r['card_hash']} != {card['hash']} "
                f"— MADCTL changes how the panel SCANS what it is given, never "
                f"what the renderer produces, so this is a finding ***", logf)
            rc = rc or 6
        if r["frame_hash"] not in (None, REFERENCE_FRAME_HASH):
            log(f"  *** MEASUREMENT FRAME HASH MOVED: {r['frame_hash']} != "
                f"{REFERENCE_FRAME_HASH} ***", logf)
            rc = rc or 5
        if r["panic"] or r["hardfault"]:
            log(f"  *** TARGET FAULTED (panic={r['panic']} "
                f"hardfault={r['hardfault']}) ***", logf)
            rc = rc or 3
        if rb and r["rb_swap_applied"] and not r["rb_swap_correct"]:
            rc = rc or 8

        log(f"  card hash={r['card_hash']} (want {card['hash']}) "
            f"drawn_by={r['card_path_label']}", logf)
        log(f"  target reports MADCTL={got} verified={r['madctl_verified']} "
            f"RDDMADCTL raw=[{r['rddmadctl_raw']}] "
            f"sw_rb_swap={'APPLIED' if r['rb_swap_applied'] else 'not applied'}",
            logf)
        if r["fb_probes"]:
            log("  framebuffer bytes in ADDRESS order (pre-correction): "
                + " ".join(f"{k}=[{v}]" for k, v in r["fb_probes"].items()), logf)

        pred = predict(args.path, madctl, rb)
        entry = {
            "id": rid, "when": now(), "path": args.path, "card": args.card,
            "madctl": want, "madctl_flags": flags(madctl), "sw_rb_swap": bool(rb),
            "features": feats, "machine": r, "predicts": pred,
            "question": QUESTION.format(ref=card["ref_png"]),
            "observed": st.get("runs", {}).get(rid, {}).get("observed"),
        }
        st.setdefault("runs", {})[rid] = entry
        save_state(st)

        log("", logf)
        log("PREDICTS  rotation: " + pred["rotation"], logf)
        log("PREDICTS  colour:   " + pred["colour"], logf)
        log("PREDICTS  mirroring:" + pred["mirroring"], logf)
        log("", logf)
        for line in QUESTION.format(ref=card["ref_png"]).rstrip().splitlines():
            log(line, logf)
        log("", logf)
        log(f"RECORD IT WITH: python3 scripts/board-madctl.py --record {rid} "
            f"--badge1 <corner> --edge-wedge <yellow|cyan> "
            f"--text <forwards|mirrored>", logf)
    return rc


def do_record(args) -> int:
    st = load_state()
    run = st.get("runs", {}).get(args.record)
    if run is None:
        print(f"no such run: {args.record}\nknown: "
              + ", ".join(sorted(st.get("runs", {}))))
        return 1
    corner = args.badge1
    rot = {"top-left": "upright", "top-right": "90 CCW (badge 1 moved right)",
           "bottom-right": "180", "bottom-left": "90 CW (badge 1 moved down)"}.get(corner)
    run["observed"] = {
        "when": now(),
        "badge1_corner": corner,
        "rotation": rot,
        "edge_wedge": args.edge_wedge,
        "channels": ("true" if args.edge_wedge == "yellow" else "R/B swapped"),
        "text": args.text,
        "mirrored": args.text == "mirrored",
        "note": args.note,
    }
    save_state(st)
    print(json.dumps({args.record: run["observed"]}, indent=2))
    with open(TMP / "board-madctl.log", "a") as logf:
        log(f"RECORDED {args.record}: badge1={corner} => rotation {rot}; "
            f"edge wedge {args.edge_wedge} => channels "
            f"{run['observed']['channels']}; text {args.text}", logf)
    return 0


def do_plan() -> int:
    st = load_state()
    runs = st.get("runs", {})
    print("MADCTL sweep plan — decisive readings first. One flash, one "
          "question, one recorded answer.\n")
    print(f"Baseline (already read on the glass at MADCTL=0xC8): the present "
          f"path is rotated {BASELINE['present_rotation']} and its raw RGB888 "
          f"colours are {BASELINE['present_colour_raw']}. The lcd and ltdc "
          f"paths have never been read for either.\n")
    for i, (path, madctl, why) in enumerate(PLAN, 1):
        rid = run_id(path, madctl, 0, "orient")
        r = runs.get(rid)
        if r and r.get("observed"):
            o = r["observed"]
            state = (f"DONE  badge1={o['badge1_corner']} ({o['rotation']}), "
                     f"wedge={o['edge_wedge']} ({o['channels']}), "
                     f"text={o['text']}")
        elif r:
            state = "FLASHED — waiting on a human reading"
        else:
            state = "not run"
        print(f"{i}. {path:8s} MADCTL={madctl:#04x} ({flags(madctl):>10s})  [{state}]")
        print(f"     {why}")
        print(f"     python3 scripts/board-madctl.py --flash --path {path} "
              f"--madctl {madctl:#04x}")
    print("\nEvery flash sets VYR_RB_SWAP=0: MADCTL's BGR bit and the software "
          "swap cancel each other, and a panel that looks right for two wrong "
          "reasons is the one outcome this sweep must not produce.")
    return 0


# The R/B swap pass, MEASURED on this board (board-orientcard, present leg,
# 2026-07-24): a full read-modify-write over the 230,400 B RGB888 framebuffer.
# 10,743,380 cycles at 180 MHz = 59.7 ms. Quoted, not re-derived, and named
# here so the verdict below can be recomputed if it is ever re-measured.
SWAP_CYCLES = 10_743_380
SYSCLK_HZ = 180_000_000

VERDICT_RE = re.compile(
    r"present: verdict tier=(?P<tier>\w+) rect=(?P<rect>\w+) "
    r"banded_total=(?P<banded>\d+) c direct_total=(?P<direct>\d+) c "
    r"winner=(?P<winner>\w+) margin_pct=(?P<margin>\d+)")


def do_verdict(args) -> int:
    """Direct vs banded, with and without the software R/B pass charged to
    direct — the arithmetic that #30's conclusion turns on.

    The comparison the firmware prints already EXCLUDES the swap: it is a tail
    pass, not part of the timed window. So the swap is charged here, explicitly,
    because whether direct rendering wins depends entirely on whether that pass
    is necessary — which is a question about the panel, not about cycles.
    """
    path = Path(args.log or (TMP / "board-madctl-orient-present-0x00-rb0.log"))
    if not path.exists():
        print(f"no present-leg log at {path}; flash the present path first")
        return 1
    rows = list(VERDICT_RE.finditer(path.read_text()))
    if not rows:
        print(f"no verdict lines in {path}")
        return 1
    ms = lambda c: c * 1000.0 / SYSCLK_HZ  # noqa: E731
    out = [f"direct vs banded — from {path.name}",
           "",
           "Both cycle columns are MEASURED, on the real board, in a run built "
           "with VYR_RB_SWAP=0 — i.e. an image in which the correcting pass is "
           "genuinely absent, not one in which it was subtracted afterwards.",
           f"The pass itself is measured separately at {SWAP_CYCLES:,} c "
           f"({ms(SWAP_CYCLES):.1f} ms) over the {230400:,} B RGB888 "
           f"framebuffer; the '+ swap' column is those two measurements added, "
           f"which is the only part of this table that is arithmetic.", "",
           f"{'tier':7s} {'rect':8s} {'banded':>10s} {'direct':>10s} "
           f"{'no swap':>12s} {'+ swap':>12s}",
           f"{'':7s} {'':8s} {'ms':>10s} {'ms':>10s} "
           f"{'winner':>12s} {'winner':>12s}"]
    won_raw = won_swap = 0
    for m in rows:
        b, d = int(m["banded"]), int(m["direct"])
        raw = "direct" if d < b else "banded"
        sw = "direct" if d + SWAP_CYCLES < b else "banded"
        won_raw += raw == "direct"
        won_swap += sw == "direct"
        raw_s = f"{raw} {abs(b - d) * 100 // max(b, 1)}%"
        sw_s = f"{sw} {abs(b - d - SWAP_CYCLES) * 100 // max(b, 1)}%"
        out.append(f"{m['tier']:7s} {m['rect']:8s} {ms(b):10.2f} {ms(d):10.2f} "
                   f"{raw_s:>12s} {sw_s:>12s}")
    out += ["",
            f"direct wins {won_raw}/{len(rows)} cells with NO software swap; "
            f"{won_swap}/{len(rows)} with it.",
            "",
            "So the verdict is not a measurement question — it is the colour "
            "question. If MADCTL (or a BGR888 output format) removes the pass, "
            "direct rendering wins outright; if the pass is required, direct "
            "loses everywhere. Nothing in between."]
    txt = "\n".join(out) + "\n"
    (TMP / "madctl-verdict.log").write_text(txt)
    print(txt)
    return 0


def do_status() -> int:
    st = load_state()
    runs = st.get("runs", {})
    if not runs:
        print("no runs yet — start with --plan")
        return 0
    print(f"{'run':34s} {'madctl':8s} {'sw swap':8s} {'card hash':20s} observed")
    for rid in sorted(runs):
        r = runs[rid]
        o = r.get("observed")
        obs = ("—" if not o else
               f"badge1 {o['badge1_corner']} ({o['rotation']}), wedge "
               f"{o['edge_wedge']} => {o['channels']}, text {o['text']}")
        print(f"{rid:34s} {r['madctl']:8s} "
              f"{'on' if r['sw_rb_swap'] else 'off':8s} "
              f"{str(r['machine'].get('card_hash')):20s} {obs}")
    done = [r for r in runs.values() if r.get("observed")]
    print(f"\n{len(done)} of {len(runs)} flashed runs have a human reading.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__)
    ap.add_argument("--plan", action="store_true",
                    help="the recommended sweep order, with what is done")
    ap.add_argument("--status", action="store_true",
                    help="every run and its recorded reading")
    ap.add_argument("--flash", action="store_true",
                    help="build + flash + run ONE candidate, then ask")
    ap.add_argument("--path", choices=sorted(PATHS), default="present")
    ap.add_argument("--madctl", default="0x00",
                    help="MADCTL byte, hex (default 0x00)")
    ap.add_argument("--rb-swap", type=int, choices=(0, 1), default=0,
                    help="software R/B correction: 0 = off (the default for a "
                         "sweep, so it cannot cancel MADCTL's BGR bit)")
    ap.add_argument("--card", choices=sorted(CARDS), default="orient")
    ap.add_argument("--record", metavar="RUN_ID",
                    help="attach a human reading to a flashed run")
    ap.add_argument("--badge1", choices=("top-left", "top-right",
                                         "bottom-right", "bottom-left"))
    ap.add_argument("--edge-wedge", choices=("yellow", "cyan", "other"))
    ap.add_argument("--text", choices=("forwards", "mirrored"),
                    default="forwards")
    ap.add_argument("--note", default=None)
    ap.add_argument("--verdict", action="store_true",
                    help="direct vs banded from a present-leg log, with and "
                         "without the software R/B pass charged to direct")
    ap.add_argument("--log", default=None, help="which present-leg log to read")
    args = ap.parse_args()

    if args.verdict:
        return do_verdict(args)
    if args.record:
        if not (args.badge1 and args.edge_wedge):
            ap.error("--record needs --badge1 and --edge-wedge")
        return do_record(args)
    if args.flash:
        return do_flash(args)
    if args.status:
        return do_status()
    return do_plan()


if __name__ == "__main__":
    sys.exit(main())

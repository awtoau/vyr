#!/usr/bin/env python3
"""make-testcard.py — regenerate `vyr-size/assets/testcard.json`, the
panel-native (240x320) LABELLED COLOUR TEST CARD.

Why this exists at all
----------------------
The LTDC direct-render path (`--features present`) writes RGB888 straight into
the SDRAM framebuffer. LTDC's RGB888 layer is documented (RM0090 pixel-data
mapping) as reading B at the LOWEST address while vyr-core writes R,G,B — so a
correcting R/B swap pass was added, and it costs 59.7 ms against the 12.59 ms
blit it replaced. That single fact reverses the verdict for direct rendering,
and NOBODY HAS CONFIRMED IT, because the only thing ever put on the glass was a
UI mock-up where "does that look right?" is a judgement call.

This card removes the judgement. Every swatch carries the name of the colour it
is SUPPOSED to be. Read the label, look at the swatch. If the block labelled
RED is blue, the channels are swapped. It is photographable, and a photograph
is evidence.

What each element diagnoses
---------------------------
* **Primaries** RED/GREEN/BLUE — settle channel ORDER on their own.
* **Secondaries** YELLOW/CYAN/MAGENTA — settle WHICH PAIR is swapped: an R/B
  swap turns YELLOW into CYAN and CYAN into YELLOW and leaves MAGENTA alone;
  an R/G swap would leave CYAN alone instead.
* **Greyscale ramp** (16 steps, 0..255) — gamma, bit-depth truncation and
  RGB565 quantisation banding. Grey is the one colour a channel swap CANNOT
  change, so a ramp that goes coloured means something worse than reordering.
* **Per-channel ramps** (black→R, black→G, black→B, 16 steps) — a channel that
  is DEAD or TRUNCATED rather than merely reordered: a stuck channel shows a
  flat bar, a 5-bit-truncated one shows fewer than 16 distinct steps.
* **Every ramp sits on a white surround** so the 0-value block is delimited
  from the near-black background instead of vanishing into it.

The bottom 40 rows are NOT in this file: they are the build-identity strip,
composed at RUN TIME (feature set, pixel format, framebuffer address, which
presentation path drew it) because it necessarily differs per path. That split
is what lets the CARD BODY hash be identical across every path and both ISAs
while the strip still says what produced the picture. See
`vyr-size/src/testcard.rs`.

Geometry is generated, not hand-written, so "16 equal steps" is arithmetic
rather than a claim.

Usage: python3 scripts/make-testcard.py
Output: vyr-size/assets/testcard.json, log ./tmp/make-testcard.log
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "vyr-size" / "assets" / "testcard.json"
LOG = REPO / "tmp" / "make-testcard.log"

# --- the geometry contract ---------------------------------------------------
# The panel. The card is drawn at the panel's native size: no scaling, no
# resampler, so every pixel on the glass is a pixel the renderer produced.
W, H = 240, 320
# Rows [0, BODY_H) are the card body -- fixed IR, identical on every path, and
# the region whose FNV-1a hash is compared across paths and ISAs.
BODY_H = 280
# Rows [BODY_H, H) are the runtime identity strip. Must match
# vyr-size/src/testcard.rs.
BG = "#1A1A1A"      # near-black, but not black: a #000000 ramp step must read
                    # as a step and not as a hole in the card.
FG = "#FFFFFF"
SURROUND = "#FFFFFF"

_lines: list[str] = []


def log(msg: str) -> None:
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    line = f"[{stamp}] INFO  [make-testcard] {msg}"
    print(line, flush=True)
    _lines.append(line)


def node(name: str, **attrs) -> dict:
    return {"name": name, "attrs": {k: str(v) for k, v in attrs.items()}}


def frame(x, y, w, h, background) -> dict:
    return node("vy_frame", x=x, y=y, width=w, height=h, background=background)


def label(x, y, w, h, text, size, color=FG) -> dict:
    return node("vy_label", x=x, y=y, width=w, height=h, text=text,
                color=color, font_family="roboto", font_size=size)


def main() -> int:
    kids: list[dict] = []

    # --- title ---------------------------------------------------------------
    kids.append(label(6, 1, 228, 20, "VYR COLOUR TEST CARD", 16))

    # --- primaries then secondaries -----------------------------------------
    # Order is deliberate: R, G, B first (they settle the question), then the
    # secondaries (they say which pair). The NAME sits on the dark background
    # to the left -- never on the swatch -- so the label is legible whatever
    # the swatch turns out to be. The hex sits ON the swatch, in an ink chosen
    # only for legibility against that colour.
    swatches = [
        ("RED", "#FF0000", "#000000"),
        ("GREEN", "#00FF00", "#000000"),
        ("BLUE", "#0000FF", "#FFFFFF"),
        ("YELLOW", "#FFFF00", "#000000"),
        ("CYAN", "#00FFFF", "#000000"),
        ("MAGENTA", "#FF00FF", "#000000"),
    ]
    SW_Y0, SW_PITCH, SW_H = 24, 23, 21
    for i, (name, hexcol, ink) in enumerate(swatches):
        y = SW_Y0 + i * SW_PITCH
        kids.append(label(4, y + 4, 64, 15, name, 12))
        kids.append(frame(70, y, 166, SW_H, hexcol))
        kids.append(label(178, y + 4, 56, 15, hexcol.lstrip("#"), 12, ink))
    sw_end = SW_Y0 + len(swatches) * SW_PITCH
    log(f"swatch block: y {SW_Y0}..{sw_end} ({len(swatches)} rows, "
        f"pitch {SW_PITCH}, bar {SW_H} px)")

    # --- greyscale ramp ------------------------------------------------------
    STEPS = 16
    kids.append(label(4, 163, 232, 14, "GREY RAMP  0 TO 255  16 STEPS", 11))
    kids.append(frame(4, 177, 228, 24, SURROUND))
    for i in range(STEPS):
        v = round(i * 255 / (STEPS - 1))
        kids.append(frame(6 + i * 14, 179, 14, 20, f"#{v:02X}{v:02X}{v:02X}"))

    # --- per-channel ramps ---------------------------------------------------
    # HALF the grey ramp's step count, and the reason is the MCU: every IR node
    # costs a serde_json map allocation on a 120 KiB arena, and the card has to
    # fit alongside the band pixmap on an STM32F429 (a 92-node version panicked
    # the board with `memory allocation of 5120 bytes failed`). 8 steps is still
    # decisive for what these ramps diagnose -- a DEAD or TRUNCATED channel --
    # because 0,36,..,255 stays 8 distinct levels even after RGB565's 5-bit
    # truncation (0,4,9,13,18,22,27,31). Banding resolution is the GREY ramp's
    # job and it keeps all 16.
    CH_STEPS = 8
    kids.append(label(4, 203, 232, 14, "R / G / B RAMPS  0 TO 255", 11))
    for row, (letter, mask) in enumerate(
            [("R", (1, 0, 0)), ("G", (0, 1, 0)), ("B", (0, 0, 1))]):
        top = 218 + row * 21
        kids.append(label(4, top + 4, 12, 14, letter, 12))
        kids.append(frame(16, top, 212, 19, SURROUND))
        for i in range(CH_STEPS):
            v = round(i * 255 / (CH_STEPS - 1))
            r, g, b = (v * m for m in mask)
            kids.append(frame(18 + i * 26, top + 2, 26, 15,
                              f"#{r:02X}{g:02X}{b:02X}"))
    last = 218 + 2 * 21 + 19
    log(f"ramps end at y={last}; card body is [0,{BODY_H}) and the runtime "
        f"identity strip is [{BODY_H},{H})")
    if last > BODY_H:
        log(f"ERROR: card body overflows: {last} > {BODY_H}")
        return 1

    req = {
        "schema_version": "0.6-vyvanse",
        "w": W,
        "h": H,
        "root": {"name": "view", "attrs": {"background": BG}, "children": kids},
    }
    text = json.dumps(req, indent=2) + "\n"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    log(f"wrote {OUT} ({len(text):,} B, {len(kids)} nodes)")
    return 0


if __name__ == "__main__":
    rc = main()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write("\n".join(_lines) + "\n")
    raise SystemExit(rc)

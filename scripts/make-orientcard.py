#!/usr/bin/env python3
"""make-orientcard.py — regenerate `vyr-size/assets/orientcard.json`, the
panel-native (240x320) CORNER AND ORIENTATION CARD.

Why this exists
---------------
Every number this project has ever measured about the STM32F429I-DISC1's panel
is in RENDERER coordinates: `render(tree, area, buf, stride)` writes row 0 first
and column 0 leftmost, and the LTDC layer geometry plus the ILI9341's MADCTL
decide where that lands on the glass. Nobody has ever stated the mapping. The
colour card (`make-testcard.py`) settled channel ORDER; it says nothing about
which physical corner is the renderer's (0,0), which way +X runs, or whether the
scan is mirrored.

This card makes a human able to answer that in one sentence. Its whole design
requirement is that ALL EIGHT possible orientations — four rotations x
mirrored/not — look different:

* **Four numbered corner badges**, 1..4 running CLOCKWISE from the renderer's
  top-left, each with the name the renderer believes it has. Which physical
  corner shows "1" picks one of the four rotations on its own.
* **All the text**. A mirrored scan-out renders every glyph backwards, and the
  1>2>3>4 sequence runs anticlockwise on the glass instead of clockwise. Those
  are two independent tells for the mirror bit, and neither depends on colour.
* **Corner 1's badge is SOLID** (white fill, black digit) while 2/3/4 are
  outlined — the single filled corner, readable at a glance before any text.
* **Two wedges**, one along the top edge growing thicker with +X and one down
  the left edge growing thicker with +Y. A wedge states a direction without a
  glyph, so it survives being photographed small, blurry or upside down.
* **Edge rulers**: the wedges carry tick marks at x = 60/120/180 and
  y = 70/140/210 with the pixel coordinate printed beside each, so a reader can
  also confirm the panel is not cropping or offsetting the frame.
* **Origin block** exactly at renderer (0,0), labelled `0,0`.

Colour is deliberately SECONDARY (X+ yellow, Y+ magenta, origin cyan): the
orientation reading must not depend on the channel order being right. It is
printed in the legend so a wrong-colour build is itself visible, not confusing.

Rows [0, BODY_H) are this file and are hashed; rows [BODY_H, H) are the runtime
build-identity strip composed by `testcard::identity_ir` (path, pixel format,
framebuffer address, whether an R/B correction was applied), which necessarily
differs per presentation path and so is outside the hash. Same split, same
mechanism and same module as the colour card — see `vyr-size/src/testcard.rs`.

Node count is load-bearing, not aesthetics: the F429's 120 KiB arena fragments
under per-frame IR churn and a 92-node card has already panicked this board
(`memory allocation of 5120 bytes failed`). This card is kept under 50 nodes and
the legs that draw it call `testcard::reclaim` first.

Geometry is arithmetic here, not a hand-written claim about equal steps.

Usage: python3 scripts/make-orientcard.py
Output: vyr-size/assets/orientcard.json, log ./tmp/make-orientcard.log
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "vyr-size" / "assets" / "orientcard.json"
LOG = REPO / "tmp" / "make-orientcard.log"

# --- the geometry contract ---------------------------------------------------
# Panel-native: no scaling anywhere, so every pixel on the glass is a pixel the
# renderer produced and a ruler tick means what it says.
W, H = 240, 320
# Rows [0, BODY_H) are hashed and identical on every path; rows [BODY_H, H) are
# the runtime identity strip. Must match vyr-size/src/testcard.rs.
BODY_H = 280

BG = "#101010"        # near-black; the white rules/wedges have to read against it
INK = "#FFFFFF"
BLACK = "#000000"
X_COL = "#FFFF00"     # +X wedge
Y_COL = "#FF00FF"     # +Y wedge (magenta is the one hue an R/B swap leaves alone)
O_COL = "#00FFFF"     # origin block

# The wedges double as the edge rules, so their segment boundaries ARE the
# ruler's coarse graduations: 4 segments of 60 px across 240, 4 of 70 down 280.
X_SEGS, Y_SEGS = 4, 4
X_SEG_W, Y_SEG_H = W // X_SEGS, BODY_H // Y_SEGS
# Thin end at the origin, thick end at the far end: 3, 9, 15, 21 px. A 7x
# thickness ratio, not the 4x an even 4/8/12/16 would give — the wedge has to
# read as a DIRECTION in a hand-held photograph of a 2.4" panel, and a shallow
# taper does not survive that.
WEDGE_MIN, WEDGE_STEP = 3, 6
# Ticks sit on the segment boundaries (not arbitrary round numbers) so the
# wedge and the ruler cannot disagree about where 60 px is.
X_TICKS = [i * X_SEG_W for i in range(1, X_SEGS)]      # 60, 120, 180
Y_TICKS = [j * Y_SEG_H for j in range(1, Y_SEGS)]      # 70, 140, 210
# Must exceed the thickest wedge segment, or the tick vanishes into the wedge
# at the far end and stops being a ruler mark.
TICK_LEN = WEDGE_MIN + WEDGE_STEP * (X_SEGS - 1) + 5   # 26

# Left gutter: the wedge (<=16) plus its tick labels. Content starts clear of it.
GUTTER = 50
BADGE = 44            # corner badge box
BADGE_M = 8           # badge inset from the panel edge on the far sides
DIGIT_PT = 30

_lines: list[str] = []


def log(msg: str) -> None:
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    line = f"[{stamp}] INFO  [make-orientcard] {msg}"
    print(line, flush=True)
    _lines.append(line)


def node(name: str, **attrs) -> dict:
    return {"name": name, "attrs": {k: str(v) for k, v in attrs.items()}}


def frame(x, y, w, h, background, **extra) -> dict:
    return node("vy_frame", x=x, y=y, width=w, height=h,
                background=background, **extra)


def label(x, y, w, h, text, size, color=INK) -> dict:
    return node("vy_label", x=x, y=y, width=w, height=h, text=text,
                color=color, font_family="roboto", font_size=size)


def main() -> int:
    kids: list[dict] = []
    boxes: list[tuple[str, int, int, int, int]] = []   # for the overlap audit

    # --- the +X wedge along the TOP edge -------------------------------------
    # Thin at x=0, thick at x=239. A wedge states "this way" with no glyph, so
    # the direction survives a blurry photograph and a reader who cannot make
    # out the labels.
    for i in range(X_SEGS):
        h = WEDGE_MIN + WEDGE_STEP * i
        kids.append(frame(i * X_SEG_W, 0, X_SEG_W, h, X_COL))
    log(f"+X wedge: {X_SEGS} segments of {X_SEG_W} px, {WEDGE_MIN}.."
        f"{WEDGE_MIN + WEDGE_STEP * (X_SEGS - 1)} px thick, spanning x 0..{W}")

    # --- the +Y wedge down the LEFT edge -------------------------------------
    for j in range(Y_SEGS):
        w = WEDGE_MIN + WEDGE_STEP * j
        kids.append(frame(0, j * Y_SEG_H, w, Y_SEG_H, Y_COL))
    log(f"+Y wedge: {Y_SEGS} segments of {Y_SEG_H} px, {WEDGE_MIN}.."
        f"{WEDGE_MIN + WEDGE_STEP * (Y_SEGS - 1)} px thick, spanning y 0..{BODY_H}")

    # --- edge rulers ---------------------------------------------------------
    # White ticks through the wedges at the segment boundaries, each with its
    # pixel coordinate printed beside it. This is the crop/offset check: if the
    # tick labelled 180 is not three-quarters of the way along the physical
    # edge, the panel is not showing the whole frame.
    for x in X_TICKS:
        kids.append(frame(x, 0, 2, TICK_LEN, INK))
        kids.append(label(x + 4, 20, 26, 12, str(x), 9))
    for y in Y_TICKS:
        kids.append(frame(0, y, TICK_LEN, 2, INK))
        kids.append(label(22, y + 3, 26, 12, str(y), 9))

    # --- the origin block ----------------------------------------------------
    # Exactly at (0,0) and hard against both edges: if any of it is missing, the
    # panel is cropping or offsetting the frame.
    kids.append(frame(0, 0, 24, 12, O_COL))
    kids.append(label(2, 0, 22, 12, "0,0", 9, BLACK))

    # --- axis captions, with a pixel coordinate at each end ------------------
    kids.append(label(22, 20, 30, 12, "y=0", 9))
    kids.append(label(22, 92, 30, 14, "Y+", 11, Y_COL))
    kids.append(label(22, BODY_H - 22, 40, 12, f"y={BODY_H - 1}", 9))
    kids.append(label(GUTTER - 2, 33, 28, 12, "x=0", 9))
    kids.append(label(100, 32, 64, 14, "X+ ->", 11, X_COL))
    kids.append(label(W - 42, 33, 40, 12, f"x={W - 1}", 9))

    # --- the four corner badges ----------------------------------------------
    # Clockwise from the renderer's top-left, so "corner 1 is at the physical
    # top right" is a complete report. Corner 1 is the only SOLID badge: the
    # asymmetric anchor, readable before any glyph is.
    left_x, right_x = GUTTER + 2, W - BADGE_M - BADGE
    top_y, bot_y = 52, 194
    corners = [
        (1, left_x, top_y, "TOP-LEFT", left_x, True),
        (2, right_x, top_y, "TOP-RIGHT", 168, False),
        (3, right_x, bot_y, "BOTTOM-RIGHT", 148, False),
        (4, left_x, bot_y, "BOTTOM-LEFT", left_x, False),
    ]
    for num, bx, by, name, nx, solid in corners:
        if solid:
            kids.append(frame(bx, by, BADGE, BADGE, INK))
            digit_ink = BLACK
        else:
            kids.append(frame(bx, by, BADGE, BADGE, BG,
                              border_width=2, border_color=INK))
            digit_ink = INK
        kids.append(label(bx + 13, by + 4, 22, BADGE, str(num), DIGIT_PT, digit_ink))
        kids.append(label(nx, by + BADGE + 2, W - nx, 14, name, 11))
        boxes.append((f"badge{num}", bx, by, BADGE, BADGE))
    log(f"corner badges: {BADGE}x{BADGE} at x {left_x}/{right_x}, "
        f"y {top_y}/{bot_y}; 1 is solid, 2-4 outlined")

    # --- the statement of the numbering direction, and the colour legend -----
    # The direction has to be STATED or "corner 1 is at the top right" is only
    # half a report. The legend makes colour a cross-check rather than a
    # dependency: the orientation is readable with every hue wrong.
    kids.append(label(GUTTER + 2, 118, W - GUTTER - 4, 18,
                      "VYR ORIENTATION CARD", 13))
    kids.append(label(GUTTER + 2, 138, W - GUTTER - 4, 12,
                      "CORNERS 1>2>3>4 CLOCKWISE", 9))
    kids.append(label(GUTTER + 2, 150, W - GUTTER - 4, 12,
                      "IN RENDERER COORDINATES", 9))
    kids.append(label(GUTTER + 2, 168, W - GUTTER - 4, 12,
                      "X+ YELLOW   Y+ MAGENTA", 9))
    kids.append(label(GUTTER + 2, 180, W - GUTTER - 4, 12,
                      "0,0 CYAN AT THE ORIGIN", 9))

    # --- audit ---------------------------------------------------------------
    for (n1, x1, y1, w1, h1) in boxes:
        if x1 < 0 or y1 < 0 or x1 + w1 > W or y1 + h1 > BODY_H:
            log(f"ERROR: {n1} leaves the card body")
            return 1
    lowest = max(int(k["attrs"]["y"]) + int(k["attrs"]["height"]) for k in kids)
    if lowest > BODY_H:
        log(f"ERROR: card body overflows: {lowest} > {BODY_H}")
        return 1
    log(f"lowest element bottom y={lowest}; card body is [0,{BODY_H}) and the "
        f"runtime identity strip is [{BODY_H},{H})")

    req = {
        "schema_version": "0.6-vyvanse",
        "w": W,
        "h": H,
        "root": {"name": "view", "attrs": {"background": BG}, "children": kids},
    }
    text = json.dumps(req, indent=2) + "\n"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    log(f"wrote {OUT} ({len(text):,} B, {len(kids)} nodes) — the 120 KiB arena "
        f"panicked at 92 nodes, so the node count is a budget, not a detail")
    return 0


if __name__ == "__main__":
    rc = main()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write("\n".join(_lines) + "\n")
    raise SystemExit(rc)

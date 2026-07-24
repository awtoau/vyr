#!/usr/bin/env python3
"""testcard-host.py — the HOST REFERENCE for the labelled colour test card.

The panel cannot be compared against memory. This produces the thing it IS
compared against: a PNG of the same card, rendered by the same renderer from
the same committed IR with the same font the M4 uses, plus the two hashes that
make "same pixels everywhere" a measurement rather than a hope.

What it does, in one run:

  1. composes tmp/testcard-host.json = the committed card body
     (vyr-size/assets/testcard.json) + a HOST identity strip, so the reference
     image is self-describing exactly as the panel renders are;
  2. renders it with `vyr-cli render` at Quality::Exact, with VYR_FONTS pointed
     at a directory holding the DEVICE font (vyr-size/assets/roboto-ascii.ttf,
     installed as roboto.ttf) -- the same bytes the board rasterizes, which is
     what makes the comparison legitimate;
  3. re-folds FNV-1a 64 over rows 0..280 of the produced PNG and checks it
     against the hash vyr-size's own host leg reports -- i.e. the PNG the human
     will look at is proven to be the hashed card, not a lookalike;
  4. runs the vyr-size host x86-64 leg and the EMULATED M4 leg
     (qemu-system-arm, netduinoplus2) with --features run-qemu,testcard and
     compares their card hashes: the cross-ISA half of the claim.

No board is touched and no lock is taken -- nothing here goes near the probe.

`--card orient` does all of the above for the CORNER AND ORIENTATION CARD
(vyr-size/assets/orientcard.json, `--features run-qemu,orientcard`) instead of
the colour card. Same mechanism, same split, different asset — the point of the
orientation card is that the reference PNG is what the human holds next to the
panel, so it must be provably the same pixels the firmware folded.

Usage:  python3 scripts/testcard-host.py [--card colour|orient] [--no-qemu]
Output: tmp/<card>card-host.png (the reference image), tmp/<card>card-host.json,
        tmp/<card>card-host-result.json; log tmp/<card>card-host.log.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
DEVICE_FONT = REPO / "vyr-size" / "assets" / "roboto-ascii.ttf"
FONT_DIR = TMP / "testcard-fonts"

# The two cards this fixture can produce a reference for. One mechanism, two
# assets: the cargo feature selects which `testcard::CARD_IR` compiles in, and
# everything downstream (hash, PNG, identity strip) is unchanged.
CARDS = {
    "colour": {
        "asset": "testcard.json",
        "features": "run-qemu,testcard",
        "stem": "testcard-host",
        # Probe points quoted back for a machine-readable sanity check; for the
        # colour card these are the RED/GREEN/BLUE swatch centres.
        "probes": {"RED(153,34)": (153, 34), "GREEN(153,57)": (153, 57),
                   "BLUE(153,80)": (153, 80)},
        "ident": ("PATH host: vyr-cli render to PNG",
                  "FMT RGB888 - REFERENCE IMAGE",
                  "x86_64 - what the panel should show"),
    },
    "orient": {
        "asset": "orientcard.json",
        "features": "run-qemu,orientcard",
        "stem": "orientcard-host",
        # Geometry from scripts/make-orientcard.py: the origin block is cyan at
        # (0,0)..(24,12), the +X wedge is yellow along the top edge, the +Y
        # wedge magenta down the left. Sampling one pixel of each proves the
        # reference image carries the colours the legend claims.
        "probes": {"ORIGIN(2,8)": (2, 8), "XWEDGE(200,4)": (200, 4),
                   "YWEDGE(2,240)": (2, 240)},
        "ident": ("PATH host: vyr-cli render to PNG",
                  "FMT RGB888 - REFERENCE IMAGE",
                  "x86_64 - what the panel should show"),
    },
}

# Must match vyr-size/src/testcard.rs, scripts/make-testcard.py and
# scripts/make-orientcard.py — all three cards share the body/strip split.
BODY_H = 280
W, H = 240, 320

# Rebound per --card in main(); defaults keep the module importable and give the
# log file a home if main() dies before it gets that far.
COMPOSED = TMP / "testcard-host.json"
PNG = TMP / "testcard-host.png"
LOG = TMP / "testcard-host.log"
RESULT = TMP / "testcard-host-result.json"

FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
MASK64 = (1 << 64) - 1

# qemu wall-clock guard (a Python-side poll, never a shell timeout / sleep):
# the emulated leg runs the same ~13 s workload after the card, and icount
# shift=0 makes that a few minutes at worst on a loaded host.
QEMU_DEADLINE_S = 900
HOST_DEADLINE_S = 300

_log_lines: list[str] = []


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    _log_lines.append(line)


def fnv1a(data: bytes, h: int = FNV_OFFSET) -> int:
    for b in data:
        h = ((h ^ b) * FNV_PRIME) & MASK64
    return h


def label(x, y, w, h, text, size, color="#FFFFFF") -> dict:
    return {"name": "vy_label", "attrs": {
        "x": str(x), "y": str(y), "width": str(w), "height": str(h),
        "text": text, "color": color,
        "font_family": "roboto", "font_size": str(size)}}


def compose(spec: dict) -> dict:
    """Card body + a host identity strip, laid out exactly as
    `testcard::identity_ir` lays out the device one (y0+1 / +14 / +27, 11 px)."""
    req = json.loads((REPO / "vyr-size" / "assets" / spec["asset"]).read_text())
    kids = req["root"]["children"]
    # The strip's own black backdrop: on device the strip is a SEPARATE render
    # whose root background paints it, so the reference must paint it too.
    kids.append({"name": "vy_frame", "attrs": {
        "x": "0", "y": str(BODY_H), "width": str(W), "height": str(H - BODY_H),
        "background": "#000000"}})
    for i, line in enumerate(spec["ident"]):
        kids.append(label(4, BODY_H + 1 + i * 13, 232, 13, line, 11))
    return req


def png_body_hash(path: Path, probes: dict) -> tuple[int, dict]:
    """FNV-1a over rows 0..BODY_H of the PNG as RGB888, plus the probe pixels
    the firmware also reports. Same byte order the renderer emitted, so it is
    directly comparable with the firmware's fold.

    This is the step that makes the reference image EVIDENCE: the human compares
    the panel against this PNG, so the PNG has to be provably the same card the
    firmware hashed and not a lookalike rendered from a stale asset."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    if im.size != (W, H):
        raise SystemExit(f"PNG is {im.size}, expected {(W, H)}")
        # (unreachable in a healthy run; a size mismatch means the IR changed)
    px = im.load()
    h = FNV_OFFSET
    for y in range(BODY_H):
        row = bytearray()
        for x in range(W):
            row += bytes(px[x, y])
        h = fnv1a(bytes(row), h)
    return h, {k: list(px[x, y]) for k, (x, y) in probes.items()}


def run(cmd, env=None, deadline=None, cwd=REPO):
    log("$ " + " ".join(str(c) for c in cmd))
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       env=env, timeout=deadline, check=False)
    return p


def card_hash_from(text: str) -> str | None:
    m = re.search(r"testcard: path=(?P<p>[^ ]+(?: [^ ]+)*?) body=\d+x\d+ "
                  r"fnv1a=(0x[0-9a-f]{16})", text)
    return m.group(2) if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", choices=sorted(CARDS), default="colour",
                    help="which card to produce the reference for")
    ap.add_argument("--no-qemu", action="store_true",
                    help="skip the emulated-M4 leg (host + PNG only)")
    args = ap.parse_args()
    spec = CARDS[args.card]
    stem = spec["stem"]
    global COMPOSED, PNG, LOG, RESULT
    COMPOSED = TMP / f"{stem}.json"
    PNG = TMP / f"{stem}.png"
    LOG = TMP / f"{stem}.log"
    RESULT = TMP / f"{stem}-result.json"
    TMP.mkdir(exist_ok=True)
    out: dict = {"when": datetime.datetime.now().astimezone().isoformat(
        timespec="seconds"), "card": args.card, "asset": spec["asset"],
        "features": spec["features"]}
    rc = 0

    # --- 1. the font the DEVICE uses, under the name the IR asks for ---------
    FONT_DIR.mkdir(exist_ok=True)
    shutil.copyfile(DEVICE_FONT, FONT_DIR / "roboto.ttf")
    log(f"fonts: {DEVICE_FONT} -> {FONT_DIR / 'roboto.ttf'} "
        f"({DEVICE_FONT.stat().st_size:,} B) — the M4's own subset face, so the "
        f"reference PNG rasterizes the same glyphs the board does")

    # --- 2. compose + render -------------------------------------------------
    COMPOSED.write_text(json.dumps(compose(spec), indent=2) + "\n")
    log(f"composed {COMPOSED}")
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["VYR_FONTS"] = str(FONT_DIR)
    p = run(["cargo", "run", "--release", "-p", "vyr-cli", "--",
             "render", str(COMPOSED), str(PNG)], env=env, deadline=HOST_DEADLINE_S)
    for line in (p.stdout + p.stderr).splitlines():
        if "[vyr-cli]" in line or "ERROR" in line:
            log("  " + line)
    if p.returncode != 0:
        log(f"*** vyr-cli render FAILED rc={p.returncode} ***")
        return 2
    log(f"reference PNG -> {PNG}")
    out["png"] = str(PNG)

    png_hash, probes = png_body_hash(PNG, spec["probes"])
    out["png_body_fnv1a"] = f"{png_hash:#018x}"
    out["png_probe_pixels"] = probes
    log(f"PNG rows 0..{BODY_H} fnv1a={png_hash:#018x}")
    log(f"PNG probe pixels {probes} (RGB as the renderer emitted it)")

    # --- 3. the vyr-size host leg -------------------------------------------
    feats = ["--no-default-features", "--features", spec["features"]]
    noincr = dict(env)
    noincr["CARGO_INCREMENTAL"] = "0"
    p = run(["cargo", "build", "--release", "-p", "vyr-size", *feats],
            env=noincr, deadline=HOST_DEADLINE_S)
    if p.returncode:
        log(p.stderr[-3000:])
        return 2
    p = run([str(REPO / "target" / "release" / "vyr-size")], env=noincr,
            deadline=HOST_DEADLINE_S)
    host_txt = p.stdout + p.stderr
    (TMP / f"{stem}-run.log").write_text(host_txt)
    host_hash = card_hash_from(host_txt)
    host_frame = re.search(r"frame fnv1a=(0x[0-9a-f]{16})", host_txt)
    out["host_card_hash"] = host_hash
    out["host_workload_frame_hash"] = host_frame.group(1) if host_frame else None
    log(f"vyr-size host x86-64 card hash = {host_hash}")

    out["png_matches_host_hash"] = (host_hash == f"{png_hash:#018x}")
    if not out["png_matches_host_hash"]:
        log(f"*** PNG BODY HASH {png_hash:#018x} != host leg {host_hash} — the "
            f"reference image is NOT the hashed card ***")
        rc = rc or 4

    # --- 4. the emulated M4 --------------------------------------------------
    if not args.no_qemu:
        if shutil.which("qemu-system-arm") is None:
            log("qemu-system-arm not on PATH — skipping the emulated-M4 leg")
        else:
            p = run(["cargo", "build", "-p", "vyr-size", "--target",
                     "thumbv7em-none-eabihf", "--profile", "release-mcu", *feats],
                    env=noincr, deadline=HOST_DEADLINE_S)
            if p.returncode:
                log(p.stderr[-3000:])
                return 2
            elf = REPO / "target/thumbv7em-none-eabihf/release-mcu/vyr-size"
            try:
                p = run(["qemu-system-arm", "-machine", "netduinoplus2",
                         "-nographic", "-semihosting-config",
                         "enable=on,target=native", "-icount", "shift=0,sleep=off",
                         "-kernel", str(elf)], deadline=QEMU_DEADLINE_S)
                guest = p.stdout + p.stderr
            except subprocess.TimeoutExpired as e:
                guest = (e.stdout or "") if isinstance(e.stdout, str) else ""
                log(f"*** qemu exceeded the {QEMU_DEADLINE_S}s guard ***")
                rc = rc or 5
            (TMP / f"{stem}-qemu-run.log").write_text(guest)
            m4_hash = card_hash_from(guest)
            out["qemu_m4_card_hash"] = m4_hash
            out["cross_isa_identical"] = (m4_hash is not None
                                          and m4_hash == host_hash)
            log(f"emulated M4 card hash = {m4_hash} -> "
                + ("IDENTICAL to x86-64" if out["cross_isa_identical"]
                   else "*** MISMATCH ***"))
            if not out["cross_isa_identical"]:
                rc = rc or 6

    RESULT.write_text(json.dumps(out, indent=2) + "\n")
    log(f"wrote {RESULT}")
    log(f"OPEN THE REFERENCE:  code-insiders {PNG}")
    return rc


if __name__ == "__main__":
    code = main()
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a") as f:
        f.write("\n".join(_log_lines) + "\n")
    sys.exit(code)

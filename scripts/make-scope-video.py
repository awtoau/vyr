#!/usr/bin/env python3
"""Render a real scope-style animation with vyr and encode it to MP4/GIF.

Produces:
  - tmp/scope-render/frame-0000.png ...
  - tmp/scope-render.mp4
  - tmp/scope-render.gif

Logs progress to tmp/make-scope-video.log.
"""

from __future__ import annotations

import json
import math
import argparse
import shutil
import subprocess
import time
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
FRAMES_DIR = TMP / "scope-render"
IR_PATH = TMP / "scope-render-scene.json"
LOG_PATH = TMP / "make-scope-video.log"
OUT_MP4 = TMP / "scope-render.mp4"
OUT_GIF = TMP / "scope-render.gif"
W = 320
H = 240
FPS = 60
N_FRAMES = 600
N_SAMPLES = 1800


def stamp() -> str:
    now = time.time()
    return time.strftime("%H:%M:%S", time.gmtime(now)) + f".{int((now % 1) * 1_000_000):06d} UTC"


def log(msg: str) -> None:
    line = f"{stamp()}  INFO  [scope-video] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def build_points(frame: int) -> str:
    # Composite waveform with drift + occasional transients; clamped 0..100 so
    # range mapping stays stable for scope-style rendering.
    vals: list[str] = []
    for i in range(N_SAMPLES):
        t = (i + frame * 9) / N_SAMPLES
        y = 50.0
        y += 26.0 * math.sin(2.0 * math.pi * (t * 3.2))
        y += 12.0 * math.sin(2.0 * math.pi * (t * 17.0 + frame * 0.003))
        # Short impulses to exercise min/max decimation visibility.
        k = (i + frame * 7) % 260
        if k < 4:
            y += 28.0
        if 130 <= k < 133:
            y -= 22.0
        y = max(0.0, min(100.0, y))
        vals.append(str(int(y + 0.5)))
    return ",".join(vals)


def scene_json(points_csv: str, decimate: str) -> str:
    scene = {
        "w": W,
        "h": H,
        "root": {
            "name": "view",
            "attrs": {"background": "#0B0F14"},
            "children": [
                {
                    "name": "vy_frame",
                    "attrs": {
                        "x": "6",
                        "y": "6",
                        "width": "308",
                        "height": "228",
                        "background": "#0F1620",
                        "border_width": "1",
                        "border_color": "#2A3E55",
                        "radius": "0",
                    },
                },
                {
                    "name": "vy_chart",
                    "attrs": {
                        "x": "10",
                        "y": "10",
                        "width": "300",
                        "height": "220",
                        "mode": "scope",
                        "decimate": decimate,
                        "show_background": "0",
                        "show_grid": "1",
                        "show_frame": "1",
                        "show_markers": "0",
                        "div_count_x": "9",
                        "div_count_y": "7",
                        "line_width": "1",
                        "line_color": "#65FF9B",
                        "range_min": "0",
                        "range_max": "100",
                        "points": points_csv,
                    },
                },
            ],
        },
    }
    return json.dumps(scene, separators=(",", ":"))


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render scope-style video via vyr-cli")
    parser.add_argument(
        "--decimate",
        choices=["minmax", "none"],
        default="minmax",
        help="Scope decimation mode",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=N_FRAMES,
        help="Frame count to render",
    )
    parser.add_argument(
        "--no-encode",
        action="store_true",
        help="Render PNG frames only (skip MP4/GIF encoding)",
    )
    args = parser.parse_args()

    TMP.mkdir(parents=True, exist_ok=True)
    if FRAMES_DIR.exists():
        shutil.rmtree(FRAMES_DIR)
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_PATH.exists():
        LOG_PATH.unlink()

    cli = REPO / "target" / "release" / "vyr-cli"
    if not cli.exists():
        log("building vyr-cli (release)")
        run(["cargo", "build", "-p", "vyr-cli", "--release"])

    log(
        f"rendering {args.frames} scope frames at {W}x{H} "
        f"(decimate={args.decimate})"
    )
    t0 = time.time()
    for fidx in range(args.frames):
        IR_PATH.write_text(scene_json(build_points(fidx), args.decimate), encoding="utf-8")
        out = FRAMES_DIR / f"frame-{fidx:04}.png"
        run([str(cli), "render", str(IR_PATH), str(out)])
        if (fidx + 1) % 50 == 0:
            log(f"  rendered {fidx + 1}/{args.frames}")
    dt = time.time() - t0
    log(f"render done in {dt:.2f}s ({dt / args.frames * 1000:.2f} ms/frame)")

    if args.no_encode:
        log("done: render-only mode (no encoding)")
        return 0

    log("encoding MP4")
    run(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            str(FPS),
            "-i",
            str(FRAMES_DIR / "frame-%04d.png"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(OUT_MP4),
        ]
    )

    log("encoding GIF")
    run(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            "30",
            "-i",
            str(FRAMES_DIR / "frame-%04d.png"),
            str(OUT_GIF),
        ]
    )

    log(f"done: {OUT_MP4.relative_to(REPO)}")
    log(f"done: {OUT_GIF.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

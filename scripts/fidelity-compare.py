#!/usr/bin/env python3
"""fidelity-compare.py — the #27 evidence: render the SAME scene through
vyr Exact, vyr Draft and (optionally) LVGL, quantify the Exact-vs-Draft delta,
and emit the side-by-side plates that go in docs/quality-tiers.md.

What it produces (all under docs/quality-tiers/ + tmp/fidelity/):
  * <scene>-exact.png / <scene>-draft.png       — the two vyr tiers
  * <scene>-diff.png                            — differing pixels, heat-mapped
  * quality-tiers-panel.png                     — Exact | Draft | diff strip
  * quality-tiers-lvgl.png                      — Exact | Draft | LVGL strip
  * tmp/fidelity/fidelity.json                  — every number, machine-readable

Scenes come from the committed Rust consts, extracted verbatim (never retyped):
  DEMO_IR   vyr-core/src/demo.rs      120x120  — the historical 766/14400 figure
  FIXTURE_IR vyr-size/src/workload.rs 480x270  — the scene the M4 + LVGL
                                                 instruction counts were taken on

The LVGL image, when present, is the raw RGB888 frame dumped off the emulated
M4 by `scripts/lvgl-m4-bench/run.py --dump-frame` — real LVGL pixels from the
same bare-metal harness the instruction counts came from, not a host mock-up.

Usage:
  python3 scripts/fidelity-compare.py [--lvgl-raw tmp/fidelity/lvgl-frame.rgb888]

Logs: tmp/fidelity-compare.log
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(REPO, "tmp", "fidelity")
DOCS = os.path.join(REPO, "docs", "quality-tiers")
LOG = os.path.join(REPO, "tmp", "fidelity-compare.log")
CLI = os.path.join(REPO, "target", "release", "vyr-cli")

_logf = None


def log(msg):
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    if _logf:
        _logf.write(line + "\n")
        _logf.flush()


def extract_const(path, name):
    """Pull `pub const NAME: &str = r##"..."##;` out of a Rust source verbatim."""
    src = open(os.path.join(REPO, path)).read()
    m = re.search(rf'pub const {name}: &str = r##"(.*?)"##;', src, re.S)
    if not m:
        raise SystemExit(f"could not find {name} in {path}")
    return m.group(1)


TIERS = ("exact", "fast", "draft")
TIER_FLAG = {"exact": [], "fast": ["--fast"], "draft": ["--draft"]}
TIER_LABEL = {
    "exact": "vyr Exact — the oracle: float AA everywhere",
    "fast": "vyr Fast — integer spans + anti-aliased curves (#27)",
    "draft": "vyr Draft — integer spans, no AA anywhere",
}


def render(ir_path, out, tier):
    cmd = [CLI, "render", ir_path, out] + TIER_FLAG[tier]
    env = dict(os.environ)
    env["VYR_FONTS"] = os.path.join(REPO, "fonts")
    env["VYR_ASSETS"] = os.path.join(REPO, "vyr-core", "tests", "assets")
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=REPO)
    if r.returncode != 0:
        log(r.stdout + r.stderr)
        raise SystemExit(f"vyr-cli render failed for {ir_path} (draft={draft})")
    return out


# ---- fidelity maths ---------------------------------------------------------

def band_label(y, h):
    """Coarse vertical zone name — enough to say WHERE the differences are
    without inventing a segmentation the renderer does not expose."""
    return int(y * 8 // h)


def compare(exact_png, draft_png, regions):
    a = np.asarray(Image.open(exact_png).convert("RGB")).astype(np.int16)
    b = np.asarray(Image.open(draft_png).convert("RGB")).astype(np.int16)
    if a.shape != b.shape:
        raise SystemExit("tier renders differ in size")
    h, w, _ = a.shape
    d = np.abs(a - b)
    per_px = d.max(axis=2)
    diff_mask = per_px > 0
    n_diff = int(diff_mask.sum())
    total = h * w
    # Distribution of the error, so "5.3% of pixels differ" can be read
    # alongside "and almost all of them differ imperceptibly".
    errs = per_px[diff_mask]
    hist = {
        "1..8": int(((errs >= 1) & (errs <= 8)).sum()),
        "9..32": int(((errs >= 9) & (errs <= 32)).sum()),
        "33..64": int(((errs >= 33) & (errs <= 64)).sum()),
        "65..128": int(((errs >= 65) & (errs <= 128)).sum()),
        "129..255": int((errs >= 129).sum()),
    } if n_diff else {}
    out = {
        "w": w, "h": h,
        "pixels": total,
        "pixels_differing": n_diff,
        "pixels_differing_pct": round(100.0 * n_diff / total, 3),
        "max_channel_error": int(per_px.max()),
        "mean_channel_error_over_differing": (
            round(float(errs.mean()), 2) if n_diff else 0.0),
        "median_channel_error_over_differing": (
            int(np.median(errs)) if n_diff else 0),
        "error_histogram": hist,
    }
    # WHERE: named regions the scene author declared (widget rects from the IR),
    # so the answer is "edges of the gauge ring / glyph runs", not a guess.
    if regions:
        where = []
        for name, (x, y, rw, rh) in regions.items():
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(w, x + rw), min(h, y + rh)
            if x1 <= x0 or y1 <= y0:
                continue
            sub = diff_mask[y0:y1, x0:x1]
            n = int(sub.sum())
            if n == 0:
                continue
            where.append({
                "region": name,
                "px_differing": n,
                "pct_of_region": round(100.0 * n / sub.size, 2),
                "pct_of_all_differing": round(100.0 * n / n_diff, 1),
                "max_channel_error": int(per_px[y0:y1, x0:x1].max()),
            })
        where.sort(key=lambda r: -r["px_differing"])
        out["by_region"] = where
        covered = np.zeros_like(diff_mask)
        for _, (x, y, rw, rh) in regions.items():
            covered[max(0, y):y + rh, max(0, x):x + rw] = True
        out["differing_outside_declared_widgets"] = int(
            (diff_mask & ~covered).sum())
    return out, per_px, diff_mask


def diff_image(exact_png, per_px, path):
    """Heat-map the differing pixels over a dimmed Exact render: dark red = a
    small (AA-fringe) delta, bright yellow = a large one."""
    base = np.asarray(Image.open(exact_png).convert("RGB")).astype(np.float32)
    out = base * 0.25
    e = per_px.astype(np.float32)
    m = e > 0
    t = np.clip(e / 255.0, 0.0, 1.0) ** 0.5  # gamma so small deltas stay visible
    out[..., 0] = np.where(m, 90 + 165 * t, out[..., 0])
    out[..., 1] = np.where(m, 20 + 215 * t, out[..., 1])
    out[..., 2] = np.where(m, 20 + 40 * t, out[..., 2])
    Image.fromarray(out.clip(0, 255).astype(np.uint8)).save(path)


# ---- plates -----------------------------------------------------------------

def _font(size):
    for p in ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def strip(panels, path, scale=1, caption_h=26, gap=10, bg=(18, 18, 20),
          vertical=True):
    """Labelled images, one per ROW by default.

    #27 Task C: these sheets used to lay panels out side by side, which on a
    480x270 scene meant three ~1/3-width thumbnails that nobody can compare a
    2-px AA fringe in. Stacking vertically gives every render the full sheet
    width at 1:1, which is the whole point of a fidelity plate. `vertical=False`
    is kept for the zoomed crops, where the panels are small and side-by-side
    is genuinely easier to read.
    """
    imgs = [Image.open(p).convert("RGB") if isinstance(p, str) else p
            for p, _ in panels]
    if scale != 1:
        imgs = [im.resize((im.width * scale, im.height * scale), Image.NEAREST)
                for im in imgs]
    f = _font(15)
    dr_probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    if vertical:
        # The sheet must be wide enough for the CAPTIONS too — a clipped label
        # ("...anti-aliased curv") is exactly the kind of small wrongness that
        # makes a plate untrustworthy.
        cap_w = max(int(dr_probe.textlength(lbl, font=f)) for _, lbl in panels)
        w = max(max(im.width for im in imgs), cap_w) + gap * 2
        h = sum(im.height + caption_h + gap for im in imgs) + gap
        sheet = Image.new("RGB", (w, h), bg)
        dr = ImageDraw.Draw(sheet)
        y = gap
        for im, (_, label) in zip(imgs, panels):
            dr.text((gap, y + 4), label, font=f, fill=(235, 239, 244))
            sheet.paste(im, (gap, y + caption_h))
            y += caption_h + im.height + gap
    else:
        w = sum(im.width for im in imgs) + gap * (len(imgs) + 1)
        h = max(im.height for im in imgs) + caption_h + gap * 2
        sheet = Image.new("RGB", (w, h), bg)
        dr = ImageDraw.Draw(sheet)
        x = gap
        for im, (_, label) in zip(imgs, panels):
            sheet.paste(im, (x, gap + caption_h))
            dr.text((x, gap + 4), label, font=f, fill=(235, 239, 244))
            x += im.width + gap
    sheet.save(path)
    return path


# ---- the #27 quality metric -------------------------------------------------

def edge_stats(png, box):
    """Blend pixels and distinct values over `box` — the CORRECT #27 metric.

    Counting distinct COLOURS over a region is the trap the issue's first
    reading fell into: the count rises with how much CONTENT is in the region,
    not with how well its edges are anti-aliased, so a renderer that draws tick
    marks and numeric labels where another draws a plain ring wins the count
    while being no smoother. A blend pixel is one whose value is neither of the
    two dominant (flat) values present — something only edge blending creates.
    """
    arr = np.asarray(Image.open(png).convert("RGB"))
    x0, y0, x1, y1 = box
    sub = arr[y0:y1, x0:x1]
    n = sub.shape[0] * sub.shape[1]
    keys = (sub.astype(np.uint32) @ np.array([65536, 256, 1], np.uint32)).ravel()
    vals, counts = np.unique(keys, return_counts=True)
    order = np.argsort(-counts)
    flat = int(counts[order[:2]].sum()) if len(order) >= 2 else int(counts.max())
    return {
        "region_px": int(n),
        "blend_px": int(n - flat),
        "blend_pct": round(100.0 * (n - flat) / n, 2),
        "distinct_colours": int(len(vals)),
    }


def edge_scanline(png, y, x0, x1):
    """The raw values along one scanline through a curve edge — the smallest
    piece of evidence that settles "does this renderer blend or not"."""
    arr = np.asarray(Image.open(png).convert("RGB"))
    return [int(v) for v in arr[y, x0:x1, 1]]


def lvgl_png(raw_path, out_png, w=480, h=270):
    """Convert the semihosting-dumped LVGL frame to PNG.

    LVGL's LV_COLOR_FORMAT_RGB888 stores BLUE first in memory (it is ARGB8888
    minus the alpha byte, little-endian). We do not assume: the scene's
    background is #22262B, so the corner pixel decides the byte order and the
    decision is logged."""
    raw = open(raw_path, "rb").read()
    want = w * h * 3
    if len(raw) != want:
        raise SystemExit(f"{raw_path}: {len(raw)} B, expected {want}")
    a = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3)
    px = tuple(int(v) for v in a[2, 2])
    bgr = abs(px[0] - 0x2B) + abs(px[2] - 0x22) < abs(px[0] - 0x22) + abs(px[2] - 0x2B)
    log(f"LVGL raw corner pixel bytes {px} → byte order "
        f"{'BGR (LVGL native RGB888)' if bgr else 'RGB'}")
    if bgr:
        a = a[..., ::-1]
    Image.fromarray(a.copy()).save(out_png)
    return out_png, ("BGR" if bgr else "RGB")


# ---- the widget rects, straight out of the IR -------------------------------

def regions_from_ir(ir_json):
    req = json.loads(ir_json)
    out = {}
    seen = {}
    for ch in req["root"].get("children", []):
        at = ch.get("attrs", {})
        if not all(k in at for k in ("x", "y", "width", "height")):
            continue
        name = ch["name"]
        seen[name] = seen.get(name, 0) + 1
        key = name if seen[name] == 1 else f"{name}#{seen[name]}"
        # Widen by 2px: AA fringes land just outside the declared rect.
        out[key] = (int(at["x"]) - 2, int(at["y"]) - 2,
                    int(at["width"]) + 4, int(at["height"]) + 4)
    return out


def main():
    global _logf
    ap = argparse.ArgumentParser()
    ap.add_argument("--lvgl-raw",
                    default=os.path.join(TMP, "lvgl-frame.rgb888"),
                    help="raw 480x270 RGB888 frame from lvgl-m4-bench --dump-frame")
    args = ap.parse_args()

    os.makedirs(TMP, exist_ok=True)
    os.makedirs(DOCS, exist_ok=True)
    _logf = open(LOG, "a")
    log("=" * 60)
    log("fidelity-compare: Exact vs Draft (+ LVGL when the dump is present)")

    if not os.path.exists(CLI):
        raise SystemExit(f"{CLI} missing — cargo build --release -p vyr-cli")

    scenes = {
        "demo": ("vyr-core/src/demo.rs", "DEMO_IR"),
        "panel": ("vyr-size/src/workload.rs", "FIXTURE_IR"),
    }
    results = {}
    for scene, (path, const) in scenes.items():
        ir = extract_const(path, const)
        ir_path = os.path.join(TMP, f"{scene}.json")
        open(ir_path, "w").write(ir)
        pngs = {t: render(ir_path, os.path.join(DOCS, f"{scene}-{t}.png"), t)
                for t in TIERS}
        ex, fa, dr = pngs["exact"], pngs["fast"], pngs["draft"]
        stats, per_px, _ = compare(ex, dr, regions_from_ir(ir))
        stats["scene"] = scene
        stats["ir_source"] = f"{path}::{const}"
        diff_image(ex, per_px, os.path.join(DOCS, f"{scene}-diff.png"))
        results[scene] = stats
        log(f"{scene} ({stats['w']}x{stats['h']}): "
            f"{stats['pixels_differing']}/{stats['pixels']} px differ "
            f"({stats['pixels_differing_pct']}%), max channel error "
            f"{stats['max_channel_error']}/255, median over differing "
            f"{stats['median_channel_error_over_differing']}")
        for r in stats.get("by_region", [])[:6]:
            log(f"    {r['region']:<16} {r['px_differing']:>6} px "
                f"({r['pct_of_all_differing']:>5.1f}% of all diffs, "
                f"max Δ{r['max_channel_error']})")
        log(f"    outside declared widget rects: "
            f"{stats.get('differing_outside_declared_widgets')} px")
        # #27: Fast against BOTH neighbours — same renderer, same scene, which
        # is the only comparison that is sound without a content audit.
        fe, _, _ = compare(ex, fa, None)
        fd, _, _ = compare(fa, dr, None)
        stats["fast_vs_exact"] = {k: fe[k] for k in
                                  ("pixels_differing", "pixels_differing_pct",
                                   "max_channel_error")}
        stats["fast_vs_draft"] = {k: fd[k] for k in
                                  ("pixels_differing", "pixels_differing_pct",
                                   "max_channel_error")}
        log(f"    Fast vs Exact: {fe['pixels_differing']} px differ "
            f"({fe['pixels_differing_pct']}%), max Δ{fe['max_channel_error']}")
        log(f"    Fast vs Draft: {fd['pixels_differing']} px differ "
            f"({fd['pixels_differing_pct']}%), max Δ{fd['max_channel_error']}")
        sc = 3 if stats["w"] <= 160 else 1
        strip([(ex, TIER_LABEL["exact"]),
               (fa, TIER_LABEL["fast"]),
               (dr, TIER_LABEL["draft"]),
               (os.path.join(DOCS, f"{scene}-diff.png"),
                f"Exact vs Draft: {stats['pixels_differing_pct']}% of pixels differ")],
              os.path.join(DOCS, f"{scene}-tiers.png"), scale=sc)

    # ---- the three-way plate, if a real LVGL frame is available -------------
    lvgl_ok = os.path.exists(args.lvgl_raw)
    if lvgl_ok:
        png, order = lvgl_png(args.lvgl_raw, os.path.join(DOCS, "panel-lvgl.png"))
        results["lvgl"] = {
            "source": os.path.relpath(args.lvgl_raw, REPO),
            "byte_order": order,
            "note": "real LVGL 9.6.0-dev pixels, dumped off the emulated M4 by "
                    "scripts/lvgl-m4-bench/run.py --dump-frame (semihosting "
                    "SYS_OPEN/WRITE). Same 480x270 scene, LVGL's own widgets "
                    "and Montserrat font — a SYSTEM comparison, not pixel-identical "
                    "input (see scripts/lvgl-m4-bench/compare.md).",
        }
        strip([(os.path.join(DOCS, "panel-exact.png"), TIER_LABEL["exact"]),
               (os.path.join(DOCS, "panel-fast.png"), TIER_LABEL["fast"]),
               (os.path.join(DOCS, "panel-draft.png"), TIER_LABEL["draft"]),
               (png, "LVGL 9.6.0-dev — its own widgets/theme/font (not "
                     "content-identical)")],
              os.path.join(DOCS, "three-way.png"))
        log(f"four-row plate written: {os.path.join(DOCS, 'three-way.png')}")
        # The claim "LVGL anti-aliases, Draft does not" is only worth making if
        # you can see it. The toggle knob is a circle all three engines draw.
        crop = (176, 190, 244, 228)
        strip([(Image.open(os.path.join(DOCS, "panel-exact.png")).crop(crop),
                "vyr Exact"),
               (Image.open(os.path.join(DOCS, "panel-fast.png")).crop(crop),
                "vyr Fast"),
               (Image.open(os.path.join(DOCS, "panel-draft.png")).crop(crop),
                "vyr Draft"),
               (Image.open(png).crop(crop), "LVGL")],
              os.path.join(DOCS, "zoom-toggle.png"), scale=6, vertical=False)
        log(f"zoom plate written: {os.path.join(DOCS, 'zoom-toggle.png')}")
        # The gauge ring — the curve #27 is entirely about. Zoomed on the LEFT
        # edge of the ring, where a blend step is either present or it is not.
        gcrop = (24, 118, 60, 146)
        strip([(Image.open(os.path.join(DOCS, "panel-exact.png")).crop(gcrop), "vyr Exact"),
               (Image.open(os.path.join(DOCS, "panel-fast.png")).crop(gcrop), "vyr Fast"),
               (Image.open(os.path.join(DOCS, "panel-draft.png")).crop(gcrop), "vyr Draft"),
               (Image.open(png).crop(gcrop), "LVGL")],
              os.path.join(DOCS, "zoom-gauge.png"), scale=8, vertical=False)
        log(f"gauge zoom written: {os.path.join(DOCS, 'zoom-gauge.png')}")
        # Where does LVGL SIT between the two tiers? Anti-aliasing manufactures
        # intermediate colours; a no-AA integer path cannot. Distinct-colour
        # count over the same scene is a crude but honest PROXY — it does not
        # measure geometric accuracy, only whether edge blending happened.
        # #27 edge quality, over the GAUGE-RING region (x 24-134, y 76-186 —
        # the fixture's vy_gauge rect, 12,100 px), which is where the curve is.
        # BLEND-pixel count, not colour count: see edge_stats() for why the
        # latter is a trap. The vyr rows are directly comparable to each other;
        # the LVGL row is context only until the harness is content-audited.
        box = (24, 76, 134, 186)
        edges = {}
        for label, p in (("vyr Exact", os.path.join(DOCS, "panel-exact.png")),
                         ("vyr Fast", os.path.join(DOCS, "panel-fast.png")),
                         ("vyr Draft", os.path.join(DOCS, "panel-draft.png")),
                         ("LVGL", png)):
            e = edge_stats(p, box)
            e["ring_edge_scanline_y131_x24_36"] = edge_scanline(p, 131, 24, 36)
            edges[label] = e
            log(f"#27 gauge region {label:<10} blend {e['blend_px']:>5} px "
                f"({e['blend_pct']:>5.2f}%)  distinct {e['distinct_colours']:>4}  "
                f"scanline y=131 {e['ring_edge_scanline_y131_x24_36']}")
        results["edge_quality_gauge_region"] = {
            "box_xyxy": box,
            "note": "blend_px = pixels whose value is neither of the two "
                    "dominant flat values — the thing anti-aliasing creates. "
                    "distinct_colours is reported alongside but is NOT the "
                    "metric: it rises with content, not with edge quality.",
            "tiers": edges,
        }
    else:
        log(f"NO LVGL FRAME at {args.lvgl_raw} — shipping the Exact-vs-Draft "
            "half only. Run: python3 scripts/lvgl-m4-bench/run.py "
            f"--dump-frame {args.lvgl_raw}")
        results["lvgl"] = {"available": False}

    out = os.path.join(TMP, "fidelity.json")
    with open(out, "w") as f:
        json.dump({
            "when": datetime.datetime.now().astimezone().isoformat(
                timespec="seconds"),
            "git_commit": subprocess.run(
                ["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True).stdout.strip(),
            "scenes": results,
        }, f, indent=2)
    log(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

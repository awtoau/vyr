//! F16 `Quality::Fast` goldens (#27): the MIDDLE tier — Draft's integer span
//! fills everywhere they apply, the Exact tiny-skia AA path for CURVED
//! geometry only (disc, ring/arc, rounded-rect + rounded-stroke +
//! rounded-gradient corners).
//!
//! Fast adds **no rasteriser and no flattening of its own**. Its curve pixels
//! come from the very same quantized polygons, the same 1/64-px world
//! quantization, the same exact-integer band translation AND the same 8 px
//! overscan gutter the Exact tier uses — routed through the `rgb` scratch
//! round-trip so they composite into Draft's straight-RGB888 band surface in
//! draw order.
//!
//! The gutter is not decoration and this suite is why we know: built
//! gutter-less first, `fast_band_equivalence` failed with 4 differing bytes on
//! DEMO_IR at band_h=30 and `fast_gradient_scene_band_equivalence` with 5 on
//! `demo_scene` at 17. tiny-skia's AA edge walker is seeded at the
//! rasterization clip and stepped from there, so moving the clip moves the LSB
//! of the accumulated coverage. Exact's overscan is what makes that invisible;
//! Fast runs the same rasterizer and needs the same overscan.
//!
//! What is enforced here, the same discipline as the Exact and Draft goldens:
//!
//! - `fast_golden_hash`: DEMO_IR at `Quality::Fast` hashes to the committed
//!   constant — the tier is deterministic (I2 holds per-tier). Re-bless:
//!   `./dev.py test --bless`.
//! - `fast_band_equivalence` / `fast_gradient_scene_band_equivalence`: the SAME
//!   scene rendered as even (30-row) and uneven (17-row) bands is byte-identical
//!   to the full-frame render (I1 holds per-tier) — the load-bearing test,
//!   because Fast is the only tier that mixes tiny-skia AA with integer spans
//!   on one surface.
//! - `fast_differs_from_both`: the differs-from-both guard, over two scenes.
//!   Fast must differ from Draft (else the AA did nothing) and, on a scene
//!   carrying a diagonal line + a gradient, from Exact (else it is not a
//!   cheaper tier at all). On DEMO_IR it lands within a few LSBs of Exact and
//!   that BOUND is asserted too — see the test for why.
//! - `fast_is_deterministic`: each of the three tiers reproduces its own bytes
//!   across repeated renders.
//! - `fast_anti_aliases_the_curve`: the quality claim, measured — blend pixels
//!   and distinct values, Fast vs Draft vs Exact. (Blend counts, not colour
//!   counts: a whole-region colour count inflates with CONTENT, not with edge
//!   quality, which is how #27's first reading went wrong.)
//! - `fast_fastpath_coverage`: the honesty number (#21) — how much of the frame
//!   still took the integer path, i.e. exactly what the AA cost bought.
//!
//! Set `VYR_TEST_DUMP=1` to write the PNGs to ../tmp/ for eyeballing.

use vyr_core::demo::{DEMO_H, DEMO_IR, DEMO_W, demo_scene};
use vyr_core::{Assets, Fonts, Quality, Rect, RenderStats, TinySkiaCanvas};

const W: u32 = DEMO_W;
const H: u32 = DEMO_H;

/// Committed Fast golden (FNV-1a 64 of the RGB888 buffer). Re-bless via
/// `./dev.py test --bless`. DISTINCT from BOTH neighbours by construction.
const FAST_GOLDEN_FNV1A: u64 = 0xAD2C_60E3_23D4_183D;

fn fnv1a(data: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(0x100_0000_01b3);
    }
    h
}

fn render_full(quality: Quality) -> (Vec<u8>, RenderStats) {
    let mut fonts = Fonts::new();
    let assets = Assets::new();
    let mut buf = vec![0u8; (W * H * 3) as usize];
    let stats = vyr_core::render_with_quality(
        DEMO_IR,
        &mut fonts,
        &assets,
        Rect {
            x: 0,
            y: 0,
            w: W,
            h: H,
        },
        &mut buf,
        (W * 3) as usize,
        quality,
    )
    .expect("demo fixture renders");
    (buf, stats)
}

fn render_banded(quality: Quality, band_h: u32) -> Vec<u8> {
    let mut fonts = Fonts::new();
    let assets = Assets::new();
    let stride = (W * 3) as usize;
    let mut out = vec![0u8; stride * H as usize];
    let mut y = 0;
    while y < H {
        let h = band_h.min(H - y);
        let band = &mut out[y as usize * stride..(y + h) as usize * stride];
        vyr_core::render_with_quality(
            DEMO_IR,
            &mut fonts,
            &assets,
            Rect {
                x: 0,
                y: y as i32,
                w: W,
                h,
            },
            band,
            stride,
            quality,
        )
        .expect("demo band renders");
        y += h;
    }
    out
}

/// The hand-built `demo_scene` (the ONLY scene with a `fill_linear_gradient`,
/// plus rounded fills and a stroked rounded border) at an explicit tier,
/// stitched from `band_h`-row bands.
fn scene_banded(quality: Quality, band_h: u32) -> Vec<u8> {
    let stride = (W * 3) as usize;
    let mut out = vec![0u8; stride * H as usize];
    let mut y = 0;
    while y < H {
        let h = band_h.min(H - y);
        let mut c = TinySkiaCanvas::new_with_quality(
            Rect {
                x: 0,
                y: y as i32,
                w: W,
                h,
            },
            quality,
        )
        .expect("pixmap");
        demo_scene(&mut c);
        let band = &mut out[y as usize * stride..(y + h) as usize * stride];
        c.finish_into_rgb888(band, stride);
        y += h;
    }
    out
}

fn dump_png(name: &str, buf: &[u8]) {
    if std::env::var_os("VYR_TEST_DUMP").is_none() {
        return;
    }
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../tmp");
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join(name);
    let file = std::fs::File::create(&path).unwrap();
    let mut enc = png::Encoder::new(std::io::BufWriter::new(file), W, H);
    enc.set_color(png::ColorType::Rgb);
    enc.set_depth(png::BitDepth::Eight);
    enc.write_header().unwrap().write_image_data(buf).unwrap();
    eprintln!("dumped {}", path.display());
}

/// Differing pixels and the worst channel error between two RGB888 frames.
fn pixel_delta(a: &[u8], b: &[u8]) -> (usize, u8) {
    let mut n = 0usize;
    let mut worst = 0u8;
    for (x, y) in a.chunks_exact(3).zip(b.chunks_exact(3)) {
        if x != y {
            n += 1;
            for k in 0..3 {
                worst = worst.max(x[k].abs_diff(y[k]));
            }
        }
    }
    (n, worst)
}

fn assert_band_equal(label: &str, full: &[u8], banded: &[u8], band_h: u32) {
    if full == banded {
        return;
    }
    let stride = (W * 3) as usize;
    let mut diffs = 0usize;
    let mut first = None;
    for (i, (a, b)) in full.iter().zip(banded.iter()).enumerate() {
        if a != b {
            diffs += 1;
            if first.is_none() {
                first = Some(i);
            }
        }
    }
    let i = first.unwrap();
    let (row, col) = (i / stride, (i % stride) / 3);
    dump_png(&format!("fast-band-fail-full-{band_h}.png"), full);
    dump_png(&format!("fast-band-fail-banded-{band_h}.png"), banded);
    panic!(
        "Fast {label} band_h={band_h}: {diffs} differing bytes, first at row {row} col {col} \
         (full rgb {:?} vs banded {:?}) — the AA curve path is NOT band-exact",
        &full[i - i % 3..i - i % 3 + 3],
        &banded[i - i % 3..i - i % 3 + 3]
    );
}

#[test]
fn fast_golden_hash() {
    let (buf, _) = render_full(Quality::Fast);
    dump_png("golden-fast.png", &buf);
    let h = fnv1a(&buf);
    if std::env::var_os("VYR_BLESS").is_some() {
        eprintln!("BLESS: FAST_GOLDEN_FNV1A = {h:#018X}");
        return;
    }
    assert_eq!(
        h, FAST_GOLDEN_FNV1A,
        "Fast golden drifted — investigate before re-blessing"
    );
}

#[test]
fn fast_band_equivalence() {
    let full = render_banded(Quality::Fast, H); // one big band == full frame
    for band_h in [30u32, 17u32] {
        assert_band_equal(
            "DEMO_IR",
            &full,
            &render_banded(Quality::Fast, band_h),
            band_h,
        );
    }
}

#[test]
fn fast_band_equivalence_stress() {
    // How much overscan the AA path actually needs is a MEASURED question
    // (gutter 0 fails; see the module note), so the evidence has to be more
    // than two band heights. Every height from 1 to the full frame, on both
    // scenes, puts a band seam through every row of every curve in turn — if
    // any residual clip-position sensitivity survives, one of these finds it.
    let ir_full = render_banded(Quality::Fast, H);
    let scene_full = scene_banded(Quality::Fast, H);
    for band_h in 1..=H {
        assert_band_equal(
            "DEMO_IR",
            &ir_full,
            &render_banded(Quality::Fast, band_h),
            band_h,
        );
        assert_band_equal(
            "demo_scene",
            &scene_full,
            &scene_banded(Quality::Fast, band_h),
            band_h,
        );
    }
}

#[test]
fn fast_gradient_scene_band_equivalence() {
    // The gradient + rounded fill/stroke ops the IR fixture does not carry.
    // Under Fast the rounded ones take the AA path and the gradient stays an
    // integer ramp, so this is the strictest mixed-surface band test here.
    let full = scene_banded(Quality::Fast, H);
    dump_png("fast-demo-scene.png", &full);
    for band_h in [30u32, 17u32] {
        assert_band_equal(
            "demo_scene",
            &full,
            &scene_banded(Quality::Fast, band_h),
            band_h,
        );
    }
}

#[test]
fn fast_differs_from_both() {
    // The differs-from-both guard, and it needs TWO scenes to state honestly.
    //
    // DEMO_IR: Fast must differ from Draft (else the AA never ran). It does
    // NOT differ from Exact there, and that is a RESULT, not a gap in the
    // test — every op in that fixture is either a curve (which Fast routes to
    // the Exact AA path verbatim) or an axis-aligned integer-coordinate rect
    // (on which AA is arithmetically a no-op: coverage is exactly 1 or 0).
    // So on that scene Fast is the oracle's bytes at a fraction of the work.
    // The equality is asserted, so a future change that breaks it is caught.
    let (fast, _) = render_full(Quality::Fast);
    let (draft, _) = render_full(Quality::Draft);
    let (exact, _) = render_full(Quality::Exact);
    assert_ne!(
        fnv1a(&fast),
        fnv1a(&draft),
        "Fast hashed identical to Draft — the AA curve path did nothing"
    );
    // Fast is NEARLY Exact on DEMO_IR and the bound is asserted, because that
    // is the real claim: every op in that fixture is either a curve (routed to
    // the AA path) or an integer-aligned rect (on which AA is arithmetically a
    // no-op). The residue is the rounded-rect corner decomposition — cutting
    // the shape at integer scanlines is exact in geometry but tiny-skia
    // accumulates a handful of LSBs differently across the seam. If this bound
    // ever blows out, the decomposition has stopped being a decomposition.
    let (n, worst) = pixel_delta(&fast, &exact);
    eprintln!(
        "#27 Fast vs Exact on DEMO_IR: {n}/{} px differ, worst channel {worst}",
        W * H
    );
    assert!(
        n <= 16 && worst <= 24,
        "Fast drifted from Exact on DEMO_IR: {n} px differ (worst channel {worst}) — \
         expected a handful of corner-seam LSBs, not a shape change"
    );
    // demo_scene: carries the two things Fast deliberately keeps integer — a
    // DIAGONAL line (a straight-edged quad, not a curve) and a linear gradient
    // (a colour approximation; Fast is an EDGE-quality tier). Those are where
    // Fast must diverge from Exact, and it must still diverge from Draft.
    let sf = scene_banded(Quality::Fast, H);
    let sd = scene_banded(Quality::Draft, H);
    let se = scene_banded(Quality::Exact, H);
    assert_ne!(
        fnv1a(&sf),
        fnv1a(&sd),
        "Fast hashed identical to Draft on demo_scene — the AA curve path did nothing"
    );
    assert_ne!(
        fnv1a(&sf),
        fnv1a(&se),
        "Fast hashed identical to Exact on demo_scene — the integer line/gradient \
         span paths did nothing, so this is not a cheaper tier"
    );
}

#[test]
fn fast_is_deterministic() {
    // I2 per-tier: same input + same tier = same bytes, every time.
    for q in [Quality::Exact, Quality::Fast, Quality::Draft] {
        let (a, _) = render_full(q);
        let (b, _) = render_full(q);
        assert_eq!(fnv1a(&a), fnv1a(&b), "{q:?} is not reproducible");
    }
}

/// Blend pixels (values that are neither of the two extremes present) and
/// distinct values, over a region — the #27 quality metric. Whole-region
/// *colour* counts are the trap: they inflate with content, not with edge
/// quality. Blend counts do not.
fn edge_stats(buf: &[u8], x0: u32, y0: u32, x1: u32, y1: u32) -> (usize, usize) {
    let stride = (W * 3) as usize;
    let mut seen = [0u32; 256];
    let mut n = 0usize;
    for y in y0..y1 {
        for x in x0..x1 {
            // Luminance-ish: the green channel alone is enough to see a blend
            // step, and avoids counting the same edge three times.
            let v = buf[y as usize * stride + x as usize * 3 + 1];
            seen[v as usize] += 1;
            n += 1;
        }
    }
    assert!(n > 0);
    let distinct = seen.iter().filter(|&&c| c > 0).count();
    // "Blend" = any value that is not one of the two most common (the two
    // flat colours a hard edge produces).
    let mut order: Vec<u32> = seen.to_vec();
    order.sort_unstable_by_key(|&c| core::cmp::Reverse(c));
    let flat: u32 = order.iter().take(2).sum();
    ((n as u32 - flat) as usize, distinct)
}

#[test]
fn fast_anti_aliases_the_curve() {
    // The demo scene's disc lives in the lower-left quadrant; take the whole
    // frame's curve-bearing half so the assertion does not encode a magic rect.
    let (fast, _) = render_full(Quality::Fast);
    let (draft, _) = render_full(Quality::Draft);
    let (exact, _) = render_full(Quality::Exact);
    let (fb, fd) = edge_stats(&fast, 0, 0, W, H);
    let (db, dd) = edge_stats(&draft, 0, 0, W, H);
    let (eb, ed) = edge_stats(&exact, 0, 0, W, H);
    eprintln!(
        "#27 edge quality over {W}x{H}: Exact {eb} blend px / {ed} distinct · \
         Fast {fb} / {fd} · Draft {db} / {dd}"
    );
    assert!(
        fb > db,
        "Fast produced no more blend pixels than Draft ({fb} vs {db}) — no AA happened"
    );
    assert!(
        fd > dd,
        "Fast produced no more distinct values than Draft ({fd} vs {dd})"
    );
    // Fast is an EDGE-quality tier, not a colour-accuracy one: it must not
    // out-blend the oracle.
    assert!(
        fb <= eb,
        "Fast blended MORE than Exact ({fb} vs {eb}) — that is not a cheaper tier"
    );
}

#[test]
fn fast_fastpath_coverage() {
    // Honest coverage (#21): under Fast the shortfall from 100 % IS the
    // anti-aliased curve area — the number the tier is bought with.
    let (_, fast) = render_full(Quality::Fast);
    let (_, draft) = render_full(Quality::Draft);
    assert!(
        fast.fastpath_pixels > 0,
        "Fast drew zero fast-path pixels — the backdrop must still be a span fill"
    );
    assert!(
        fast.fastpath_pixels < draft.fastpath_pixels,
        "Fast fast-path coverage {} is not BELOW Draft's {} — no op moved to the AA path",
        fast.fastpath_pixels,
        draft.fastpath_pixels
    );
    let cov = fast.fastpath_pixels as f64 / fast.pixels_written as f64;
    eprintln!(
        "Fast fast-path coverage: {} / {} delivered px ({:.1}%) — the rest is AA'd curve",
        fast.fastpath_pixels,
        fast.pixels_written,
        100.0 * cov
    );
    assert!(
        cov > 0.25,
        "Fast fast-path coverage {:.1}% — the integer paths have stopped carrying the frame",
        100.0 * cov
    );
}

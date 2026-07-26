//! F16 Draft-tier goldens (#16): the integer no-AA fast path is its OWN
//! separately-gated tier — `Quality::Draft` pixels DIFFER from `Quality::Exact`
//! (hard edges, no AA), so this file pins Draft's own bytes and never asserts
//! Draft == Exact. As of the **v3 set Draft is FULLY INTEGER**: opaque +
//! translucent rects, rounded fills + rounded strokes (hard corners), the curve
//! primitives (disc/ring/line), and the gradient all take the integer span
//! path, and the tier DROPS the 8 px gutter (no AA ⇒ no overscan). On DEMO_IR
//! that is 100 % fast-path coverage and zero tiny-skia. The golden is the
//! hard-edged render; the coverage assertion expects ~all delivered pixels in
//! `fastpath_pixels`. What IS enforced, same discipline as the Exact goldens:
//!
//! - `draft_golden_hash`: the DEMO_IR scene rendered at `Quality::Draft` hashes
//!   to the committed constant — Draft is deterministic (invariant I2 holds
//!   per-tier, the F16 design rule). Re-bless: `./dev.py test --bless`.
//! - `draft_band_equivalence`: the SAME Draft scene rendered as horizontal
//!   bands (even 30-row + uneven 17-row splits) is byte-identical to the
//!   full-frame Draft render (invariant I1 holds per-tier — the span fill is
//!   band-invariant by the same induction as the glyph/image blits).
//! - `draft_differs_from_exact`: a guard that Draft and Exact actually DIVERGE
//!   (no-AA hard edges) — if they ever matched byte-for-byte the fast path
//!   silently did nothing.
//! - `draft_fastpath_coverage`: the honesty number (#21) — the fraction of
//!   delivered pixels the integer fast path carried vs fell back to Exact.
//!
//! Set `VYR_TEST_DUMP=1` to write the Draft + Exact PNGs to ../tmp/ for
//! eyeballing (confirm Draft is hard-edged but correct before blessing).

use vyr_core::demo::{DEMO_H, DEMO_IR, DEMO_W, demo_scene};
use vyr_core::{Assets, Fonts, Quality, Rect, RenderStats, TinySkiaCanvas};

const W: u32 = DEMO_W;
const H: u32 = DEMO_H;

/// Committed Draft golden (FNV-1a 64 of the RGB888 buffer). Re-bless via
/// `./dev.py test --bless`. DISTINCT from the Exact golden by construction.
const DRAFT_GOLDEN_FNV1A: u64 = 0xA705_4B20_5B12_FAA3;

fn fnv1a(data: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(0x100_0000_01b3);
    }
    h
}

/// Render DEMO_IR full-frame at the given quality (no fonts/assets — the demo
/// scene is text- and image-free, so empty registries are honest).
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

#[test]
fn draft_golden_hash() {
    let (buf, _) = render_full(Quality::Draft);
    dump_png("golden-draft.png", &buf);
    let h = fnv1a(&buf);
    if std::env::var_os("VYR_BLESS").is_some() {
        eprintln!("BLESS: DRAFT_GOLDEN_FNV1A = {h:#018X}");
        return;
    }
    assert_eq!(
        h, DRAFT_GOLDEN_FNV1A,
        "Draft golden drifted — investigate before re-blessing"
    );
}

#[test]
fn draft_band_equivalence() {
    let full = render_banded(Quality::Draft, H); // one big band == full frame
    for band_h in [30u32, 17u32] {
        let banded = render_banded(Quality::Draft, band_h);
        if full != banded {
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
            dump_png(&format!("draft-band-fail-full-{band_h}.png"), &full);
            dump_png(&format!("draft-band-fail-banded-{band_h}.png"), &banded);
            panic!(
                "Draft band_h={band_h}: {diffs} differing bytes, first at row {row} col {col} \
                 (full rgb {:?} vs banded {:?})",
                &full[i - i % 3..i - i % 3 + 3],
                &banded[i - i % 3..i - i % 3 + 3]
            );
        }
    }
}

/// Render the hand-built `demo_scene` (the ONLY scene with a `fill_linear_gradient`,
/// plus rounded fills and a stroked rounded border) at `Quality::Draft` as
/// horizontal bands of `band_h`, stitched into a full-frame buffer. Exercises
/// the v3 integer gradient + rounded-fill + rounded-stroke fast paths the IR
/// fixture (DEMO_IR) does not — these MUST stay band-exact for the gutter-off.
fn draft_scene_banded(band_h: u32) -> Vec<u8> {
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
            Quality::Draft,
        )
        .expect("pixmap");
        demo_scene(&mut c);
        let band = &mut out[y as usize * stride..(y + h) as usize * stride];
        c.finish_into_rgb888(band, stride);
        y += h;
    }
    out
}

#[test]
fn draft_gradient_scene_band_equivalence() {
    // The gradient + rounded fill/stroke fast paths, full-frame vs even (30) AND
    // uneven (17) bands — byte-identical, the per-tier I1 invariant. If the
    // integer gradient ramp or the rounded-rect corner carve were not a pure
    // function of WORLD position, a band seam would diverge here.
    let full = draft_scene_banded(H);
    dump_png("draft-demo-scene.png", &full);
    for band_h in [30u32, 17u32] {
        let banded = draft_scene_banded(band_h);
        if full != banded {
            let stride = (W * 3) as usize;
            let i = full
                .iter()
                .zip(banded.iter())
                .position(|(a, b)| a != b)
                .unwrap();
            let (row, col) = (i / stride, (i % stride) / 3);
            panic!(
                "Draft demo_scene band_h={band_h}: first diff at row {row} col {col} \
                 (full {:?} vs banded {:?}) — a gradient/rounded fast path is not band-exact",
                &full[i - i % 3..i - i % 3 + 3],
                &banded[i - i % 3..i - i % 3 + 3]
            );
        }
    }
}

#[test]
fn draft_differs_from_exact() {
    // The whole point of the tier: no AA ⇒ different bytes. If these ever
    // matched, the fast path was a no-op (a silent regression).
    let (draft, _) = render_full(Quality::Draft);
    let (exact, _) = render_full(Quality::Exact);
    dump_png("golden-draft-vs-exact-exact.png", &exact);
    assert_ne!(
        fnv1a(&draft),
        fnv1a(&exact),
        "Draft hashed identical to Exact — the no-AA fast path did nothing"
    );
}

#[test]
fn draft_fastpath_coverage() {
    // Honest coverage (#21): Draft must route a meaningful share of the frame
    // through the integer fast path, and Exact must route NONE.
    let (_, draft) = render_full(Quality::Draft);
    let (_, exact) = render_full(Quality::Exact);
    // Exact uses no integer fast path — UNLESS #37's exact-flat-fast is on, which
    // gives Exact the pixmap-direct flat path (byte-identically; the golden-hash
    // tests still pass). Feature-gate the "none" assertion accordingly.
    #[cfg(not(feature = "exact-flat-fast"))]
    assert_eq!(
        exact.fastpath_pixels, 0,
        "Exact must never use the Draft fast path"
    );
    #[cfg(feature = "exact-flat-fast")]
    let _ = exact.fastpath_pixels;
    assert!(
        draft.fastpath_pixels > 0,
        "Draft drew zero fast-path pixels — the demo scene's opaque rects must hit it"
    );
    // The backdrop alone (120*120) is opaque radius-0 → the fast path covers
    // at least the full screen once; sanity-bound it well below absurd.
    assert!(
        draft.fastpath_pixels >= (W * H) as u64,
        "Draft fast-path pixels {} < one screen ({}) — backdrop should be a span fill",
        draft.fastpath_pixels,
        W * H
    );
    // v3: EVERY op on DEMO_IR (opaque/rounded fills, rounded border stroke, the
    // curves, no gradient here) takes the integer fast path — coverage is 100 %.
    // A regression that dropped ANY op back to the tiny-skia/Exact path (which
    // would also break the gutter-off safety) sinks below this.
    let cov = draft.fastpath_pixels as f64 / draft.pixels_written as f64;
    assert!(
        cov >= 0.999,
        "Draft fast-path coverage {:.1}% < 100% — an op fell back to the tiny-skia path \
         (and the gutter-off is then unsafe for it)",
        100.0 * cov
    );
    eprintln!(
        "Draft fast-path coverage: {} / {} delivered px ({:.1}%)",
        draft.fastpath_pixels,
        draft.pixels_written,
        100.0 * draft.fastpath_pixels as f64 / draft.pixels_written as f64
    );
}

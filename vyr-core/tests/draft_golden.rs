//! F16 Draft-tier goldens (#16): the integer no-AA fast path is its OWN
//! separately-gated tier — `Quality::Draft` pixels DIFFER from `Quality::Exact`
//! (hard edges, no AA), so this file pins Draft's own bytes and never asserts
//! Draft == Exact. What IS enforced, same discipline as the Exact goldens:
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

use vyr_core::demo::{DEMO_H, DEMO_IR, DEMO_W};
use vyr_core::{Assets, Fonts, Quality, Rect, RenderStats};

const W: u32 = DEMO_W;
const H: u32 = DEMO_H;

/// Committed Draft golden (FNV-1a 64 of the RGB888 buffer). Re-bless via
/// `./dev.py test --bless`. DISTINCT from the Exact golden by construction.
const DRAFT_GOLDEN_FNV1A: u64 = 0x4701_15C1_764D_30EE;

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
    assert_eq!(
        exact.fastpath_pixels, 0,
        "Exact must never use the Draft fast path"
    );
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
    eprintln!(
        "Draft fast-path coverage: {} / {} delivered px ({:.1}%)",
        draft.fastpath_pixels,
        draft.pixels_written,
        100.0 * draft.fastpath_pixels as f64 / draft.pixels_written as f64
    );
}

//! #32 — the flattened-contour memo ([`vyr_core::Shapes`]) is a PURE memo.
//!
//! The claim being defended is narrow and total: passing a long-lived
//! `Shapes` in must not change a single output byte, at any tier, at any band
//! height, on any fixture. If it can, the memo is keying on something it must
//! not (something band-dependent), or it is re-associating float arithmetic
//! it must not touch — and band equivalence, which is the repo's day-1
//! invariant I1, would be a coin toss rather than a proof.
//!
//! So every test here renders the SAME scene twice and demands byte equality:
//!
//! - **cached** — one `Shapes` held across every band and across frames, the
//!   way a real banded renderer holds it;
//! - **uncached** — `Shapes::with_budget(0)`, which admits nothing and
//!   therefore flattens every contour on every call. That is bit-for-bit the
//!   pre-#32 painter, so "cached == uncached" is "#32 changed no pixels".
//!
//! The existing golden suites pin the absolute hashes; this suite pins the
//! *difference*, which is the thing the optimisation could plausibly break.
//! It also asserts the two properties the win depends on — that the memo hits
//! ACROSS bands (where 17/18ths of the saving is) and that it stays inside a
//! fixed heap budget, because the M4 arena is 122,880 B and `Quality::Fast`
//! already peaks at 106,889 B of it.

use vyr_core::demo::{CLIP_IR, DEMO_H, DEMO_IR, DEMO_W, IMAGE_ASSET};
use vyr_core::{Assets, Fonts, Quality, Rect, RgbaImage, Shapes};

const W: u32 = DEMO_W;
const H: u32 = DEMO_H;
const CLIP_W: u32 = 120;
const CLIP_H: u32 = 120;

const TIERS: [Quality; 3] = [Quality::Exact, Quality::Fast, Quality::Draft];

fn roboto() -> Fonts {
    let mut fonts = Fonts::new();
    fonts
        .register("roboto", include_bytes!("../../fonts/roboto.ttf").to_vec())
        .expect("roboto registers");
    fonts
}

fn checker_assets() -> Assets {
    let mut reader = png::Decoder::new(std::io::Cursor::new(include_bytes!(
        "assets/checker-24.png"
    )))
    .read_info()
    .expect("png header");
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf).expect("png decode");
    buf.truncate(info.buffer_size());
    let img = RgbaImage::new(info.width, info.height, buf).expect("valid dims");
    let mut assets = Assets::new();
    assets.register(IMAGE_ASSET, img).expect("register");
    assets
}

/// A fixture: the IR and the frame it draws into.
#[derive(Clone, Copy)]
struct Scene {
    ir: &'static str,
    w: u32,
    h: u32,
}

const DEMO: Scene = Scene {
    ir: DEMO_IR,
    w: W,
    h: H,
};
const CLIP: Scene = Scene {
    ir: CLIP_IR,
    w: CLIP_W,
    h: CLIP_H,
};

/// One full frame of `scene`, stitched from `band_h`-row bands, with `shapes`
/// carried across every band (that carry is the thing under test).
fn banded(
    scene: Scene,
    quality: Quality,
    band_h: u32,
    fonts: &mut Fonts,
    assets: &Assets,
    shapes: &mut Shapes,
) -> Vec<u8> {
    let Scene { ir, w, h } = scene;
    let stride = (w * 3) as usize;
    let mut out = vec![0u8; stride * h as usize];
    let mut y = 0;
    while y < h {
        let bh = band_h.min(h - y);
        let band = &mut out[y as usize * stride..(y + bh) as usize * stride];
        vyr_core::render_with_shapes(
            ir,
            fonts,
            assets,
            shapes,
            Rect {
                x: 0,
                y: y as i32,
                w,
                h: bh,
            },
            band,
            stride,
            quality,
        )
        .expect("band renders");
        y += bh;
    }
    out
}

/// Where the two buffers first disagree, and how many bytes do — a diff a
/// human can act on, not just "not equal".
fn diff(a: &[u8], b: &[u8]) -> Option<(usize, usize)> {
    let n = a.iter().zip(b).filter(|(x, y)| x != y).count();
    let first = a.iter().zip(b).position(|(x, y)| x != y)?;
    Some((first, n))
}

/// THE test: a carried memo changes nothing, anywhere.
///
/// Band heights 30 (even divisor), 17 (uneven — the seam-crossing case that
/// found the original flattening bugs), 1 (every row its own band, the
/// harshest re-flattening pattern) and the full frame.
#[test]
fn memo_is_byte_exact_on_demo_ir() {
    for quality in TIERS {
        for band_h in [H, 30, 17, 1] {
            let mut cached = Shapes::new();
            let with = banded(
                DEMO,
                quality,
                band_h,
                &mut Fonts::new(),
                &Assets::new(),
                &mut cached,
            );
            let without = banded(
                DEMO,
                quality,
                band_h,
                &mut Fonts::new(),
                &Assets::new(),
                &mut Shapes::with_budget(0),
            );
            assert_eq!(
                diff(&with, &without),
                None,
                "DEMO_IR {quality:?} band_h={band_h}: memoised render differs from uncached \
                 ({} entries, {} B, {} hits) — the memo is NOT pure",
                cached.cache_entries(),
                cached.cache_bytes(),
                cached.hits(),
            );
        }
    }
}

/// The clip fixture: rounded clips build their A8 mask from `rrect_points`
/// too, once per band. That path shares the memo, so it needs the same proof —
/// a mask byte that moved would be a band-equivalence failure hiding inside a
/// cache hit.
#[test]
fn memo_is_byte_exact_on_clip_ir() {
    for quality in TIERS {
        for band_h in [CLIP_H, 30, 17] {
            let assets = checker_assets();
            let mut cached = Shapes::new();
            let with = banded(CLIP, quality, band_h, &mut roboto(), &assets, &mut cached);
            let without = banded(
                CLIP,
                quality,
                band_h,
                &mut roboto(),
                &assets,
                &mut Shapes::with_budget(0),
            );
            assert_eq!(
                diff(&with, &without),
                None,
                "CLIP_IR {quality:?} band_h={band_h}: memoised render differs from uncached",
            );
        }
    }
}

/// A memo carried across FRAMES is still pure — and by frame 2 it is doing no
/// flattening at all, which is the steady state a real device lives in.
#[test]
fn memo_is_byte_exact_across_frames() {
    let mut shapes = Shapes::new();
    let mut fonts = Fonts::new();
    let assets = Assets::new();
    let first = banded(DEMO, Quality::Exact, 17, &mut fonts, &assets, &mut shapes);
    let after_frame1 = shapes.misses();
    for frame in 1..4 {
        let again = banded(DEMO, Quality::Exact, 17, &mut fonts, &assets, &mut shapes);
        assert_eq!(
            diff(&first, &again),
            None,
            "frame {frame} differs from frame 0 with a warm memo",
        );
        assert_eq!(
            shapes.misses(),
            after_frame1,
            "frame {frame} flattened a contour the warm memo should have held",
        );
    }
}

/// Where the win actually comes from: the SAME shape looked up in band after
/// band. If this ratio ever collapses the memo has started keying on something
/// band-dependent and #32 has silently un-fixed itself.
#[test]
fn memo_hits_across_bands() {
    let mut shapes = Shapes::new();
    banded(
        DEMO,
        Quality::Exact,
        17,
        &mut Fonts::new(),
        &Assets::new(),
        &mut shapes,
    );
    let (hits, misses) = (shapes.hits(), shapes.misses());
    assert!(misses > 0, "the fixture has curves to flatten");
    assert_eq!(shapes.overflow(), 0, "DEMO_IR fits the default budget");
    assert!(
        hits > 3 * misses,
        "only {hits} hits against {misses} misses over {} bands — the memo is not \
         hitting across bands, which is where the saving lives",
        H.div_ceil(17),
    );
    eprintln!(
        "DEMO_IR Exact, 17-row bands: {} entries, {} B, {hits} hits / {misses} misses",
        shapes.cache_entries(),
        shapes.cache_bytes(),
    );
}

/// The memory contract: bounded by construction, and the default budget is
/// enough for the fixtures. A cache that outgrew the M4's ~16 KB of headroom
/// would be a regression, not an optimisation.
#[test]
fn memo_stays_inside_its_budget() {
    for quality in TIERS {
        let mut shapes = Shapes::new();
        banded(
            DEMO,
            quality,
            16,
            &mut Fonts::new(),
            &Assets::new(),
            &mut shapes,
        );
        assert!(
            shapes.cache_bytes() <= Shapes::DEFAULT_BUDGET,
            "{quality:?}: {} B held against a {} B budget",
            shapes.cache_bytes(),
            Shapes::DEFAULT_BUDGET,
        );
        eprintln!(
            "DEMO_IR {quality:?}: {} entries, {} B",
            shapes.cache_entries(),
            shapes.cache_bytes()
        );
    }
}

/// A budget too small to hold the scene must degrade to *exactly* the old
/// behaviour — flatten and return, never a wrong contour and never a panic.
#[test]
fn memo_overflow_is_still_byte_exact() {
    let mut tiny = Shapes::with_budget(256);
    let squeezed = banded(
        DEMO,
        Quality::Exact,
        17,
        &mut Fonts::new(),
        &Assets::new(),
        &mut tiny,
    );
    let plain = banded(
        DEMO,
        Quality::Exact,
        17,
        &mut Fonts::new(),
        &Assets::new(),
        &mut Shapes::with_budget(0),
    );
    assert_eq!(diff(&squeezed, &plain), None, "budget-starved memo differs");
    assert!(
        tiny.overflow() > 0,
        "a 256 B budget should have refused entries on this scene",
    );
    assert!(tiny.cache_bytes() <= 256, "budget breached");
}

/// `Shapes::clear` is a memory decision, never a correctness one.
#[test]
fn cleared_memo_still_renders_the_same_bytes() {
    let mut shapes = Shapes::new();
    let a = banded(
        DEMO,
        Quality::Fast,
        17,
        &mut Fonts::new(),
        &Assets::new(),
        &mut shapes,
    );
    shapes.clear();
    assert_eq!(shapes.cache_bytes(), 0);
    assert_eq!(shapes.cache_entries(), 0);
    let b = banded(
        DEMO,
        Quality::Fast,
        17,
        &mut Fonts::new(),
        &Assets::new(),
        &mut shapes,
    );
    assert_eq!(diff(&a, &b), None, "a cleared memo changed the pixels");
}

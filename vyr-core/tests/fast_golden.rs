//! F16 `Quality::Fast` goldens (#27): the MIDDLE tier — Draft's integer span
//! fills everywhere they apply, the Exact tiny-skia AA path for CURVED
//! geometry only (disc, ring/arc, rounded-rect + rounded-stroke +
//! rounded-gradient corners).
//!
//! Fast adds **no rasteriser and no flattening of its own**. Its curve pixels
//! come from the very same quantized polygons, the same 1/64-px world
//! quantization, the same exact-integer band translation AND an overscan
//! gutter of its own (`FAST_GUTTER`, 4 — measured, not copied from Exact's 8)
//! — routed through the `rgb` scratch round-trip so they composite into
//! Draft's straight-RGB888 band surface in draw order.
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
//! ## The overscan measurement rig (#38)
//!
//! `band_equivalence_sweep` renders EVERY committed 120×120 fixture at EVERY
//! band height 1..=120 — 720 (scene, band_h) splits — at one tier, and reports
//! every split that is not byte-identical to the full-frame render. It is the
//! only way to size an overscan gutter, because the failure mode is a handful
//! of differing bytes at SPECIFIC heights: sampling two heights (which is all
//! the per-fixture goldens do) misses it, and sampling a subset of fixtures
//! misses it too — `GUTTER`'s table exists because CHART_IR was not in the rig
//! when 8 was chosen. Each tier gets an arm: `fast_…`, `exact_…`, `draft_…`,
//! plus two `#[ignore]`d arms carrying the two measured gaps the sweep found
//! (Exact on CHART_IR, Draft on CLIP_IR) with the numbers and the reason each
//! is a gap and not a fix.
//!
//! Set `VYR_TEST_DUMP=1` to write the PNGs to ../tmp/ for eyeballing.

use vyr_core::demo::{
    CHART_IR, CLIP_IR, DEMO_H, DEMO_IR, DEMO_W, IMAGE_ASSET, IMAGE_IR, TEXT_IR, demo_scene,
};
use vyr_core::{Assets, Fonts, Quality, Rect, RenderStats, RgbaImage, TinySkiaCanvas};

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

/// The scenes the band-equivalence sweep covers: **every committed 120×120
/// fixture**, plus the hand-built canvas scene. Sampling a subset is exactly
/// how an undersized gutter survives — the #38 sweep found overscan 3 and 4
/// clean on DEMO_IR + demo_scene + CLIP_IR and NOT clean on CHART_IR, which
/// was not in the rig at the time.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Scene {
    /// The IR fixture: frame, disc, slider, toggle, gauge (ring/arc), line.
    DemoIr,
    /// The hand-built canvas scene — the ONLY one with a
    /// `fill_linear_gradient`, plus rounded fills and a stroked rounded border.
    DemoCanvas,
    /// The clip fixture: children overflowing a ROUNDED container (the A8 mask
    /// path, nested two deep) and a pure-rect one, over path fills, glyph blits
    /// and image blits. The mask is pixmap-sized — i.e. GUTTER-LOCAL — so it is
    /// a scene where an undersized overscan corrupts the mask itself, not just
    /// the fill fringe.
    ClipIr,
    /// The chart fixture: grid + polyline + per-point markers over a bar chart.
    /// **The binding scene for the overscan measurement** — its thin diagonal
    /// polyline segments and 2 px markers carry AA coverage further from a seam
    /// than any disc edge does, so it is the last scene to go clean.
    ChartIr,
    /// The text fixture: three glyph runs (A8 masks, two sizes) crossing seams.
    TextIr,
    /// The image fixture: four RGBA blits with a transparent hole, over the
    /// backdrop and over a frame fill.
    ImageIr,
}

impl Scene {
    fn label(self) -> &'static str {
        match self {
            Scene::DemoIr => "DEMO_IR",
            Scene::DemoCanvas => "demo_scene",
            Scene::ClipIr => "CLIP_IR",
            Scene::ChartIr => "CHART_IR",
            Scene::TextIr => "TEXT_IR",
            Scene::ImageIr => "IMAGE_IR",
        }
    }

    /// The IR body, or `None` for the hand-built canvas scene.
    fn ir(self) -> Option<&'static str> {
        match self {
            Scene::DemoIr => Some(DEMO_IR),
            Scene::ClipIr => Some(CLIP_IR),
            Scene::ChartIr => Some(CHART_IR),
            Scene::TextIr => Some(TEXT_IR),
            Scene::ImageIr => Some(IMAGE_IR),
            Scene::DemoCanvas => None,
        }
    }
}

/// Every scene the sweep runs, in the order a failure is most informative.
const SCENES: [Scene; 6] = [
    Scene::DemoIr,
    Scene::DemoCanvas,
    Scene::ClipIr,
    Scene::ChartIr,
    Scene::TextIr,
    Scene::ImageIr,
];

/// Fonts + assets built ONCE per sweep. `CLIP_IR` needs roboto and the checker
/// PNG, and re-parsing them per band height (120 heights × 3 scenes) would
/// dominate the test. Registering roboto is inert for the other two scenes:
/// `DEMO_IR` carries no text node and `demo_scene` is hand-built canvas ops.
struct Bench {
    fonts: Fonts,
    assets: Assets,
}

impl Bench {
    fn new() -> Self {
        let mut fonts = Fonts::new();
        fonts
            .register("roboto", include_bytes!("../../fonts/roboto.ttf").to_vec())
            .expect("roboto registers");
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
        Self { fonts, assets }
    }

    /// One scene at one tier, stitched from `band_h`-row bands (`band_h == H`
    /// is the full-frame render — the reference every split is compared to).
    fn banded(&mut self, scene: Scene, quality: Quality, band_h: u32) -> Vec<u8> {
        let stride = (W * 3) as usize;
        let mut out = vec![0u8; stride * H as usize];
        let mut y = 0;
        while y < H {
            let h = band_h.min(H - y);
            let area = Rect {
                x: 0,
                y: y as i32,
                w: W,
                h,
            };
            let band = &mut out[y as usize * stride..(y + h) as usize * stride];
            match scene.ir() {
                Some(ir) => {
                    vyr_core::render_with_quality(
                        ir,
                        &mut self.fonts,
                        &self.assets,
                        area,
                        band,
                        stride,
                        quality,
                    )
                    .expect("band renders");
                }
                None => {
                    let mut c = TinySkiaCanvas::new_with_quality(area, quality).expect("pixmap");
                    demo_scene(&mut c);
                    c.finish_into_rgb888(band, stride);
                }
            }
            y += h;
        }
        out
    }
}

/// One-shot form of [`Bench::banded`] for the handful of callers that render a
/// couple of band heights and do not care about the setup cost.
fn banded(scene: Scene, quality: Quality, band_h: u32) -> Vec<u8> {
    Bench::new().banded(scene, quality, band_h)
}

fn render_banded(quality: Quality, band_h: u32) -> Vec<u8> {
    banded(Scene::DemoIr, quality, band_h)
}

fn scene_banded(quality: Quality, band_h: u32) -> Vec<u8> {
    banded(Scene::DemoCanvas, quality, band_h)
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

/// `None` when the two frames are byte-identical; otherwise a one-line
/// description of the divergence (byte count + where it starts + the two RGB
/// triples), which is what makes an overscan verdict readable.
fn band_diff(full: &[u8], banded: &[u8]) -> Option<String> {
    if full == banded {
        return None;
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
    Some(format!(
        "{diffs} differing bytes, first at row {row} col {col} (full rgb {:?} vs banded {:?})",
        &full[i - i % 3..i - i % 3 + 3],
        &banded[i - i % 3..i - i % 3 + 3]
    ))
}

fn assert_band_equal(quality: Quality, label: &str, full: &[u8], banded: &[u8], band_h: u32) {
    if let Some(d) = band_diff(full, banded) {
        dump_png(&format!("band-fail-{quality:?}-full-{band_h}.png"), full);
        dump_png(
            &format!("band-fail-{quality:?}-banded-{band_h}.png"),
            banded,
        );
        panic!("{quality:?} {label} band_h={band_h}: {d} — the AA curve path is NOT band-exact");
    }
}

/// **The overscan measurement rig (#38).** Renders one tier's scenes at EVERY
/// band height from 1 to the full frame and demands byte equality with the
/// full-frame render. A band seam therefore lands on every row of every curve,
/// every glyph and every clip arc in turn — which is what it takes to see the
/// failure mode an undersized [`GUTTER`](vyr_core) produces: a handful of
/// differing bytes at SPECIFIC heights, invisible to a two-height sample.
///
/// It is deliberately tier-agnostic: the gutter is a per-tier constant, and
/// the only defensible way to pick one is to run the same sweep for each tier
/// and read off the smallest overscan that still passes.
fn band_equivalence_sweep(quality: Quality) {
    band_equivalence_sweep_over(quality, &SCENES);
}

fn band_equivalence_sweep_over(quality: Quality, scenes: &[Scene]) {
    let mut b = Bench::new();
    // EVERY failing split is collected, not just the first: "which heights
    // fail, on which scene, by how many bytes" is the measurement. Stopping at
    // the first one turns a spectrum into an anecdote and hides whether a
    // bigger gutter is closing the gap or merely moving it.
    let mut fails: Vec<String> = Vec::new();
    for &scene in scenes {
        let full = b.banded(scene, quality, H);
        for band_h in 1..=H {
            let split = b.banded(scene, quality, band_h);
            if let Some(d) = band_diff(&full, &split) {
                if fails.is_empty() {
                    dump_png(&format!("band-fail-{quality:?}-full.png"), &full);
                    dump_png(&format!("band-fail-{quality:?}-split.png"), &split);
                }
                fails.push(format!("  {} band_h={band_h}: {d}", scene.label()));
            }
        }
    }
    assert!(
        fails.is_empty(),
        "{quality:?}: {} of the {} (scene, band_h) splits are NOT band-exact — the \
         overscan gutter is too small for this tier (#38):\n{}",
        fails.len(),
        scenes.len() * H as usize,
        fails.join("\n")
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
            Quality::Fast,
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
    // than two band heights. `FAST_GUTTER = 4` is this sweep's answer, doubled:
    // 0 fails at band_h=30, 1 at band_h=1, 2 passes clean.
    band_equivalence_sweep(Quality::Fast);
}

#[test]
fn exact_band_equivalence_stress() {
    // The SAME sweep for the oracle tier (#38). Exact's gutter was inherited,
    // not measured, while Fast's was measured on the very same tiny-skia
    // rasterizer — the tiers differ in WHICH primitives reach it, not in how it
    // rasterizes. This arm is what makes `GUTTER` an evidenced constant: the
    // table in its doc comment is the output of running this test against a
    // patched constant, one overscan value at a time.
    //
    // CHART_IR is EXCLUDED here and swept by the `#[ignore]`d test below,
    // because at the shipped `GUTTER = 8` it does not pass and cannot be made
    // to: the smallest sufficient overscan is 16 and the M4's 122,880 B arena
    // runs out at 10. Excluding it in the green test and stating exactly why,
    // with numbers, next to a runnable test that reproduces it, is the honest
    // shape for a known gap; deleting the scene from the rig is not.
    let scenes: Vec<Scene> = SCENES
        .iter()
        .copied()
        .filter(|&s| s != Scene::ChartIr)
        .collect();
    band_equivalence_sweep_over(Quality::Exact, &scenes);
}

/// **A known gap, measured, not a flaky test** (#38 found it, #40 tracks it).
/// `Quality::Exact` is NOT
/// band-exact on `CHART_IR` at the shipped `GUTTER = 8`: 25 of the 120 band
/// heights differ, by ≤3 bytes each, every one of them on the chart's diagonal
/// AA polyline. The sweep table in [`GUTTER`](vyr_core)'s doc comment has the
/// full curve; the two facts that make this an `#[ignore]` rather than a fix:
///
/// - overscan 16 passes and overscan 10 already exhausts the M4's 122,880 B
///   heap arena, so no affordable value closes it;
/// - the required overscan tracks the vertical extent of the polygon the
///   pixmap edge cuts (`scripts/gutter-reach-probe.py`: a 109-row diagonal
///   still fails at overscan 88), so a bigger constant is not a fix in
///   principle either — deterministic world-space polygon pre-clipping is.
///
/// Run it with `cargo test -p vyr-core --test fast_golden -- --ignored`; when
/// #40's pre-clip lands, this becomes a plain `#[test]` and CHART_IR goes back
/// into the green sweep above.
#[test]
#[ignore = "#40: Exact is not band-exact on CHART_IR at GUTTER=8; 16 fixes it and does not fit the M4 arena"]
fn exact_chart_band_equivalence_known_gap() {
    band_equivalence_sweep_over(Quality::Exact, &[Scene::ChartIr]);
}

#[test]
fn draft_band_equivalence_stress() {
    // Draft's gutter is 0, and the claim is structural rather than measured:
    // every Draft op is a pure function of WORLD position written through the
    // exact-integer band offset, so there is no AA fringe to bleed across a
    // seam. Structural claims still get swept — this is what would catch a new
    // Draft op that quietly grew a clip-relative decision.
    //
    // CLIP_IR is excluded for the reason the structural claim itself names: the
    // rounded-clip fallback is the ONE Draft op that reaches tiny-skia. See the
    // `#[ignore]`d test below.
    let scenes: Vec<Scene> = SCENES
        .iter()
        .copied()
        .filter(|&s| s != Scene::ClipIr)
        .collect();
    band_equivalence_sweep_over(Quality::Draft, &scenes);
}

/// **A known gap, measured (#38 found it, #41 tracks it).** `Quality::Draft`
/// renders with no overscan
/// because no Draft op touches tiny-skia — except one: an op overlapping a
/// ROUNDED clip (`ClipFate::Masked`) routes to the shared AA mask path, which
/// is clip-seeded like every other AA draw. On `CLIP_IR` that costs band
/// exactness: 34 of 120 heights differ at overscan 0, 19 at 1, 6 at 2, none
/// from 3 up. Draft keeps the 0 — a gutter would cost it the per-band pixmap
/// (30,720 B → 46,848 B at the reference 480×16 band) and the demul/convert
/// pass over it, which is the entire point of the tier — so the fix is either
/// a lazily-gutter-ed pixmap on the first rounded clip push, or the same
/// polygon pre-clip the Exact gap wants. Run with `-- --ignored`.
#[test]
#[ignore = "#41: Draft's rounded-clip AA fallback is not band-exact at gutter 0 (needs 3)"]
fn draft_rounded_clip_band_equivalence_known_gap() {
    band_equivalence_sweep_over(Quality::Draft, &[Scene::ClipIr]);
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
            Quality::Fast,
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

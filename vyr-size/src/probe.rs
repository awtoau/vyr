//! The painter GEOMETRY PROBE (#37) — an instruction-level test harness for
//! the question "what is the SIMD-shaped pipeline actually charging us?".
//!
//! #37 asks for a decision, and the decision needs three numbers that the
//! whole-frame fixture cannot produce, because a real frame mixes every cost
//! together:
//!
//!   1. **per-draw setup** — path build, edge list, pipeline construction,
//!      paid once per `fill_path` no matter how big the shape is;
//!   2. **per-16-px-chunk cost** — tiny-skia's `lowp` pipeline steps 16 px at
//!      a time (`STAGE_WIDTH = 16`), carrying ~256 B of `u16x16` state across
//!      stage boundaries. On a core with no SIMD that state cannot live in
//!      registers, so this is where the spill traffic lives;
//!   3. **per-pixel cost** — the irreducible coverage/blend arithmetic.
//!
//! This module renders a SWEEP of scenes whose (draws, chunks, pixels) triple
//! varies independently, each one bracketed by a semihosting `bkpt` so
//! `libinsn`'s `match=bkpt,trace=on` delta stream prices it EXACTLY (the same
//! vehicle and the same rule as every other published M4 number — see
//! docs/performance.md §5; never SYS_CLOCK, whose 1 cs = 10^7 insns is coarser
//! than a whole probe case). `scripts/painter-probe.py` fits
//! `cost = a·draws + b·chunks + c·px` to the result.
//!
//! Identifiability comes from the WIDTH sweep, not from the draw count: at a
//! fixed width, draws / chunks / pixels are collinear. Widths below 16 and
//! widths that are not multiples of 16 are the cases that separate `b` from
//! `c` — a partial 16-px chunk does a full chunk's work for fewer pixels,
//! which is exactly the lane waste #37 asks about.
//!
//! Nothing here changes the painter. It is a measurement vehicle: same
//! `render_with_shapes` entry point, same banding, same tier selection as
//! `workload.rs`, so a probe number and a fixture number are the same kind of
//! number.

use alloc::format;
use alloc::string::String;
use alloc::vec::Vec;

use vyr_core::{Assets, Fonts, Quality, RenderError, Shapes};

use crate::opaque;
use crate::workload::{BAND_BYTES, BAND_H, FIXTURE_H, FIXTURE_W, render_frame_banded};

/// Timed repeats per case. The first render of a case is a WARM-UP outside
/// the bracket (it fills the contour memo — #32 — so the timed passes are
/// steady state, the same rule the fixture workload uses), then this many
/// bracketed renders follow. 2 is enough to show that the two agree; more
/// only costs qemu wall time (a probe run is ~20 cases × reps × ~10^6..10^7
/// guest insns under a per-TB plugin callback).
pub const REPS: u32 = 2;

/// The painted strip: rows 16..256 of the 480×270 frame, so every case
/// crosses 15 of the 17 band seams exactly like the fixture does.
const STRIP_Y: u32 = 16;
const STRIP_H: u32 = 240;

/// Maximum draws per case. Bounds the IR-tree heap so EVERY case fits the
/// tightest tier's arena: Fast/Draft carry a full-width RGB scratch (23,040 B)
/// plus the gutter pixmap on top of the tree, and a 64-node tree overran the
/// M4's 122,880 B arena there. 24 leaves comfortable headroom on all tiers.
const MAX_COUNT: u32 = 24;

/// What a case draws. All of them fill; none of them stroke or text — this
/// probe prices the fill pipeline and nothing else.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// Nothing but the root background — the null case. Its cost is the band
    /// loop, the pixmap clear and the IR walk with no widgets, and it is what
    /// every other case is measured ABOVE.
    Null,
    /// `count` axis-aligned rectangles, `w` wide and [`STRIP_H`] tall, laid
    /// side by side from x=0. `radius == 0` is square corners (straight edges
    /// only — the minimum a general path rasteriser can do); `radius > 0`
    /// adds curved corners, so the flattened contour and the anti-aliased
    /// coverage — and the `libm` f64 trig behind them (#63) — come into the
    /// price. A rect/rrect pair at one width isolates the cost of curvature;
    /// a radius SWEEP at one width isolates how that cost scales.
    Rect,
    /// `count` discs of diameter `w`. All curve, no straight edge — the
    /// heaviest flattening/AA per pixel, and the area/perimeter shape #36
    /// characterises. The point where the f64-trig determinism tax is largest.
    Disc,
    /// A hand-written IR child node (`Case::raw`) — the rest of the IR the
    /// parametric grid does not reach: `vy_chart`'s DIAGONAL AA polyline (the
    /// #40/#42 band-equivalence case), axis-aligned `vy_line`, the `vy_gauge`
    /// stroked ring, a bordered `vy_frame`, glyph runs (`vy_label`/`vy_lcd`),
    /// image blits (`vy_image`), and the real composite widgets (slider /
    /// progress / toggle). Pixel count is MEASURED (`pixels_written`), not
    /// analytic. (Gradients are not here: no IR attribute reaches
    /// `fill_linear_gradient` — it is painter-internal, not in the schema.)
    Widget,
}

/// One probe case. `alpha` < 255 selects the blended source-over path;
/// `radius` is the corner radius for [`Kind::Rect`] (0 = square).
pub struct Case {
    pub name: String,
    pub kind: Kind,
    pub w: u32,
    pub count: u32,
    pub alpha: u8,
    pub radius: u32,
    /// [`Kind::Widget`] only: the raw IR child node(s) to drop into the scene,
    /// and the short kind label to report (e.g. "chart", "text").
    pub raw: Option<String>,
    pub klabel: &'static str,
}

/// A hand-listed identifiability case (static name). `radius` defaults to 0
/// for `Rect` — the rounded grid cases use [`cr`].
fn c(name: &'static str, kind: Kind, w: u32, count: u32, alpha: u8) -> Case {
    Case {
        name: name.into(),
        kind,
        w,
        count,
        alpha,
        radius: 0,
        raw: None,
        klabel: "",
    }
}

/// A case with an explicit corner radius (rounded rects).
fn cr(name: String, kind: Kind, w: u32, count: u32, alpha: u8, radius: u32) -> Case {
    Case {
        name,
        kind,
        w,
        count,
        alpha,
        radius,
        raw: None,
        klabel: "",
    }
}

/// A raw-IR "see the rest of the IR" case: `child` is a full IR node
/// (`{"name":…,"attrs":{…}}`) dropped verbatim into the scene. `klabel` is the
/// short kind reported (chart / line / ring / border / text / image / …).
fn cw(name: &'static str, klabel: &'static str, child: &str) -> Case {
    Case {
        name: name.into(),
        kind: Kind::Widget,
        w: 0,
        count: 1,
        alpha: 255,
        radius: 0,
        raw: Some(child.into()),
        klabel,
    }
}

/// The sizes swept in the generated grid: small → large, deliberately mixing
/// chunk-aligned (16, 48, 96, 192) with unaligned (8, 24) widths.
const GRID_SIZES: &[u32] = &[8, 16, 24, 48, 96, 192];
/// Opaque and one translucent alpha — the blend axis (#60), applied to every
/// grid primitive so the per-pixel-blend cost is priced on curves too.
const GRID_ALPHAS: &[u8] = &[255, 128];
/// Corner radii for the rounded-rect grid rows: a small and a large radius,
/// so curvature cost is a SWEEP (r=0 rect vs r=8 vs r=24), not a single point.
const GRID_RADII: &[u32] = &[8, 24];

/// The full sweep, built at runtime.
///
/// Two parts:
///   1. **Identifiability cases** (hand-listed): the width sweep, the count
///      sweep and the `b15…b33` chunk-boundary family that
///      `scripts/painter-probe.py`'s `null + a·draws + b·chunks + c·px` fit
///      needs. These carry the regression that separates per-draw from
///      per-chunk from per-pixel — see their comments below.
///   2. **The grid** (generated): every {primitive × size × alpha} — and, for
///      rounded rects, × radius. This is the ~200-point landscape: broad
///      coverage of where cost lives, priced identically to the fixture.
///
/// `count` is chosen so `w·count ≤ FIXTURE_W` (draws never overlap → exact
/// pixel count) and the node tree fits the M4's 122,880 B arena.
///
/// `VYR_PROBE_POINT`, if set at build time, filters to the single case of that
/// name — the isolated build the deep class-split pass (`--deep`) runs under
/// hotblocks so a point's {int, mem, hw-f32, soft-f64} mix is attributable
/// (an aggregate hotblocks pass over the whole suite cannot be split per
/// point). `null` is always kept so the driver can subtract the boot floor.
pub fn cases() -> Vec<Case> {
    let mut v: Vec<Case> = Vec::new();
    v.push(c("null", Kind::Null, 0, 0, 255));
    // Width sweep, opaque, square corners. `count` is capped at [`MAX_COUNT`]:
    // the Fast/Draft tiers carry a full-width RGB scratch AND a gutter pixmap
    // (~70 KB fixed) on top of the IR tree, and a 64-node tree overran the
    // 122,880 B arena on those tiers (Exact has no RGB scratch, so it fit).
    // Capping keeps every case renderable on EVERY tier, which is what makes a
    // point comparable across tiers. The identifiability the fit needs comes
    // from the `b15…b33` boundary family and `n1/n4/n16` (draw-count sweep at
    // fixed width); the width sweep at a fixed count is still a clean
    // width-vs-cost curve.
    for &(name, w, count) in &[
        ("w1", 1u32, 64u32),
        ("w2", 2, 64),
        ("w4", 4, 64),
        ("w8", 8, 60),
        ("w12", 12, 40),
        ("w16", 16, 30),
        ("w20", 20, 24),
        ("w32", 32, 15),
        ("w48", 48, 10),
        ("w60", 60, 8),
        ("w120", 120, 4),
        ("w240", 240, 2),
        ("w480", 480, 1),
    ] {
        v.push(c(name, Kind::Rect, w, count.min(MAX_COUNT), 255));
    }
    // draw count at a chunk-aligned width (16 = exactly one lowp chunk)
    v.push(c("n1", Kind::Rect, 16, 1, 255));
    v.push(c("n4", Kind::Rect, 16, 4, 255));
    v.push(c("n16", Kind::Rect, 16, 16, 255));
    // CHUNK BOUNDARY family — the decisive lane-waste test. COUNT IS FIXED, so
    // draws are constant and only the width moves; crossing a multiple of 16
    // adds a whole chunk per row for one extra pixel. A cliff means the
    // pipeline charges by the chunk; a smooth line means by the pixel.
    for &(name, w) in &[
        ("b15", 15u32),
        ("b16", 16),
        ("b17", 17),
        ("b31", 31),
        ("b33", 33),
    ] {
        v.push(c(name, Kind::Rect, w, 15, 255));
    }
    // blend identifiability pair (kept for continuity with earlier runs)
    v.push(c("blend16", Kind::Rect, 16, 30, 128));
    v.push(c("blend480", Kind::Rect, 480, 1, 128));

    // --- the generated grid: primitive × size × alpha (× radius) ---
    for &alpha in GRID_ALPHAS {
        for &w in GRID_SIZES {
            let count = (FIXTURE_W / w).clamp(1, 4);
            let a = alpha; // shorthand for the names
            // flat rect
            v.push(cr(
                fmt_name("rect", w, a, 0),
                Kind::Rect,
                w,
                count,
                alpha,
                0,
            ));
            // rounded rects at each radius (radius capped at w/2 — a radius
            // wider than the box is not a rounded rect, it is the disc below)
            for &rad in GRID_RADII {
                if rad * 2 <= w {
                    v.push(cr(
                        fmt_name("rr", w, a, rad),
                        Kind::Rect,
                        w,
                        count,
                        alpha,
                        rad,
                    ));
                }
            }
            // disc of diameter w
            v.push(cr(
                fmt_name("disc", w, a, 0),
                Kind::Disc,
                w,
                count,
                alpha,
                0,
            ));
        }
    }

    // --- the rest of the IR: hand-written showcase widgets (count 1 each) ---
    // These reach what the rect/disc grid cannot — most importantly the
    // DIAGONAL anti-aliased polyline (`vy_chart`), which is the exact geometry
    // #40/#42 fight over band-equivalence on. Pixels are MEASURED, so the grid
    // prices them the same way as the grid cases. Positions cross band seams.
    v.push(cw(
        "chart_diag",
        "chart",
        "{\"name\":\"vy_chart\",\"attrs\":{\"x\":\"40\",\"y\":\"30\",\"width\":\"400\",\
         \"height\":\"200\",\"points\":\"5,95,25,60,80,20,50,88,15,70\",\"range_min\":\"0\",\
         \"range_max\":\"100\",\"line_color\":\"#88C0D0\",\"line_width\":\"2\"}}",
    ));
    v.push(cw(
        "chart_thick",
        "chart",
        "{\"name\":\"vy_chart\",\"attrs\":{\"x\":\"40\",\"y\":\"30\",\"width\":\"400\",\
         \"height\":\"200\",\"points\":\"5,95,25,60,80,20,50,88,15,70\",\"range_min\":\"0\",\
         \"range_max\":\"100\",\"line_color\":\"#88C0D0\",\"line_width\":\"5\"}}",
    ));
    v.push(cw(
        "chart_bar",
        "chart",
        "{\"name\":\"vy_chart\",\"attrs\":{\"x\":\"40\",\"y\":\"30\",\"width\":\"400\",\
         \"height\":\"200\",\"chart_type\":\"bar\",\"points\":\"25,55,40,75,50,88,35\",\
         \"range_min\":\"0\",\"range_max\":\"100\",\"line_color\":\"#A3BE8C\"}}",
    ));
    v.push(cw(
        "line_h",
        "line",
        "{\"name\":\"vy_line\",\"attrs\":{\"x\":\"20\",\"y\":\"133\",\"width\":\"440\",\
         \"height\":\"3\",\"background\":\"#4C566A\"}}",
    ));
    v.push(cw(
        "gauge",
        "ring",
        "{\"name\":\"vy_gauge\",\"attrs\":{\"x\":\"170\",\"y\":\"45\",\"width\":\"170\",\
         \"height\":\"170\",\"value\":\"65\",\"color\":\"#88C0D0\"}}",
    ));
    v.push(cw(
        "border",
        "border",
        "{\"name\":\"vy_frame\",\"attrs\":{\"x\":\"40\",\"y\":\"40\",\"width\":\"400\",\
         \"height\":\"180\",\"background\":\"#2E3440\",\"radius\":\"12\",\"border_width\":\"3\",\
         \"border_color\":\"#88C0D0\"}}",
    ));
    v.push(cw(
        "text",
        "text",
        "{\"name\":\"vy_label\",\"attrs\":{\"x\":\"24\",\"y\":\"120\",\"width\":\"440\",\
         \"height\":\"32\",\"text\":\"vyr glyph run 0123456789\",\"color\":\"#ECEFF4\",\
         \"style_text_font\":\"roboto_20\"}}",
    ));
    v.push(cw("lcd", "text",
        "{\"name\":\"vy_lcd\",\"attrs\":{\"x\":\"170\",\"y\":\"110\",\"width\":\"140\",\
         \"height\":\"44\",\"text\":\"1480\",\"color\":\"#A3BE8C\",\"style_text_font\":\"roboto_20\"}}"));
    v.push(cw(
        "image",
        "image",
        "{\"name\":\"vy_image\",\"attrs\":{\"x\":\"216\",\"y\":\"96\",\"width\":\"48\",\
         \"height\":\"48\",\"src\":\"checker-24.png\"}}",
    ));
    v.push(cw(
        "slider",
        "widget",
        "{\"name\":\"vy_slider\",\"attrs\":{\"x\":\"40\",\"y\":\"120\",\"width\":\"400\",\
         \"height\":\"22\",\"value\":\"62\"}}",
    ));
    v.push(cw(
        "progress",
        "widget",
        "{\"name\":\"vy_progress\",\"attrs\":{\"x\":\"40\",\"y\":\"120\",\"width\":\"400\",\
         \"height\":\"14\",\"value\":\"80\"}}",
    ));
    v.push(cw(
        "toggle",
        "widget",
        "{\"name\":\"vy_toggle\",\"attrs\":{\"x\":\"210\",\"y\":\"116\",\"width\":\"60\",\
         \"height\":\"32\",\"value\":\"1\"}}",
    ));

    // Build-time single-point isolation for the deep pass.
    if let Some(sel) = option_env!("VYR_PROBE_POINT") {
        v.retain(|case| case.name == sel || case.name == "null");
    }
    v
}

/// `rr48a128r24` etc. — a stable per-point slug. `r0` is elided for the flat
/// primitives so `rect48a255` reads cleanly.
fn fmt_name(prim: &str, w: u32, alpha: u8, radius: u32) -> String {
    if radius == 0 {
        format!("{prim}{w}a{alpha}")
    } else {
        format!("{prim}{w}a{alpha}r{radius}")
    }
}

/// Painted pixels, analytically. Discs use the ideal area — the rasterised
/// count differs by the AA fringe, which is < 1 % at d=120 and is reported
/// by the renderer's own `pixels_written` anyway.
pub fn case_px(case: &Case) -> u64 {
    match case.kind {
        Kind::Null => 0,
        // Rounded corners remove a few px per corner; the AA-exact count is in
        // the renderer's own `pixels_written`, so the analytic value is the
        // square-box area for both flat and rounded rects.
        Kind::Rect => (case.w as u64) * (STRIP_H as u64) * (case.count as u64),
        // πr² with r = w/2, in integer arithmetic (no float in the report).
        Kind::Disc => (case.count as u64) * (355 * (case.w as u64) * (case.w as u64)) / (4 * 113),
        // Composites have no closed-form area — the driver uses the measured
        // `pixels_written` for these instead of this analytic value.
        Kind::Widget => 0,
    }
}

/// The IR for one case. Built at runtime rather than as a const string
/// because the sweep is 22 scenes and the M4 has no filesystem to read them
/// from; the string is dropped as soon as it is parsed.
pub fn case_ir(case: &Case) -> String {
    let mut s = String::with_capacity(64 + 120 * case.count as usize);
    s.push_str(&format!(
        "{{\"schema_version\":\"0.6-vyvanse\",\"w\":{FIXTURE_W},\"h\":{FIXTURE_H},\
         \"root\":{{\"name\":\"view\",\"attrs\":{{\"background\":\"#101418\"}},\"children\":["
    ));
    for i in 0..case.count {
        if i > 0 {
            s.push(',');
        }
        let x = i * case.w;
        // A single mid-grey, alpha in the 8-hex literal (ir.rs `fill_alpha`).
        let bg = format!("#7FA8C8{:02X}", case.alpha);
        match case.kind {
            Kind::Null => {}
            Kind::Rect if case.radius > 0 => s.push_str(&format!(
                "{{\"name\":\"vy_frame\",\"attrs\":{{\"x\":\"{x}\",\"y\":\"{STRIP_Y}\",\
                 \"width\":\"{}\",\"height\":\"{STRIP_H}\",\"radius\":\"{}\",\
                 \"background\":\"{bg}\"}}}}",
                case.w, case.radius
            )),
            Kind::Rect => s.push_str(&format!(
                "{{\"name\":\"vy_frame\",\"attrs\":{{\"x\":\"{x}\",\"y\":\"{STRIP_Y}\",\
                 \"width\":\"{}\",\"height\":\"{STRIP_H}\",\"background\":\"{bg}\"}}}}",
                case.w
            )),
            Kind::Disc => s.push_str(&format!(
                "{{\"name\":\"vy_circle\",\"attrs\":{{\"x\":\"{x}\",\"y\":\"{STRIP_Y}\",\
                 \"width\":\"{}\",\"height\":\"{}\",\"background\":\"{bg}\"}}}}",
                case.w, case.w
            )),
            // The raw IR node, dropped in verbatim (count is 1 for widgets).
            Kind::Widget => {
                if let Some(raw) = &case.raw {
                    s.push_str(raw);
                }
            }
        }
    }
    s.push_str("]}}");
    s
}

/// Run the whole sweep.
///
/// The shape of the run is dictated by the measurement, not by readability:
/// **the timed section emits nothing at all**. Every semihosting call is a
/// `bkpt`, and `libinsn` prints a delta at every `bkpt` — so a stray print
/// inside the timed section would insert a phantom case into the stream that
/// `scripts/painter-probe.py` has no way to identify. Metadata goes out
/// before, results after.
///
/// Delta stream contract (what the script parses):
///   * one leading delta (boot + setup), then
///   * for each case, for each rep: `[render delta][gap delta]`,
///   * i.e. deltas 1, 3, 5, … are the renders, in `CASES` × `REPS` order.
pub fn run(
    emit: &mut dyn FnMut(&str),
    heap: &dyn Fn() -> (usize, usize),
    clock: Option<&dyn Fn() -> i32>,
    band_buf: &mut [u8],
    quality: Quality,
) -> Result<u64, RenderError> {
    if band_buf.len() != BAND_BYTES {
        return Err(RenderError::BadIr(format!(
            "band_buf is {} B, expected {BAND_BYTES}",
            band_buf.len()
        )));
    }
    let qname = match quality {
        Quality::Exact => "Exact",
        Quality::Fast => "Fast",
        Quality::Draft => "Draft",
    };
    let cases = cases();
    emit(&format!(
        "INFO  [vyr-probe] painter geometry probe (#37): {} cases x {REPS} timed reps, \
         {FIXTURE_W}x{FIXTURE_H} in {FIXTURE_W}x{BAND_H} bands, quality={qname}",
        cases.len()
    ));
    // The whole case table up front, so the script can map delta index →
    // case without any output inside the timed section. `radius` is reported
    // so a rounded-rect point is distinguishable from its flat twin.
    for (i, case) in cases.iter().enumerate() {
        emit(&format!(
            "INFO  [vyr-probe] case i={i} name={} kind={} w={} count={} alpha={} radius={} px={}",
            case.name,
            match case.kind {
                Kind::Null => "null",
                Kind::Rect if case.radius > 0 => "rrect",
                Kind::Rect => "rect",
                Kind::Disc => "disc",
                Kind::Widget => case.klabel,
            },
            case.w,
            case.count,
            case.alpha,
            case.radius,
            case_px(case)
        ));
    }

    // Register the subset font + checker image so the `text`/`image` showcase
    // cases render (the fill grid does not need them; the cost is tiny and
    // outside every timed window). Mirrors workload.rs's registration.
    let mut fonts = Fonts::new();
    fonts.register("roboto", opaque(crate::workload::SUBSET_FONT).to_vec())?;
    let mut assets = Assets::new();
    assets.register(
        vyr_core::demo::IMAGE_ASSET,
        vyr_core::RgbaImage::new(24, 24, opaque(crate::workload::CHECKER).to_vec())?,
    )?;
    let mut hashes: Vec<(u64, u64)> = Vec::with_capacity(cases.len());

    for case in &cases {
        // Parse and warm OUTSIDE the bracket: the IR string, the tree build
        // and the first flatten of each contour are not per-frame render cost
        // (the fixture workload parses once and renders many, and #32's memo
        // is warm in steady state — the probe reproduces that state).
        let ir = case_ir(case);
        let req = vyr_core::ir::Request::parse(opaque(&ir[..]))?;
        drop(ir);
        let mut shapes = Shapes::new();
        // The SAME banded render the fixture workload prices — not a copy of
        // it. A probe that measured its own render loop would answer a
        // question about the probe.
        let (hash, pixels, _) = render_frame_banded(
            &req,
            &mut fonts,
            &assets,
            &mut shapes,
            band_buf,
            quality,
            None,
        )?;
        hashes.push((hash, pixels));

        // --- timed section: NO emit, NO alloc reporting, only clock bkpts ---
        if let Some(clk) = clock {
            for _ in 0..REPS {
                clk();
                render_frame_banded(
                    &req,
                    &mut fonts,
                    &assets,
                    &mut shapes,
                    band_buf,
                    quality,
                    None,
                )?;
                clk();
            }
        }
    }

    // #45: the per-case hash and the run roll are the cross-ISA claim and live
    // in the verify build; `hash` is a sentinel 0 in the perf build (which
    // computed no fold), so the perf build reports pixels only and never a hash
    // it did not measure.
    for (i, (hash, pixels)) in hashes.iter().enumerate() {
        let _ = hash;
        #[cfg(feature = "verify")]
        emit(&format!(
            "INFO  [vyr-probe] result i={i} name={} fnv1a={hash:#018x} pixels_written={pixels}",
            cases[i].name
        ));
        #[cfg(not(feature = "verify"))]
        emit(&format!(
            "INFO  [vyr-probe] result i={i} name={} pixels_written={pixels}",
            cases[i].name
        ));
    }
    let (live, peak) = heap();
    emit(&format!(
        "ALERT [vyr-probe] probe ok: cases={} reps={REPS} heap peak={peak} B live-end={live} B",
        cases.len()
    ));
    // The run's own identity: one hash over every case hash, so a probe run
    // is a single cross-ISA comparable value like the fixture's frame hash.
    #[cfg(feature = "verify")]
    {
        let mut roll = 0xcbf2_9ce4_8422_2325u64;
        for (h, _) in &hashes {
            for b in h.to_le_bytes() {
                roll ^= b as u64;
                roll = roll.wrapping_mul(0x100_0000_01b3);
            }
        }
        emit(&format!("ALERT [vyr-probe] probe roll fnv1a={roll:#018x}"));
        Ok(roll)
    }
    #[cfg(not(feature = "verify"))]
    Ok(0)
}

/// Host-only: write every case's IR to `dir/<name>.json`, so the SAME case
/// definitions can be priced on x86-64 by `vyr-cli` under callgrind. One
/// definition of the sweep, two ISAs — which is the only way the "the shape
/// costs more without SIMD" claim can be stated as a ratio rather than an
/// assertion.
#[cfg(not(target_os = "none"))]
pub fn dump_scenes(dir: &std::path::Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dir)?;
    for case in &cases() {
        std::fs::write(dir.join(format!("{}.json", case.name)), case_ir(case))?;
    }
    Ok(())
}

/// Host-only: the number of cases in the sweep (for reporting; the M4 leg
/// prints its own count). Kept as a fn so `main.rs` need not build the Vec
/// only to `.len()` it — but building it is cheap, so it just does.
#[cfg(not(target_os = "none"))]
pub fn case_count() -> usize {
    cases().len()
}

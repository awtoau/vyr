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

/// What a case draws. All of them fill; none of them stroke or text — this
/// probe prices the fill pipeline and nothing else.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// Nothing but the root background — the null case. Its cost is the band
    /// loop, the pixmap clear and the IR walk with no widgets, and it is what
    /// every other case is measured ABOVE.
    Null,
    /// `count` axis-aligned rectangles, `w` wide and [`STRIP_H`] tall, laid
    /// side by side from x=0. Square corners: straight edges only, so the
    /// coverage work is the minimum a general path rasteriser can do.
    Rect,
    /// As [`Kind::Rect`] but with an 8 px corner radius — curved edges, so
    /// the flattened contour and the anti-aliased coverage come into the
    /// price. The rect/rrect pair at the same width is the cost of curvature.
    RRect,
    /// `count` discs of diameter `w`. The area/perimeter shape the AA scaling
    /// work (#36, docs/measurements/lvgl-gap.md §4) already characterises —
    /// carried here so the probe can be cross-read against it.
    Disc,
}

/// One probe case. `alpha` < 255 selects the blended source-over path (an
/// extra pipeline stage) rather than the opaque one.
pub struct Case {
    pub name: &'static str,
    pub kind: Kind,
    pub w: u32,
    pub count: u32,
    pub alpha: u8,
}

const fn c(name: &'static str, kind: Kind, w: u32, count: u32, alpha: u8) -> Case {
    Case {
        name,
        kind,
        w,
        count,
        alpha,
    }
}

/// The sweep.
///
/// `w`×`count` never exceeds [`FIXTURE_W`], so draws never overlap and the
/// painted pixel count is exact. Counts are capped at 64: each rect is an IR
/// node, and the M4 leg's whole heap is 122,880 B — a 480-node tree does not
/// fit, and a probe that OOMs prices nothing.
///
/// Rows, in order of what they isolate:
///   * `null`                      — the floor every other case sits on
///   * `w1 … w480` (equal area where possible) — the width sweep: draws ∝ 1/w
///     at constant pixels, and the sub-16 / non-multiple-of-16 widths carry
///     the partial-chunk signal
///   * `n1 … n30` at w=16         — draw count at a chunk-aligned width
///   * `blend*`                    — the same geometry with alpha < 255
///   * `rrect*` / `disc*`          — curvature and area/perimeter shape
pub const CASES: &[Case] = &[
    c("null", Kind::Null, 0, 0, 255),
    // width sweep, opaque, square corners
    c("w1", Kind::Rect, 1, 64, 255),
    c("w2", Kind::Rect, 2, 64, 255),
    c("w4", Kind::Rect, 4, 64, 255),
    c("w8", Kind::Rect, 8, 60, 255),
    c("w12", Kind::Rect, 12, 40, 255),
    c("w16", Kind::Rect, 16, 30, 255),
    c("w20", Kind::Rect, 20, 24, 255),
    c("w32", Kind::Rect, 32, 15, 255),
    c("w48", Kind::Rect, 48, 10, 255),
    c("w60", Kind::Rect, 60, 8, 255),
    c("w120", Kind::Rect, 120, 4, 255),
    c("w240", Kind::Rect, 240, 2, 255),
    c("w480", Kind::Rect, 480, 1, 255),
    // draw count at a chunk-aligned width (16 = exactly one lowp chunk)
    c("n1", Kind::Rect, 16, 1, 255),
    c("n4", Kind::Rect, 16, 4, 255),
    c("n16", Kind::Rect, 16, 16, 255),
    // CHUNK BOUNDARY family — the decisive lane-waste test, and the reason
    // the width sweep above is not enough on its own. In that sweep the draw
    // count moves with the width to hold the area constant, which makes
    // "draws" and "16-px chunks" collinear for every width ≤ 16: the fit
    // cannot tell a partial chunk from an extra draw.
    //
    // Here the COUNT IS FIXED, so draws are constant and only the width
    // moves. Crossing a multiple of 16 adds a whole chunk per row for one
    // extra pixel: b16 → b17 is +7 % pixels but +100 % chunks. If the
    // pipeline charges by the chunk, that step is a cliff; if it charges by
    // the pixel, the line is smooth. There is no third answer, and no
    // whole-frame measurement can produce either.
    c("b15", Kind::Rect, 15, 15, 255),
    c("b16", Kind::Rect, 16, 15, 255),
    c("b17", Kind::Rect, 17, 15, 255),
    c("b31", Kind::Rect, 31, 15, 255),
    c("b33", Kind::Rect, 33, 15, 255),
    // blend: same geometry, partial alpha
    c("blend16", Kind::Rect, 16, 30, 128),
    c("blend480", Kind::Rect, 480, 1, 128),
    // curvature and shape
    c("rrect120", Kind::RRect, 120, 4, 255),
    c("rrect480", Kind::RRect, 480, 1, 255),
    c("disc120", Kind::Disc, 120, 4, 255),
];

/// Painted pixels, analytically. Discs use the ideal area — the rasterised
/// count differs by the AA fringe, which is < 1 % at d=120 and is reported
/// by the renderer's own `pixels_written` anyway.
pub fn case_px(case: &Case) -> u64 {
    match case.kind {
        Kind::Null => 0,
        Kind::Rect | Kind::RRect => (case.w as u64) * (STRIP_H as u64) * (case.count as u64),
        // πr² with r = w/2, in integer arithmetic (no float in the report).
        Kind::Disc => (case.count as u64) * (355 * (case.w as u64) * (case.w as u64)) / (4 * 113),
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
            Kind::Rect => s.push_str(&format!(
                "{{\"name\":\"vy_frame\",\"attrs\":{{\"x\":\"{x}\",\"y\":\"{STRIP_Y}\",\
                 \"width\":\"{}\",\"height\":\"{STRIP_H}\",\"background\":\"{bg}\"}}}}",
                case.w
            )),
            Kind::RRect => s.push_str(&format!(
                "{{\"name\":\"vy_frame\",\"attrs\":{{\"x\":\"{x}\",\"y\":\"{STRIP_Y}\",\
                 \"width\":\"{}\",\"height\":\"{STRIP_H}\",\"radius\":\"8\",\
                 \"background\":\"{bg}\"}}}}",
                case.w
            )),
            Kind::Disc => s.push_str(&format!(
                "{{\"name\":\"vy_circle\",\"attrs\":{{\"x\":\"{x}\",\"y\":\"{STRIP_Y}\",\
                 \"width\":\"{}\",\"height\":\"{}\",\"background\":\"{bg}\"}}}}",
                case.w, case.w
            )),
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
    emit(&format!(
        "INFO  [vyr-probe] painter geometry probe (#37): {} cases x {REPS} timed reps, \
         {FIXTURE_W}x{FIXTURE_H} in {FIXTURE_W}x{BAND_H} bands, quality={qname}",
        CASES.len()
    ));
    // The whole case table up front, so the script can map delta index →
    // case without any output inside the timed section.
    for (i, case) in CASES.iter().enumerate() {
        emit(&format!(
            "INFO  [vyr-probe] case i={i} name={} kind={} w={} count={} alpha={} px={}",
            case.name,
            match case.kind {
                Kind::Null => "null",
                Kind::Rect => "rect",
                Kind::RRect => "rrect",
                Kind::Disc => "disc",
            },
            case.w,
            case.count,
            case.alpha,
            case_px(case)
        ));
    }

    let mut fonts = Fonts::new();
    let assets = Assets::new();
    let mut hashes: Vec<(u64, u64)> = Vec::with_capacity(CASES.len());

    for case in CASES {
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
            true,
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
                    false,
                )?;
                clk();
            }
        }
    }

    for (i, (hash, pixels)) in hashes.iter().enumerate() {
        emit(&format!(
            "INFO  [vyr-probe] result i={i} name={} fnv1a={hash:#018x} pixels_written={pixels}",
            CASES[i].name
        ));
    }
    let (live, peak) = heap();
    emit(&format!(
        "ALERT [vyr-probe] probe ok: cases={} reps={REPS} heap peak={peak} B live-end={live} B",
        CASES.len()
    ));
    // The run's own identity: one hash over every case hash, so a probe run
    // is a single cross-ISA comparable value like the fixture's frame hash.
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

/// Host-only: write every case's IR to `dir/<name>.json`, so the SAME case
/// definitions can be priced on x86-64 by `vyr-cli` under callgrind. One
/// definition of the sweep, two ISAs — which is the only way the "the shape
/// costs more without SIMD" claim can be stated as a ratio rather than an
/// assertion.
#[cfg(not(target_os = "none"))]
pub fn dump_scenes(dir: &std::path::Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dir)?;
    for case in CASES {
        std::fs::write(dir.join(format!("{}.json", case.name)), case_ir(case))?;
    }
    Ok(())
}

//! The tiny-skia painter — first `Canvas` implementation (F1).
//!
//! Band-native by construction (invariant I1): a `TinySkiaCanvas` *is* one
//! band. Draw calls take WORLD coordinates.
//!
//! ## Band-equivalence engineering (why this painter feeds ONLY polygons)
//!
//! The I1 contract: stitched bands are BYTE-identical to a full-frame
//! render. Discovered by the band-equivalence golden, in order:
//!
//! 1. tiny-skia clips path geometry at the pixmap edge, and **clipping
//!    subdivides curves**, which perturbs flattening along the whole
//!    remaining arc — AA shifted an LSB 8+ rows away from the clip line, so
//!    no overscan gutter can bound it.
//! 2. The stroker expands curves into new curves, which then hit (1).
//!
//! Fix: the painter never hands tiny-skia a curve or a stroke. All geometry
//! is flattened HERE, deterministically:
//!
//! - **Fixed-step flattening** (segment count derived from radius alone, no
//!   adaptive subdivision) — identical decisions for identical shapes.
//! - **World-space quantization to 1/64 px** (`q()`), then translation by
//!   exact integers — both exact in f32 for our coordinate ranges, so a
//!   vertex's position relative to the pixel grid is bit-identical in every
//!   band. Straight edges cut by the pixmap clip stay collinear; fixed-point
//!   edge walking does the rest.
//! - **Strokes are built as polygons**: rings/outlines as outer+inner
//!   contours (winding fill), lines as quads with butt caps.
//!
//! Max flattening error ≤ ~0.1 px (visually nil); the [`GUTTER`] overscan
//! stays as belt-and-braces for clip-adjacent AA. Enforcement is
//! `tests/golden.rs::band_equivalence` (even + uneven band heights).
//!
//! **That flattening is MEMOISED (#32, [`crate::Shapes`]).** Fixed-step
//! flattening makes `circle_points`/`rrect_points` pure functions of their
//! arguments — which also means a banded render re-ran them for every band a
//! shape touches (17 at the reference `BAND_H = 16`), every frame, and on an
//! M4F that is the most expensive thing in the frame: `libm` computes f32 trig
//! in f64, the FPU is single-precision, so one `cosf` is ~1,145 instructions of
//! soft-float builtins. The memo is caller-owned (`no_std` + `forbid(unsafe)`
//! leaves no room for a global) and it is a memo and NOTHING else — same
//! arguments, same `f32` vertices, bit for bit. It cannot move a pixel, and
//! `tests/shapes.rs` proves it by rendering everything twice, cached against
//! `Shapes::with_budget(0)`. All three tier frame hashes are unchanged.
//!
//! **Glyphs (F5) sit OUTSIDE the polygon rule, safely**: glyph outlines are
//! rasterized once into A8 masks in glyph-local space (`text::raster_glyph`
//! — no band, no clip), and [`Canvas::glyph_run`] here only BLITS those
//! cached masks at integer world positions with a manual integer
//! source-over ([`d255`] rounding). Per-pixel math depends only on world
//! position and existing dst — identical in every band by induction.
//! Enforcement: `tests/text_golden.rs::text_band_equivalence`.
//!
//! **Image blits (F6) follow the same pattern**: [`Canvas::blit_image`] is a
//! source-over of caller-decoded straight-alpha RGBA onto the band — the glyph
//! formula with the image's own per-pixel colour+alpha in place of (paint
//! colour × coverage). The image is SCALED into the FIT-TO-CELL `dst` rect
//! (preserve aspect, centred — `ir::fit_to_cell`); resampling is
//! NEAREST-NEIGHBOUR via FIXED-POINT INTEGER source mapping
//! (`sx = (wx - dst.x)·W / dst.w`, integer division, clamped) — a pure integer
//! function of WORLD position, so the mapped src pixel for a given world pixel
//! is identical in every band. Band-exact by the SAME induction as the 1:1
//! blit; no float in the per-pixel map. (Qt's SMOOTH/bilinear is a future F16
//! quality knob — the dst GEOMETRY matches Qt now, resampling QUALITY is a
//! separate axis.) Enforcement: `tests/image_golden.rs::image_band_equivalence`
//! (+ the scaled-image seam-crossing band-equivalence test).
//!
//! ## The clip stack (F3) — same discipline, applied to masks
//!
//! [`Canvas::push_clip`]/[`Canvas::pop_clip`] nest rounded-rect clips; the
//! active region is the INTERSECTION of all entries (∩ the band — the band
//! is the outermost clip by construction, I1). Mechanism, in order of
//! preference per op (decided from WORLD-space data only, so every band
//! takes the same path — that is what keeps clipping band-exact):
//!
//! 1. **Skip**: the op's conservative bbox misses the clip region — no
//!    pixels, no counters (same world-space verdict in every band).
//! 2. **Unclipped**: the bbox is provably INSIDE every clip entry
//!    (exact integer rect test + integer corner-arc test with a 2 px
//!    margin clearing the clip edge's AA/flattening fringe) — the op draws
//!    exactly as if no clip existed, BYTE-identical to the pre-clip
//!    renderer. This is why goldens whose children sit inside their
//!    containers did not move when clipping landed.
//! 3. **Rect spans**: all entries are radius-0 and the op is a manual
//!    integer loop (glyph/image blit) — loop bounds intersect the exact
//!    integer clip rect. No mask, pure integer math.
//! 4. **Mask**: anything else rasterizes the clip ONCE per band into a
//!    tiny-skia A8 [`Mask`] and draws through it. The mask is built from
//!    the SAME quantized-world fixed-step polygons as paint geometry
//!    ([`rrect_points`] — never a tiny-skia curve) translated by exact
//!    integers, so mask bytes are a pure function of WORLD position:
//!    band-invariant by the same argument as fills. Nested entries
//!    compose via `Mask::intersect_path` (exact `round(a·b/255)` per
//!    pixel). Path fills pass the mask to tiny-skia (coverage × mask in
//!    the blit pipeline — per-pixel, geometry untouched); glyph/image
//!    loops multiply it in with the same [`d255`] rounding.
//!
//! Enforcement: `tests/clip_golden.rs` (overflowing children, rounded +
//! rect containers, nested clips; full-frame vs even AND uneven bands,
//! byte-exact).
//!
//! tiny-skia rasterizes premultiplied RGBA8888 internally; the caller's
//! buffer contract is RGB888 (the oracle's PNG format; RGB565 conversion for
//! MCU panels is a flush concern, measured in F9). `finish_into_rgb888`
//! does the demul+convert per band.
//!
//! Counters: pixel counts here are CLAMPED BOUNDING-BOX approximations,
//! good enough to wire the plumbing; F2 replaces them with exact per-class
//! span counting and adds the bench-side ns/px. Class split so far:
//! alpha==255 → OpaqueFill, else AlphaBlend (AA edges not yet attributed).

use alloc::vec::Vec;

use crate::shapes::{Key, Shapes};
use crate::{Canvas, OpClass, PlacedGlyph, Quality, Rect, RenderStats, Rgb, RgbaImage};
use tiny_skia::{
    FillRule, GradientStop, LinearGradient, Mask, Paint, PathBuilder, Pixmap, Point,
    PremultipliedColorU8, SpreadMode, Transform,
};

/// Vertex quantization grid: 1/64 px. `v * 64` is exact in f32 for our
/// coordinate range (|v| < 2^15), rounding is exact, dividing by a power of
/// two is exact — so quantized world coords survive integer translation
/// bit-exactly.
const SUBPX: f32 = 64.0;

/// Overscan gutter, in pixels, rasterized around every band and discarded.
/// Bounds the tiny-skia AA fringe in the clip-adjacent rows (the polygon
/// discipline above is the load-bearing fix). Cost: (w+16)×(h+16) raster for a
/// w×h band — a per-band fixed cost the I4 scaling assertion tracks (F2).
///
/// **MEASURED (#38), and the measurement says this value is a CEILING, not a
/// bound.** `tests/fast_golden.rs::exact_band_equivalence_stress` renders all
/// six committed 120×120 fixtures at EVERY band height 1..=120 — 720 (scene,
/// band_h) splits — and demands byte equality with the full-frame render:
///
/// | overscan | failing splits | where |
/// |---|---|---|
/// | 0 | 177 | DEMO_IR 57 · demo_scene 34 · CLIP_IR 34 · CHART_IR 44 · TEXT_IR 8 |
/// | 1 | 89 | DEMO_IR 19 · demo_scene 11 · CLIP_IR 19 · CHART_IR 40 |
/// | 2 | 44 | CLIP_IR 6 · CHART_IR 38 |
/// | 3 | 34 | CHART_IR only — every other fixture is clean from here up |
/// | 4 | 31 | CHART_IR |
/// | 6 | 32 | CHART_IR |
/// | 8 | 25 | CHART_IR — **shipped** |
/// | 12 | 15 | CHART_IR |
/// | 16 | 0 | passes, all 720 splits |
/// | 24 / 32 / 64 | 0 | passes |
///
/// So 8 is NOT sufficient: 25 splits still differ, by ≤3 bytes each, all of
/// them on `CHART_IR`'s diagonal AA polyline. 16 is sufficient — and **does
/// not fit the target**. Measured on the M4 workload (480×270 in 480×16 bands,
/// 122,880 B arena, `./dev.py qemu-m4`):
///
/// | GUTTER | band pixmap | M4 heap peak | insns/frame |
/// |---|--:|--:|--:|
/// | 4 | 46,848 B | 89,913 B | 42.2 M |
/// | 8 | 63,488 B | 112,473 B | 51.3 M |
/// | 10 | 72,000 B | **allocation failure** (5,280 B alloc, peak 101,777 B) | — |
/// | 16 | 98,304 B | **allocation failure** (5,120 B alloc) | — |
///
/// The largest overscan the arena admits is 8; the smallest that clears the
/// fixtures is 16. **The two sets do not intersect**, so this constant is a
/// documented compromise and the residue is a known gap, not a bound — #40,
/// held open by `exact_chart_band_equivalence_known_gap` (`#[ignore]`d, with
/// the numbers).
///
/// And growing the number is not the fix even where RAM allows it:
/// `scripts/gutter-reach-probe.py` sweeps ONE diagonal `line` op of controlled
/// vertical extent and finds the required overscan tracks the extent of the
/// polygon the pixmap edge CUTS — a 109-row line still fails 31 of 120 splits
/// at overscan 88 and only goes clean at 96. tiny-skia's edge walker is seeded
/// at the rasterization clip, so a cut edge accumulates coverage from a
/// different starting row; overscan only helps by making the cut rarer. **The
/// fix is to stop handing the rasterizer a polygon it has to cut** — clip our
/// own polygons to the pixmap in world space, on the same 1/64 grid, so every
/// band feeds tiny-skia an identical uncut path. That done, the other five
/// fixtures measure a minimum of 3, and dropping to 4 is worth 22,560 B of
/// arena (18.4 %) and 9.2 M insns/frame (17.9 %) at Exact — both measured
/// above, not projected. That is #40's payoff, not a separate optimisation.
const GUTTER: u32 = 8;

/// Overscan for the `Fast` tier (#27). Fast runs the same AA rasterizer as
/// Exact, so it needs SOME overscan — but how much is a MEASURED question, not
/// a copied constant, because Fast also carries Draft's `rgb` band buffer and
/// the two together overran the M4's 122,880 B heap arena at [`GUTTER`].
/// `tests/fast_golden.rs::fast_band_equivalence_stress` renders every committed
/// fixture at EVERY band height from 1 to the full frame — 720 (scene, band_h)
/// splits — and demands byte equality. Re-derived over all six fixtures in #38
/// (the original table saw only DEMO_IR + demo_scene and read the minimum as 2;
/// CLIP_IR's rounded-clip mask needs one more):
///
/// | overscan | failing splits | where |
/// |---|---|---|
/// | 0 | 144 | DEMO_IR 57 · demo_scene 34 · CLIP_IR 34 · CHART_IR 11 · TEXT_IR 8 |
/// | 1 | 49 | DEMO_IR 19 · demo_scene 11 · CLIP_IR 19 |
/// | 2 | 6 | CLIP_IR |
/// | 3 | 0 | passes — the measured minimum |
/// | 4 | 0 | passes — **shipped**, one px of margin |
///
/// At the reference 480x16 band that is 46,848 B of pixmap instead of 63,488,
/// which is what lets Fast run on the F405-class part at all.
///
/// **Fast is band-exact where Exact is not, and that is structural, not luck**
/// (#38): every CHART_IR failure in [`GUTTER`]'s table is the diagonal AA
/// polyline, and Fast routes `line` to Draft's integer span path — a
/// straight-edged quad whose per-pixel verdict is a pure function of world
/// position. So the two constants stay separate: they differ because the tiers
/// differ in WHICH primitives reach the rasterizer, which is the definition of
/// the tiers. One constant would have to be the max of the two, and 16 does not
/// fit the arena (see [`GUTTER`]).
const FAST_GUTTER: u32 = 4;

/// Quantize a world-space scalar to the 1/64-px grid (deterministic libm
/// rounding; no platform float environment dependence).
fn q(v: f32) -> f32 {
    libm::roundf(v * SUBPX) / SUBPX
}

/// Points of a full circle, quantized world space, counter-clockwise.
/// Fixed step count from the radius alone: per-quarter segments
/// `max(4, ceil(r/2))` keeps sagitta error under ~0.09 px up to r=60 and
/// scales linearly after; capped so giant discs stay bounded.
fn circle_points(cx: i32, cy: i32, r: f32) -> Vec<(f32, f32)> {
    let per_quarter = libm::ceilf(r / 2.0).clamp(4.0, 64.0) as u32;
    let n = per_quarter * 4;
    let (cx, cy) = (cx as f32, cy as f32);
    let mut pts = Vec::with_capacity(n as usize);
    for k in 0..n {
        let theta = (k as f32) * (2.0 * core::f32::consts::PI) / (n as f32);
        pts.push((q(cx + r * libm::cosf(theta)), q(cy + r * libm::sinf(theta))));
    }
    pts
}

/// Points of a rounded-rect contour, quantized world space, clockwise in
/// screen coords (y-down). `rad` is pre-clamped by the caller. Corner arcs
/// share the circle flattening (same step rule → same look).
fn rrect_points(x: f32, y: f32, w: f32, h: f32, rad: f32) -> Vec<(f32, f32)> {
    if rad <= 0.0 {
        return alloc::vec![q2(x, y), q2(x + w, y), q2(x + w, y + h), q2(x, y + h)];
    }
    let per_quarter = libm::ceilf(rad / 2.0).clamp(4.0, 64.0) as u32;
    // Corner centers, in traversal order from the top-left corner going
    // clockwise (screen coords): TL, TR, BR, BL. Each quarter sweeps 90°.
    let corners = [
        (x + rad, y + rad, 180.0_f32),
        (x + w - rad, y + rad, 270.0),
        (x + w - rad, y + h - rad, 0.0),
        (x + rad, y + h - rad, 90.0),
    ];
    let mut pts = Vec::with_capacity((per_quarter as usize + 1) * 4);
    for (ccx, ccy, start_deg) in corners {
        for k in 0..=per_quarter {
            let theta = (start_deg + 90.0 * (k as f32) / (per_quarter as f32)).to_radians();
            pts.push((
                q(ccx + rad * libm::cosf(theta)),
                q(ccy + rad * libm::sinf(theta)),
            ));
        }
    }
    pts
}

/// ONE corner of a rounded rect, clipped to its own integer cut lines — the
/// Fast tier's decomposition (#27, see `TinySkiaCanvas::fill_rrect_fast`).
///
/// `corner` is 0=TL, 1=TR, 2=BR, 3=BL, matching `rrect_points`' traversal
/// order, and the arc points are generated by the SAME rule that function
/// uses (`per_quarter = clamp(ceil(rad/2), 4, 64)` steps, `q()`-quantized) so
/// the curve is bit-for-bit the one the whole-shape path would have drawn.
/// The contour is then closed along `cx_l`/`cx_r`/`cy_t`/`cy_b`, which are
/// INTEGER coordinates outside the arc's own extent: a cut there is collinear
/// with the rect's straight edges and cannot alter the coverage of any pixel
/// on the curved side.
/// Borrowed run of quantized world-space vertices.
type Pts<'a> = &'a [(f32, f32)];

#[allow(clippy::too_many_arguments)] // the four integer cut lines are the point
fn rrect_corner_points(
    x: f32,
    y: f32,
    w: f32,
    h: f32,
    rad: f32,
    corner: usize,
    cx_l: i32,
    cx_r: i32,
    cy_t: i32,
    cy_b: i32,
) -> Vec<(f32, f32)> {
    let per_quarter = libm::ceilf(rad / 2.0).clamp(4.0, 64.0) as u32;
    let (ccx, ccy, start_deg) = match corner {
        0 => (x + rad, y + rad, 180.0_f32),
        1 => (x + w - rad, y + rad, 270.0),
        2 => (x + w - rad, y + h - rad, 0.0),
        _ => (x + rad, y + h - rad, 90.0),
    };
    let (lx, rx) = (cx_l as f32, cx_r as f32);
    let (ty, by) = (cy_t as f32, cy_b as f32);
    // Lead-in / lead-out vertices that close the quarter arc against the cuts,
    // traversed in the same rotational sense as `rrect_points` (clockwise in
    // screen coords).
    let (lead, tail): (Pts, Pts) = match corner {
        0 => (&[(x, ty)], &[(lx, y), (lx, ty)]),
        1 => (&[(rx, y)], &[(x + w, ty), (rx, ty)]),
        2 => (&[(rx, by), (x + w, by)], &[(rx, y + h)]),
        _ => (&[(x, by), (lx, by), (lx, y + h)], &[]),
    };
    let mut pts = Vec::with_capacity(per_quarter as usize + 5);
    for &(px, py) in lead {
        pts.push(q2(px, py));
    }
    for k in 0..=per_quarter {
        let theta = (start_deg + 90.0 * (k as f32) / (per_quarter as f32)).to_radians();
        pts.push((
            q(ccx + rad * libm::cosf(theta)),
            q(ccy + rad * libm::sinf(theta)),
        ));
    }
    for &(px, py) in tail {
        pts.push(q2(px, py));
    }
    pts
}

fn q2(x: f32, y: f32) -> (f32, f32) {
    (q(x), q(y))
}

/// Exact `round(x / 255)` in pure integer math — the deterministic blend
/// divisor for glyph blits (valid for x ≤ 2·255², well inside u32).
fn d255(x: u32) -> u32 {
    (2 * x + 255) / 510
}

/// Fill a length-multiple-of-3 RGB888 slice with one repeating `trip` colour.
/// The Draft opaque span fill — the single hottest path (the M4 backdrop +
/// every opaque rect is this). A naive per-pixel `chunks_exact_mut(3)` write
/// is ~9× the old u32 `slice.fill`; instead seed the first triple, then
/// EXPONENTIALLY double the written prefix with `copy_within` (log₂(n)
/// large memcpys), so the compiler emits wide vector copies. Byte-identical
/// to a per-pixel store.
#[inline]
fn fill_rgb_triple(buf: &mut [u8], trip: [u8; 3]) {
    if buf.is_empty() {
        return;
    }
    buf[0] = trip[0];
    buf[1] = trip[1];
    buf[2] = trip[2];
    let mut filled = 3usize;
    let len = buf.len();
    while filled < len {
        let n = filled.min(len - filled);
        buf.copy_within(0..n, filled);
        filled += n;
    }
}

/// Integer floor square root of a non-negative `i64` (binary digit-by-digit,
/// no float, deterministic). The Draft disc/ring per-scanline half-width is
/// `isqrt(r²−dy²)` — an EXACT integer (no libm, no rounding mode dependence).
fn isqrt_i64(n: i64) -> i64 {
    debug_assert!(n >= 0);
    if n < 2 {
        return n;
    }
    // Highest power-of-four ≤ n.
    let mut bit: i64 = 1 << ((63 - n.leading_zeros()) & !1);
    let mut x: i64 = 0;
    let mut rem = n;
    while bit != 0 {
        if rem >= x + bit {
            rem -= x + bit;
            x = (x >> 1) + bit;
        } else {
            x >>= 1;
        }
        bit >>= 2;
    }
    x
}

/// Round a world scalar to the Draft line's 1/256-px fixed-point grid
/// (deterministic libm rounding). Exact in f32 for |v| < 2^15 (`v*256` and the
/// power-of-two divide are exact), so vertices survive integer band offset
/// bit-exactly — the same argument as [`q`] at a finer grid.
fn fp(v: f32) -> i64 {
    libm::roundf(v * 256.0) as i64
}

/// Floor division for `i64` (rounds toward −∞, unlike `/` which truncates
/// toward zero) — needed for the line scanline-span column bounds when the
/// fixed-point crossing is negative.
fn div_floor(a: i64, b: i64) -> i64 {
    let q = a / b;
    if (a % b != 0) && ((a < 0) != (b < 0)) {
        q - 1
    } else {
        q
    }
}

/// One clip-stack entry, WORLD coordinates. `radius` is stored as pushed;
/// clamping (half the short side) happens wherever it is consumed, exactly
/// like fills.
#[derive(Clone, Copy, Debug)]
struct Clip {
    rect: Rect,
    radius: u32,
}

/// How an op relates to the active clip stack — decided ONLY from
/// world-space data (op bbox + clip entries), never from the band, so the
/// verdict is identical in every band (module docs, clip section).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ClipFate {
    /// No clip can affect this op — draw byte-identically to the unclipped path.
    Unclipped,
    /// The op cannot intersect the clip region — draw (and count) nothing.
    Skip,
    /// All entries are radius-0 rects: manual blit loops clamp their integer
    /// bounds to this exact world-space intersection rect.
    RectSpans(Rect),
    /// At least one rounded entry overlaps the op: draw through the A8 mask.
    Masked,
}

/// ## The Draft quality tier (F16, #16) — integer, no AA, no tiny-skia
///
/// (For the middle tier see "The Fast quality tier" below.)
///
/// `Quality::Draft` is the budgeted-MCU runtime tier: it recovers the
/// per-pixel cost the Exact tier spends on float-coverage AA. The **v3 set is
/// FULLY INTEGER** — every paint op rasterizes with DIRECT INTEGER spans (no
/// path, no coverage, no tiny-skia), so Draft touches tiny-skia NOWHERE on the
/// paint path:
///
/// - **`fill_rrect`** — radius-0 opaque is one slice store per row
///   ([`fill_rrect_draft`]); radius>0 (any alpha) and translucent fills carve
///   the corners with the disc's integer arc test ([`fill_rrect_rounded_draft`]
///   via [`rrect_row_span`]) — HARD rounded corners, no AA.
/// - **`stroke_rrect`** ([`stroke_rrect_rounded_draft`]): the centred-stroke
///   annulus = outer rounded-rect spans minus inner rounded-rect spans per row.
/// - **`fill_linear_gradient`** ([`fill_gradient_draft`]): an integer per-pixel
///   two-stop linear ramp `round((1−t)·from + t·to)`, `t` a 1/256 fixed-point
///   ramp across the rect (pure integer; the rounded shape reuses the carve).
/// - **`disc`** ([`disc_draft`]) / **`ring`** ([`ring_draft`]) / **`line`**
///   ([`line_draft`]): the integer circle / annulus / butt-capped-quad spans.
/// - **glyphs + images** ([`Canvas::glyph_run`]/[`Canvas::blit_image`]): already
///   hand-rolled integer source-over blits (never tiny-skia), unchanged.
///
/// Each op takes its per-pixel decision from WORLD coordinates and an integer
/// source-over for the write ([`Self::draft_span`]) — OPAQUE (plain premultiplied
/// store) and TRANSLUCENT (the d255 source-over) both, so a translucent fill is
/// still a fast-path pixel.
///
/// Because Draft never touches tiny-skia, it has **NO AA fringe to bound, so it
/// DROPS THE 8 px [`GUTTER`] overscan** (the [`gutter`](Self::gutter) field is 0
/// for Draft) — the pixmap is exactly the band, ~halving the per-band raster +
/// `finish_into_rgb888` demul+convert (the profile's dominant cost).
///
/// **Band-exact WITHIN the tier by the same induction as the glyph/image
/// blits** (module docs above): every Draft op's per-pixel verdict is a pure
/// function of WORLD position (op geometry ∩ the band ∩ the rect-only clip),
/// written with the exact-integer world→pixmap offset. No AA, no fringe — every
/// band that touches a given world pixel writes the same byte, gutter or none.
/// Enforcement: `tests/draft_golden.rs` (Draft golden hash + even/uneven
/// band equivalence on DEMO_IR AND the gradient/rounded `demo_scene`; the M4
/// workload's host leg adds the text+image fixture). Draft pixels DIFFER from
/// Exact (hard edges, no AA) — the tier's deliberate trade; never asserted
/// == Exact.
///
/// The ONLY op that declines the fast path is one meeting a ROUNDED clip overlap
/// (`ClipFate::Masked`) — it routes to the Exact tiny-skia mask path so the clip
/// stays exact (rare; the M4/DEMO fixtures never hit it). A translucent gradient
/// or stroke also declines (kept exact; vanishingly rare in the IR).
/// [`RenderStats::fastpath_pixels`] records exactly how many pixels the fast
/// path carried so the coverage % is honest (#21).
///
/// ## The Fast quality tier (#27) — Draft's spans, Exact's curves
///
/// `Quality::Fast` is the middle tier. It takes EVERY Draft integer span path
/// above, unchanged, and routes only CURVED geometry back through the Exact
/// tiny-skia polygon path so its edges are anti-aliased:
///
/// | op | Fast |
/// |---|---|
/// | `fill_rrect` radius 0 | Draft's integer span (AA on an integer-aligned axis-aligned rect is arithmetically a no-op) |
/// | `fill_rrect` radius > 0 | [`Self::fill_rrect_fast`]: four AA corner squares + integer spans for the flat remainder |
/// | `stroke_rrect` radius 0 / > 0 | integer / whole-shape AA (carrying only the four frame strips) |
/// | `disc`, `ring` | whole-shape AA — no flat part to keep |
/// | `line` | Draft's integer quad. A butt-capped line is straight-edged, not curved; its diagonal edges stay hard, deliberately |
/// | `fill_linear_gradient` radius 0 / > 0 | integer ramp / whole-shape AA (Fast is an EDGE-quality tier, not a colour-accuracy one) |
/// | glyphs, images | Draft's integer blits — glyph A8 masks are already anti-aliased |
///
/// **Fast adds no rasteriser and no flattening.** The polygons, the 1/64-px
/// world quantization and the exact-integer band translation are Exact's,
/// which is what keeps band equivalence byte-exact. It does keep an overscan
/// gutter ([`FAST_GUTTER`]) for the same reason Exact does, sized by
/// measurement rather than by copying [`GUTTER`].
///
/// **What it costs, measured, so nobody has to guess:** on the 480x270 panel
/// fixture Fast reproduces Exact's pixels EXACTLY (0 differing px) and costs
/// 29.9 M M4 instructions against Exact's 37.8 M and Draft's 6.8 M — 4.4x
/// Draft. tiny-skia's AA fill is ~1.3 k M4 instructions per curve pixel, so
/// the fixture's ~17 k curve pixels outweigh Draft's entire frame. Cutting the
/// rounded rects into corner squares is what makes it 1.26x cheaper than Exact
/// rather than more expensive; the discs and rings have no flat part to cut and
/// they dominate what is left. A tier that is genuinely NEAR Draft needs a
/// coverage-aware integer curve rasteriser, not a routing change
/// (docs/performance.md §3.1).
pub struct TinySkiaCanvas {
    pixmap: Pixmap,
    /// **Draft only** — the output surface IS this straight-RGB888 band buffer
    /// (`area.w · area.h · 3` bytes, draw-order composited). The Draft fast
    /// path writes RGB888 here directly, skipping the premul-pixmap→demul
    /// round-trip that the x86 profile pinned at ~72 % of Draft cost
    /// (`finish_into_rgb888` ~59 % + the pixmap memset ~13 %). Empty
    /// (`Vec::new()`) for Exact, which keeps the premul `pixmap` path
    /// unchanged. Dst is always opaque RGB here (the backdrop fills it opaque
    /// first), so premul == straight and the `d255` blends are byte-identical
    /// to the old path — enforced by the Draft goldens (no re-bless).
    ///
    /// For the rare rounded-clip fallback the shared tiny-skia rasterizer
    /// still renders into `pixmap` (scratch), then composites the AA'd region
    /// into `rgb` in draw order (see [`Self::fill`]). So `pixmap` stays
    /// allocated for Draft too (it also backs the clip A8 mask, which is
    /// pixmap-sized — [`Self::ensure_mask`]), but the per-band finish copy is
    /// a plain row-blit, no demul. Net Draft band heap is therefore 7 B/px
    /// (4 B pixmap + 3 B rgb) vs the old 4 B/px — a bounded rise that still
    /// sits well under the Exact tier's footprint; the insn win (the demul +
    /// convert pass deleted) is the trade.
    rgb: Vec<u8>,
    area: Rect,
    quality: Quality,
    /// Overscan, in pixels, on every band side — [`GUTTER`] for `Exact`, **0
    /// for `Draft`**. The gutter is belt-and-braces for the tiny-skia AA fringe
    /// in clip-adjacent rows; Draft NEVER touches tiny-skia (every op is an
    /// integer span with a pixel-exact hard edge, v3), so it has no AA fringe to
    /// bound and needs no overscan. Dropping it ~halves the per-band raster and
    /// the `finish_into_rgb888` demul+convert (the 59 %-of-Draft cost the
    /// profile found). Band-equivalence still holds: integer spans clip at the
    /// band edge to byte-identical pixels with or without the gutter (no AA to
    /// bleed). Enforced by `tests/draft_golden.rs` (gutter-less even+uneven
    /// band equivalence). Exact's gutter is UNCHANGED.
    gutter: u32,
    stats: RenderStats,
    /// Active clip stack (world coords). Empty = band-only clipping.
    clips: Vec<Clip>,
    /// Cached intersection bbox of all entries' rects (meaningless while
    /// `clips` is empty). The conservative outer bound for Skip tests and
    /// rect-span clamping.
    clip_bounds: Rect,
    /// Lazily-built A8 intersection mask of the clip stack, pixmap-sized
    /// (gutter-local), invalidated on push/pop. Built only when an op
    /// actually needs `ClipFate::Masked` — scenes whose children stay
    /// inside their containers never pay for it.
    clip_mask: Option<Mask>,
    /// Flattened-contour memo (#32). **Owned** by the canvas while it draws and
    /// handed back out at the end ([`Self::take_cache`]) so it can outlive the
    /// band — which is the whole point: `circle_points`/`rrect_points` are pure
    /// functions of their arguments, and a banded render re-ran them for every
    /// band a shape touches (17 at the reference `BAND_H = 16`), every frame,
    /// at ~1,145 M4 instructions per `cosf` (see [`crate::shapes`]).
    ///
    /// Owned rather than borrowed (`&'a mut Shapes`) so `TinySkiaCanvas` keeps
    /// its plain, lifetime-free type: every existing `new`/`new_with_quality`
    /// caller is untouched and simply gets a private empty cache.
    shapes: Shapes,
}

impl TinySkiaCanvas {
    /// A canvas for ONE band. `area` is the band's rectangle in world
    /// coordinates; the backing pixmap is `(area.w + 2·GUTTER) ×
    /// (area.h + 2·GUTTER)` — see [`GUTTER`].
    pub fn new(area: Rect) -> Option<Self> {
        Self::new_with_quality(area, Quality::Exact)
    }

    /// A canvas for ONE band at an explicit [`Quality`] tier (F16, #16).
    /// `Quality::Exact` is identical to [`Self::new`] (the oracle path,
    /// [`GUTTER`]-overscan pixmap); `Quality::Draft` enables the integer no-AA
    /// fast path AND drops the gutter (pixmap is exactly `area.w × area.h` — no
    /// `2·GUTTER` overscan), because no Draft op touches tiny-skia, so there is
    /// no AA fringe to bound (see the [`gutter`](Self::gutter) field). The
    /// gutter-off ~halves the per-band raster + finish cost; band-equivalence
    /// still holds by the integer-span argument.
    pub fn new_with_quality(area: Rect, quality: Quality) -> Option<Self> {
        // Draft is fully integer (no AA) ⇒ no overscan needed; Exact keeps it.
        // **Fast KEEPS the gutter**, and this was measured, not assumed: built
        // gutter-less first, `tests/fast_golden.rs::fast_band_equivalence`
        // failed with 4 differing bytes on DEMO_IR at band_h=30 and 5 on
        // `demo_scene` at 17. tiny-skia's AA edge walker is seeded at the
        // rasterization clip's top edge and stepped from there, so where the
        // clip lands relative to an edge changes the LSB of the accumulated
        // coverage. Pushing the clip GUTTER px outside the band is exactly what
        // makes Exact byte-exact across bands, and Fast runs the same
        // rasterizer, so it needs the same overscan. Fast therefore pays
        // Exact's per-band pixmap and Draft's per-band `rgb` — the honest
        // price of AA'ing curves at all, recorded here so nobody "optimises"
        // it back off.
        //
        // **Draft's 0 has one measured exception (#38).** The integer-span
        // argument covers every Draft op EXCEPT the rounded-clip fallback: an
        // op meeting `ClipFate::Masked` routes to the shared tiny-skia mask
        // path, which is AA and is therefore clip-seeded like any other. On
        // CLIP_IR the sweep finds 34 of 120 splits differ at overscan 0, 19 at
        // 1, 6 at 2, and none from 3 up — i.e. Draft is band-exact on every
        // fixture that never pushes a ROUNDED clip, and not on the one that
        // does. The 0 stands (a gutter costs Draft its whole reason to exist:
        // 30,720 B of band pixmap becomes 46,848 B at overscan 4, plus the
        // demul/convert pass over it), and the gap is recorded as a `#[ignore]`d
        // test carrying these numbers rather than papered over — #41, held open
        // by `tests/fast_golden.rs::draft_rounded_clip_band_equivalence_known_gap`.
        let gutter = match quality {
            Quality::Exact => GUTTER,
            Quality::Fast => FAST_GUTTER,
            Quality::Draft => 0,
        };
        let pixmap = Pixmap::new(area.w + 2 * gutter, area.h + 2 * gutter)?;
        // Draft/Fast: the output surface IS this zeroed RGB888 band; the
        // backdrop fill (first op of every scene) covers it opaque before any
        // sampling. Exact: stays empty — the premul pixmap is its surface.
        let rgb = match quality {
            Quality::Exact => Vec::new(),
            Quality::Fast | Quality::Draft => {
                alloc::vec![0u8; area.w as usize * area.h as usize * 3]
            }
        };
        Some(Self {
            pixmap,
            rgb,
            area,
            quality,
            gutter,
            stats: RenderStats::default(),
            clips: Vec::new(),
            clip_bounds: Rect {
                x: 0,
                y: 0,
                w: 0,
                h: 0,
            },
            clip_mask: None,
            shapes: Shapes::new(),
        })
    }

    /// Move a caller-owned contour memo into this band's canvas (#32). Pair
    /// with [`Self::take_cache`] before the canvas is consumed — that round
    /// trip is what lets one cache serve every band and every frame.
    pub fn install_cache(&mut self, shapes: Shapes) {
        self.shapes = shapes;
    }

    /// Take the contour memo back out (leaving the canvas an empty one).
    pub fn take_cache(&mut self) -> Shapes {
        core::mem::take(&mut self.shapes)
    }

    /// [`circle_points`] through the memo (#32) — same arguments, same `f32`
    /// vertices, bit for bit.
    fn circle_pts(&mut self, cx: i32, cy: i32, r: f32) -> Vec<(f32, f32)> {
        self.shapes.fetch(
            Key::Circle {
                cx,
                cy,
                r: r.to_bits(),
            },
            || circle_points(cx, cy, r),
        )
    }

    /// [`rrect_points`] through the memo (#32). A radius-0 rect is FOUR
    /// corners and no trig at all, so it is not worth a cache slot — the
    /// budget belongs to the arcs.
    fn rrect_pts(&mut self, x: f32, y: f32, w: f32, h: f32, rad: f32) -> Vec<(f32, f32)> {
        if rad <= 0.0 {
            return rrect_points(x, y, w, h, rad);
        }
        self.shapes.fetch(
            Key::Rrect {
                x: x.to_bits(),
                y: y.to_bits(),
                w: w.to_bits(),
                h: h.to_bits(),
                rad: rad.to_bits(),
            },
            || rrect_points(x, y, w, h, rad),
        )
    }

    /// [`rrect_corner_points`] through the memo (#32) — the Fast tier's
    /// per-corner arc. The four integer cut lines are part of the identity
    /// (they place the lead-in/lead-out vertices), never elided.
    #[allow(clippy::too_many_arguments)] // mirrors `rrect_corner_points`
    fn corner_pts(
        &mut self,
        x: f32,
        y: f32,
        w: f32,
        h: f32,
        rad: f32,
        corner: usize,
        cx_l: i32,
        cx_r: i32,
        cy_t: i32,
        cy_b: i32,
    ) -> Vec<(f32, f32)> {
        self.shapes.fetch(
            Key::Corner {
                x: x.to_bits(),
                y: y.to_bits(),
                w: w.to_bits(),
                h: h.to_bits(),
                rad: rad.to_bits(),
                cuts: [cx_l, cx_r, cy_t, cy_b],
                corner: corner as u8,
            },
            || rrect_corner_points(x, y, w, h, rad, corner, cx_l, cx_r, cy_t, cy_b),
        )
    }

    /// This band's world-space rectangle (what the canvas will deliver) —
    /// the cull target for walk-level paint skipping.
    pub fn area(&self) -> Rect {
        self.area
    }

    /// Tiers that rasterize STRAIGHT-EDGED geometry as integer spans straight
    /// into the `rgb` band surface: `Draft` and `Fast` (#16, #27).
    fn integer_spans(&self) -> bool {
        matches!(self.quality, Quality::Fast | Quality::Draft)
    }

    /// Tiers that route CURVED geometry (disc / ring / rounded corners) back
    /// through the Exact tiny-skia polygon path so its edges are
    /// anti-aliased: `Fast` only (#27). `Draft` keeps curves integer and hard;
    /// `Exact` never asks (everything is already the AA path).
    fn aa_curves(&self) -> bool {
        self.quality == Quality::Fast
    }

    /// Whether a `fill_rrect`-family op actually HAS curved geometry — the
    /// single predicate that decides Fast's routing. `rad` is the *clamped*
    /// radius (see [`Self::clamp_radius`]), so a radius attr that the rect is
    /// too small to honour correctly reads as "no curve".
    fn curve_to_aa(&self, rad: f32) -> bool {
        self.aa_curves() && rad > 0.0
    }

    /// The band-local region a tiny-skia op must round-trip through the
    /// scratch pixmap when the output surface is `rgb`: op bbox ∩ (rect-only)
    /// clip bounds ∩ this band, in WORLD coordinates. `None` = nothing of this
    /// op lands in this band.
    fn rgb_scratch_region(&self, bbox: Rect) -> Option<(i32, i32, i32, i32)> {
        let mut wx0 = bbox.x;
        let mut wy0 = bbox.y;
        let mut wx1 = bbox.x + bbox.w as i32;
        let mut wy1 = bbox.y + bbox.h as i32;
        if !self.clips.is_empty() {
            let cb = self.clip_bounds;
            wx0 = wx0.max(cb.x);
            wy0 = wy0.max(cb.y);
            wx1 = wx1.min(cb.x + cb.w as i32);
            wy1 = wy1.min(cb.y + cb.h as i32);
        }
        wx0 = wx0.max(self.area.x);
        wy0 = wy0.max(self.area.y);
        wx1 = wx1.min(self.area.x + self.area.w as i32);
        wy1 = wy1.min(self.area.y + self.area.h as i32);
        (wx1 > wx0 && wy1 > wy0).then_some((wx0, wy0, wx1, wy1))
    }

    /// Copy the current opaque straight-RGB888 output (`rgb`) into the scratch
    /// pixmap over `region`, so a tiny-skia source-over lands on the SAME dst
    /// the Exact tier would have had. Dst is opaque by construction (the
    /// backdrop fill is the first op of every scene), so premul == straight
    /// and the seed is a plain widen.
    fn seed_scratch(&mut self, region: (i32, i32, i32, i32)) {
        let (wx0, wy0, wx1, wy1) = region;
        let aw = self.area.w as usize;
        let pm_w = self.pixmap.width() as usize;
        let g = self.gutter as i32;
        let (rx, ry) = (self.area.x, self.area.y);
        let px = self.pixmap.pixels_mut();
        for wy in wy0..wy1 {
            let prow = (wy - ry + g) as usize * pm_w + g as usize;
            let rrow = (wy - ry) as usize * aw * 3;
            for wx in wx0..wx1 {
                let lx = (wx - rx) as usize;
                let i3 = rrow + lx * 3;
                if let Some(p) = PremultipliedColorU8::from_rgba(
                    self.rgb[i3],
                    self.rgb[i3 + 1],
                    self.rgb[i3 + 2],
                    0xFF,
                ) {
                    px[prow + lx] = p;
                }
            }
        }
    }

    /// Demultiply the scratch pixmap's `region` straight back into `rgb`, in
    /// draw order (called the instant the op rasterizes, so a later fast-path
    /// span over these pixels still wins). Dst stayed opaque, so this is the
    /// identity demul `finish_into_rgb888` would have done.
    fn demul_scratch(&mut self, region: (i32, i32, i32, i32)) {
        let (wx0, wy0, wx1, wy1) = region;
        let aw = self.area.w as usize;
        let pm_w = self.pixmap.width() as usize;
        let g = self.gutter as i32;
        let (rx, ry) = (self.area.x, self.area.y);
        let px = self.pixmap.pixels();
        for wy in wy0..wy1 {
            let prow = (wy - ry + g) as usize * pm_w + g as usize;
            let rrow = (wy - ry) as usize * aw * 3;
            for wx in wx0..wx1 {
                let lx = (wx - rx) as usize;
                let p = px[prow + lx];
                let i3 = rrow + lx * 3;
                // Opaque dst is the ONLY case here (the scratch was seeded
                // opaque and source-over onto opaque stays opaque), and for
                // alpha 255 `demultiply()` is the identity — but it computes
                // it with three float multiply-round-converts per pixel, which
                // the profile put at 9.3 % of the Fast frame. Take the identity
                // directly; the general call stays for anything that is
                // somehow not opaque, so the bytes are unchanged either way.
                if p.alpha() == 0xFF {
                    self.rgb[i3] = p.red();
                    self.rgb[i3 + 1] = p.green();
                    self.rgb[i3 + 2] = p.blue();
                } else {
                    let d = p.demultiply();
                    self.rgb[i3] = d.red();
                    self.rgb[i3 + 1] = d.green();
                    self.rgb[i3 + 2] = d.blue();
                }
            }
        }
    }

    /// The exact-integer world → pixmap-local offset (gutter-relative; the
    /// gutter is [`GUTTER`] for Exact, 0 for Draft — see [`Self::gutter`]).
    fn off(&self) -> (f32, f32) {
        (
            (self.gutter as i32 - self.area.x) as f32,
            (self.gutter as i32 - self.area.y) as f32,
        )
    }

    /// Build a path from quantized-world contours, translated by the exact
    /// integer band offset. Contour orientation is the caller's contract
    /// (winding fill: opposite orientations cut holes).
    fn path_from(&self, contours: &[&[(f32, f32)]]) -> Option<tiny_skia::Path> {
        let (ox, oy) = self.off();
        let mut pb = PathBuilder::new();
        for pts in contours {
            let mut it = pts.iter();
            let &(x0, y0) = it.next()?;
            pb.move_to(x0 + ox, y0 + oy);
            for &(x, y) in it {
                pb.line_to(x + ox, y + oy);
            }
            pb.close();
        }
        pb.finish()
    }

    fn paint_for(color: Rgb, alpha: u8) -> Paint<'static> {
        let mut p = Paint::default();
        p.set_color_rgba8(color.r, color.g, color.b, alpha);
        p.anti_alias = true;
        p
    }

    /// Is `b` provably inside this clip entry's rounded interior?
    /// Exact integer math: containment in the rect (flat clip edges lie on
    /// integer pixel boundaries — full coverage right up to them, no inset
    /// needed), plus a corner-arc test with `M` px of margin clearing the
    /// arc's AA + flattening fringe (≤ ~1.1 px worst case; 2 px is safely
    /// past it). Conservative failures only ever route an op to the masked
    /// path, never let one escape the clip.
    fn clip_contains(clip: &Clip, b: Rect) -> bool {
        const M: i64 = 2;
        let (bx0, by0) = (b.x as i64, b.y as i64);
        let (bx1, by1) = (b.x as i64 + b.w as i64, b.y as i64 + b.h as i64);
        let (cx0, cy0) = (clip.rect.x as i64, clip.rect.y as i64);
        let (cx1, cy1) = (cx0 + clip.rect.w as i64, cy0 + clip.rect.h as i64);
        if bx0 < cx0 || by0 < cy0 || bx1 > cx1 || by1 > cy1 {
            return false;
        }
        let rad = Self::clamp_radius(clip.rect, clip.radius) as i64;
        if rad == 0 {
            return true;
        }
        let r = rad - M; // inner radius the bbox corner must stay within
        // (corner-square overlap test, farthest-point distance per corner)
        let corners = [
            (
                bx0 < cx0 + rad && by0 < cy0 + rad,
                cx0 + rad - bx0,
                cy0 + rad - by0,
            ), // TL
            (
                bx1 > cx1 - rad && by0 < cy0 + rad,
                bx1 - (cx1 - rad),
                cy0 + rad - by0,
            ), // TR
            (
                bx1 > cx1 - rad && by1 > cy1 - rad,
                bx1 - (cx1 - rad),
                by1 - (cy1 - rad),
            ), // BR
            (
                bx0 < cx0 + rad && by1 > cy1 - rad,
                cx0 + rad - bx0,
                by1 - (cy1 - rad),
            ), // BL
        ];
        for (in_square, dx, dy) in corners {
            if in_square && (r < 0 || dx * dx + dy * dy > r * r) {
                return false;
            }
        }
        true
    }

    /// Decide an op's fate under the active clip stack — WORLD-space data
    /// only (band-invariant; module docs). `bbox` is the op's conservative
    /// geometry hull; +1 px here absorbs the op's own AA/quantization spill
    /// (conservative in BOTH directions: harder to skip, harder to claim
    /// containment).
    fn op_clip(&mut self, bbox: Rect) -> ClipFate {
        if self.clips.is_empty() {
            return ClipFate::Unclipped;
        }
        let b = bbox.inflate(1);
        if self.clip_bounds.is_empty() || b.intersect(self.clip_bounds).is_empty() {
            return ClipFate::Skip;
        }
        if self.clips.iter().all(|c| Self::clip_contains(c, b)) {
            return ClipFate::Unclipped;
        }
        if self.clips.iter().all(|c| c.radius == 0) {
            return ClipFate::RectSpans(self.clip_bounds);
        }
        self.ensure_mask();
        ClipFate::Masked
    }

    /// [`Self::op_clip`] for tiny-skia path fills, which cannot span-clamp:
    /// a rect-only overflow draws through the mask too (an axis-aligned
    /// integer rect rasterizes to a hard 0/255 mask — still exact).
    fn op_clip_fill(&mut self, bbox: Rect) -> ClipFate {
        match self.op_clip(bbox) {
            ClipFate::RectSpans(_) => {
                self.ensure_mask();
                ClipFate::Masked
            }
            fate => fate,
        }
    }

    /// Build (lazily) the band's clip mask: each entry's rounded rect goes
    /// through the SAME deterministic pipeline as paint geometry —
    /// [`rrect_points`] fixed-step flattening, 1/64-px world quantization,
    /// exact-integer band translation — never a tiny-skia curve, so the
    /// mask bytes per WORLD pixel are band-invariant. Entries intersect via
    /// `Mask::intersect_path` (exact per-pixel `round(a·b/255)`).
    fn ensure_mask(&mut self) {
        if self.clip_mask.is_some() {
            return;
        }
        let mut mask = Mask::new(self.pixmap.width(), self.pixmap.height())
            .expect("mask allocation (pixmap dimensions are valid by construction)");
        let mut first = true;
        // Indexed, over a COPY of the entry: the memo lookup below needs
        // `&mut self`, which an iterator borrowing `self.clips` would deny.
        // `Clip` is `Copy` and the stack is short (nesting depth), so this is
        // the same work with a friendlier borrow shape.
        for i in 0..self.clips.len() {
            let clip = self.clips[i];
            let rad = Self::clamp_radius(clip.rect, clip.radius);
            let pts = self.rrect_pts(
                clip.rect.x as f32,
                clip.rect.y as f32,
                clip.rect.w as f32,
                clip.rect.h as f32,
                rad,
            );
            let (ox, oy) = (
                (self.gutter as i32 - self.area.x) as f32,
                (self.gutter as i32 - self.area.y) as f32,
            );
            let mut pb = PathBuilder::new();
            let mut it = pts.iter();
            if let Some(&(x0, y0)) = it.next() {
                pb.move_to(x0 + ox, y0 + oy);
                for &(x, y) in it {
                    pb.line_to(x + ox, y + oy);
                }
                pb.close();
            }
            if let Some(path) = pb.finish() {
                if first {
                    mask.fill_path(&path, FillRule::Winding, true, Transform::identity());
                    first = false;
                } else {
                    mask.intersect_path(&path, FillRule::Winding, true, Transform::identity());
                }
            } else {
                // Degenerate clip rect (zero-area): nothing may draw. A
                // zeroed mask says exactly that.
                mask.clear();
                first = false;
            }
        }
        self.clip_mask = Some(mask);
    }

    /// Draft fast path for an OPAQUE AXIS-ALIGNED rect (radius 0, alpha 255):
    /// write premultiplied pixels directly into the band pixmap, one integer
    /// span per row — no path, no coverage, no tiny-skia (struct docs, F16).
    ///
    /// Band-exact within the Draft tier by the same induction as the
    /// glyph/image blits: the written region is (world rect ∩ band ∩
    /// rect-only clip), addressed with the exact-integer world→gutter-local
    /// offset, so every band that touches a world pixel writes the same byte.
    /// Returns `true` if it handled the op; `false` means the caller must use
    /// the Exact path (a rounded clip overlaps → `ClipFate::Masked`; the
    /// fast path declines so the clip stays exact).
    fn fill_rrect_draft(&mut self, r: Rect, color: Rgb, _alpha: u8) -> bool {
        // Fate under the active clip — world-space verdict (band-invariant).
        match self.op_clip(r) {
            ClipFate::Skip => return true,    // nothing to draw, op handled
            ClipFate::Masked => return false, // rounded clip → let Exact run
            ClipFate::Unclipped => {}
            ClipFate::RectSpans(_) => {} // rect-only clip: intersect below
        }
        // World-space draw rect = the op rect ∩ all rect-only clip entries
        // (clip_bounds is exactly that intersection when every entry is a
        // rect, which Masked already excluded). Intersect in world coords.
        let mut wx0 = r.x;
        let mut wy0 = r.y;
        let mut wx1 = r.x + r.w as i32;
        let mut wy1 = r.y + r.h as i32;
        if !self.clips.is_empty() {
            let cb = self.clip_bounds;
            wx0 = wx0.max(cb.x);
            wy0 = wy0.max(cb.y);
            wx1 = wx1.min(cb.x + cb.w as i32);
            wy1 = wy1.min(cb.y + cb.h as i32);
        }
        // Clamp to this band (the outermost clip, I1).
        wx0 = wx0.max(self.area.x);
        wy0 = wy0.max(self.area.y);
        wx1 = wx1.min(self.area.x + self.area.w as i32);
        wy1 = wy1.min(self.area.y + self.area.h as i32);
        if wx1 <= wx0 || wy1 <= wy0 {
            return true; // fully outside this band — op handled, no pixels
        }
        // Opaque source-over of an opaque colour = a plain store of the
        // colour. Draft writes straight RGB888 into the output band directly
        // (the surface IS rgb); the [w*3]-byte triple-pattern slice fill is
        // byte-identical to the old premul-store-then-demul (alpha 255 ⇒
        // premul == straight, demul is identity). One store per pixel.
        let aw = self.area.w as usize;
        let (rx, ry) = (self.area.x, self.area.y);
        let trip = [color.r, color.g, color.b];
        for wy in wy0..wy1 {
            let row = ((wy - ry) as usize * aw) * 3;
            let c0 = (wx0 - rx) as usize * 3;
            let c1 = (wx1 - rx) as usize * 3;
            fill_rgb_triple(&mut self.rgb[row + c0..row + c1], trip);
        }
        // Exact span attribution — these pixels took the fast path.
        let drawn = (wx1 - wx0) as u64 * (wy1 - wy0) as u64;
        self.stats.pixels_written += drawn;
        self.stats.pixels_by_class[OpClass::OpaqueFill as usize] += drawn;
        self.stats.fastpath_pixels += drawn;
        true
    }

    /// The integer `[sx0, sx1)` x-span of a rounded rect (`r`, corner radius
    /// `rad`) at world row `wy`, or `None` if the row is OUTSIDE the rounded
    /// shape (clipped off by a corner arc). The corner ends use the SAME integer
    /// circle test the disc uses (`dx = isqrt(rad² − dy²)`), so corners are
    /// hard-edged quarter-discs matching `disc_draft`; middle rows are full
    /// width. A pure INTEGER function of WORLD position — band-invariant — so
    /// every Draft op built on it (fill / gradient / stroke) is band-exact by
    /// the same induction as the fills. `rad == 0` ⇒ always the full rect span.
    fn rrect_row_span(r: Rect, rad: i32, wy: i32) -> Option<(i32, i32)> {
        let (rx0, rx1) = (r.x, r.x + r.w as i32);
        if rad <= 0 {
            return Some((rx0, rx1));
        }
        let ccy_t = r.y + rad; // first full-width row from the top
        let ccy_b = r.y + r.h as i32 - rad; // first bottom-arc row
        // dy into the rect from the nearest corner centre (0 in the middle).
        let dy = if wy < ccy_t {
            (ccy_t - wy) as i64
        } else if wy >= ccy_b {
            (wy - ccy_b + 1) as i64
        } else {
            0
        };
        if dy == 0 {
            return Some((rx0, rx1));
        }
        let rem = (rad as i64) * (rad as i64) - dy * dy;
        if rem < 0 {
            return None; // beyond the arc on this row
        }
        let dx = isqrt_i64(rem) as i32;
        // Inset each end by (rad - dx): dy=0 → inset 0 (full width), dy=rad →
        // inset rad (span shrinks to [x+rad, x+w-rad)).
        Some((rx0 + rad - dx, rx1 - rad + dx))
    }

    /// Draft fast path for a ROUNDED OPAQUE-or-TRANSLUCENT `fill_rrect`
    /// (`rad > 0`): an integer rounded-rect fill via [`rrect_row_span`] per
    /// row, written through [`draft_span`] (opaque store or d255 source-over).
    /// No AA, no tiny-skia. Band-exact by the same induction as the fills.
    /// Returns `true` if it handled the op; `false` (rounded clip overlap →
    /// `ClipFate::Masked`) routes to Exact.
    fn fill_rrect_rounded_draft(&mut self, r: Rect, rad: u32, color: Rgb, alpha: u8) -> bool {
        match self.op_clip(r) {
            ClipFate::Skip => return true,
            ClipFate::Masked => return false,
            ClipFate::Unclipped | ClipFate::RectSpans(_) => {}
        }
        let Some((cx0, cy0, cx1, cy1)) = self.draft_world_clamp(r) else {
            return true;
        };
        let rad = rad as i32;
        let mut drawn = 0u64;
        for wy in cy0..cy1 {
            if let Some((sx0, sx1)) = Self::rrect_row_span(r, rad, wy) {
                drawn += self.draft_span(wy, sx0, sx1, cx0, cx1, color, alpha);
            }
        }
        self.draft_tally(Self::class_for(alpha), drawn);
        true
    }

    /// Draft fast path for a ROUNDED [`Canvas::stroke_rrect`]: an integer
    /// rounded-rect OUTLINE. Same CENTRED-stroke contract as the Exact path —
    /// the stroke straddles the contour of `r` (corner radius `rad`): the OUTER
    /// edge is `r` inflated by `⌈width/2⌉`, the INNER edge `r` deflated by
    /// `⌊width/2⌋`. The annulus = outer rounded-rect spans minus inner
    /// rounded-rect spans per scanline (two side bands where the row crosses the
    /// hole, one full span on the caps). Both contours use [`rrect_row_span`]
    /// (the disc-consistent corner carve), so the stroke is band-exact like the
    /// fills. Opaque only on the fast path (the IR's borders are opaque).
    /// Returns `true` if it handled the op; `false` (translucent or rounded clip
    /// overlap) routes to Exact.
    fn stroke_rrect_rounded_draft(
        &mut self,
        r: Rect,
        rad: u32,
        width: u32,
        color: Rgb,
        alpha: u8,
    ) -> bool {
        if alpha != 0xFF {
            return false;
        }
        let half_out = width.div_ceil(2) as i32; // ⌈w/2⌉
        let half_in = (width / 2) as i32; // ⌊w/2⌋
        // Outer contour: r inflated by ⌈w/2⌉ on every side, radius rad+⌈w/2⌉.
        let outer = Rect {
            x: r.x - half_out,
            y: r.y - half_out,
            w: r.w + 2 * half_out as u32,
            h: r.h + 2 * half_out as u32,
        };
        let outer_rad = rad as i32 + half_out;
        // Inner contour: r deflated by ⌊w/2⌋, radius rad−⌊w/2⌋ (clamped ≥ 0).
        let inner = Rect {
            x: r.x + half_in,
            y: r.y + half_in,
            w: r.w.saturating_sub(2 * half_in as u32),
            h: r.h.saturating_sub(2 * half_in as u32),
        };
        let inner_rad = (rad as i32 - half_in).max(0);
        let inner_has_area = inner.w > 0 && inner.h > 0;
        match self.op_clip(outer) {
            ClipFate::Skip => return true,
            ClipFate::Masked => return false,
            ClipFate::Unclipped | ClipFate::RectSpans(_) => {}
        }
        let Some((cx0, cy0, cx1, cy1)) = self.draft_world_clamp(outer) else {
            return true;
        };
        let mut drawn = 0u64;
        for wy in cy0..cy1 {
            let Some((ox0, ox1)) = Self::rrect_row_span(outer, outer_rad, wy) else {
                continue; // outside the outer shape entirely
            };
            // The inner hole on this row, if the row crosses it.
            let hole = if inner_has_area && wy >= inner.y && wy < inner.y + inner.h as i32 {
                Self::rrect_row_span(inner, inner_rad, wy)
            } else {
                None
            };
            match hole {
                Some((ix0, ix1)) => {
                    // Left band [ox0, ix0) and right band [ix1, ox1).
                    drawn += self.draft_span(wy, ox0, ix0, cx0, cx1, color, alpha);
                    drawn += self.draft_span(wy, ix1, ox1, cx0, cx1, color, alpha);
                }
                None => {
                    drawn += self.draft_span(wy, ox0, ox1, cx0, cx1, color, alpha);
                }
            }
        }
        self.draft_tally(Self::class_for(alpha), drawn);
        true
    }

    /// Draft fast path for [`Canvas::fill_linear_gradient`]: an integer
    /// two-stop linear ramp filled into a (rounded) rect, no AA, no tiny-skia.
    /// Per output pixel the colour is `round((1−t)·from + t·to)` with `t` a
    /// FIXED-POINT ramp across the rect (`t = clamp(p·256/extent, 0..256)` for
    /// `p` the pixel's offset along the gradient axis from the rect origin) —
    /// a pure INTEGER function of world position, so band-exact by the same
    /// induction as the fills. The (rounded) shape reuses the same corner-arc
    /// span carving as [`fill_rrect_rounded_draft`]. Opaque only on the fast
    /// path (the IR's gradient backgrounds are opaque); translucent gradients
    /// decline to Exact. Returns `true` if it handled the op.
    #[allow(clippy::too_many_arguments)]
    fn fill_gradient_draft(
        &mut self,
        r: Rect,
        rad: u32,
        from: Rgb,
        to: Rgb,
        vertical: bool,
        alpha: u8,
    ) -> bool {
        if alpha != 0xFF {
            return false; // translucent gradient → Exact (rare; keep it exact)
        }
        match self.op_clip(r) {
            ClipFate::Skip => return true,
            ClipFate::Masked => return false,
            ClipFate::Unclipped | ClipFate::RectSpans(_) => {}
        }
        let Some((cx0, cy0, cx1, cy1)) = self.draft_world_clamp(r) else {
            return true;
        };
        let rad = rad as i32;
        // Gradient extent along the axis (px); guard the degenerate 0.
        let extent = if vertical { r.h as i64 } else { r.w as i64 }.max(1);
        let aw = self.area.w as usize;
        let rx = self.area.x;
        let mut drawn = 0u64;
        for wy in cy0..cy1 {
            let Some((sx0, sx1)) = Self::rrect_row_span(r, rad, wy) else {
                continue;
            };
            let lo = sx0.max(cx0);
            let hi = sx1.min(cx1);
            if hi <= lo {
                continue;
            }
            // Draft surface IS the straight-RGB888 band; gradient stops are
            // opaque (translucent declined above) so a plain store, no demul.
            let row = ((wy - self.area.y) as usize * aw) * 3;
            // Vertical gradients have one colour per row — compute t once.
            let t_row = if vertical {
                Some((((wy - r.y) as i64 * 256 / extent).clamp(0, 256)) as u32)
            } else {
                None
            };
            for wx in lo..hi {
                let t = match t_row {
                    Some(t) => t,
                    None => (((wx - r.x) as i64 * 256 / extent).clamp(0, 256)) as u32,
                };
                let it = 256 - t;
                // round((1-t)·from + t·to) at 1/256 fixed point: (a·it + b·t + 128) >> 8.
                let mix =
                    |a: u8, b: u8| -> u8 { ((a as u32 * it + b as u32 * t + 128) >> 8) as u8 };
                let (cr, cg, cb) = (mix(from.r, to.r), mix(from.g, to.g), mix(from.b, to.b));
                let i = row + (wx - rx) as usize * 3;
                self.rgb[i] = cr;
                self.rgb[i + 1] = cg;
                self.rgb[i + 2] = cb;
            }
            drawn += (hi - lo) as u64;
        }
        self.draft_tally(Self::class_for(alpha), drawn);
        true
    }

    /// World-space x-range clamp for the Draft curve span writers: the op's
    /// world bbox ∩ (rect-only) clip bounds ∩ the band, as integer world
    /// columns. Returns `None` if the op cannot touch this band at all.
    /// Mirrors [`fill_rrect_draft`]'s clamp so every Draft op clips identically.
    fn draft_world_clamp(&self, bbox: Rect) -> Option<(i32, i32, i32, i32)> {
        let mut wx0 = bbox.x;
        let mut wy0 = bbox.y;
        let mut wx1 = bbox.x + bbox.w as i32;
        let mut wy1 = bbox.y + bbox.h as i32;
        if !self.clips.is_empty() {
            let cb = self.clip_bounds;
            wx0 = wx0.max(cb.x);
            wy0 = wy0.max(cb.y);
            wx1 = wx1.min(cb.x + cb.w as i32);
            wy1 = wy1.min(cb.y + cb.h as i32);
        }
        wx0 = wx0.max(self.area.x);
        wy0 = wy0.max(self.area.y);
        wx1 = wx1.min(self.area.x + self.area.w as i32);
        wy1 = wy1.min(self.area.y + self.area.h as i32);
        if wx1 <= wx0 || wy1 <= wy0 {
            return None;
        }
        Some((wx0, wy0, wx1, wy1))
    }

    /// Write one INTEGER world-space span `[sx0, sx1)` on world row `wy` with
    /// `color`/`alpha`, clamped to `[cx0, cx1)` (the band ∩ clip x-range), and
    /// count the written pixels as fast-path. The pixel write is the same
    /// integer source-over the glyph/image blits use (inline below):
    /// an OPAQUE pixel is a plain premultiplied store, a TRANSLUCENT one the
    /// d255 blend over existing dst — band-invariant either way (pure function
    /// of world position + existing dst). Caller has range-clamped `wy` to the
    /// band already; `cy_local = wy + oyi`. Returns pixels written.
    #[allow(clippy::too_many_arguments)]
    fn draft_span(
        &mut self,
        wy: i32,
        sx0: i32,
        sx1: i32,
        cx0: i32,
        cx1: i32,
        color: Rgb,
        alpha: u8,
    ) -> u64 {
        let lo = sx0.max(cx0);
        let hi = sx1.min(cx1);
        if hi <= lo {
            return 0;
        }
        // Draft writes straight RGB888 into the output band directly. Dst is
        // always opaque RGB (the backdrop fills it first), so the d255 blend
        // below is byte-identical to the old premul-store→demul: premul ==
        // straight, na == 255 (demul identity).
        let aw = self.area.w as usize;
        let row = ((wy - self.area.y) as usize * aw) * 3;
        let (lc, rx) = (lo, self.area.x);
        if alpha == 0xFF {
            // Opaque source-over = a plain store of the colour (triple fill).
            let trip = [color.r, color.g, color.b];
            let c0 = (lc - rx) as usize * 3;
            let c1 = (hi - rx) as usize * 3;
            fill_rgb_triple(&mut self.rgb[row + c0..row + c1], trip);
        } else {
            // Translucent: integer d255 source-over per pixel over straight
            // opaque dst (the glyph-blit formula with full coverage; a = paint
            // alpha). na == 255 always, so only the RGB channels are stored.
            let a = alpha as u32;
            let ia = 255 - a;
            for wx in lo..hi {
                let i = row + (wx - rx) as usize * 3;
                let nr = d255(color.r as u32 * a + self.rgb[i] as u32 * ia);
                let ng = d255(color.g as u32 * a + self.rgb[i + 1] as u32 * ia);
                let nb = d255(color.b as u32 * a + self.rgb[i + 2] as u32 * ia);
                self.rgb[i] = nr as u8;
                self.rgb[i + 1] = ng as u8;
                self.rgb[i + 2] = nb as u8;
            }
        }
        (hi - lo) as u64
    }

    /// Tally `drawn` fast-path pixels of class `class`.
    fn draft_tally(&mut self, class: OpClass, drawn: u64) {
        self.stats.pixels_written += drawn;
        self.stats.pixels_by_class[class as usize] += drawn;
        self.stats.fastpath_pixels += drawn;
    }

    /// Draft fast path for [`Canvas::disc`]: an integer filled circle. Per
    /// world row the half-width is `dx = isqrt(r² − dy²)` (exact integer), the
    /// span `[cx−dx, cx+dx+1)`. Returns `true` if it handled the op; `false`
    /// (rounded clip overlap → `ClipFate::Masked`) routes the caller to Exact.
    fn disc_draft(&mut self, cx: i32, cy: i32, radius: u32, color: Rgb, alpha: u8) -> bool {
        let r = radius as i32;
        let bbox = Rect {
            x: cx - r,
            y: cy - r,
            w: radius * 2,
            h: radius * 2,
        };
        match self.op_clip(bbox) {
            ClipFate::Skip => return true,
            ClipFate::Masked => return false,
            ClipFate::Unclipped | ClipFate::RectSpans(_) => {}
        }
        let Some((cx0, cy0, cx1, cy1)) = self.draft_world_clamp(bbox) else {
            return true;
        };
        let r2 = (r as i64) * (r as i64);
        let mut drawn = 0u64;
        for wy in cy0..cy1 {
            let dy = (wy - cy) as i64;
            let rem = r2 - dy * dy;
            if rem < 0 {
                continue;
            }
            let dx = isqrt_i64(rem) as i32;
            drawn += self.draft_span(wy, cx - dx, cx + dx + 1, cx0, cx1, color, alpha);
        }
        self.draft_tally(Self::class_for(alpha), drawn);
        true
    }

    /// Draft fast path for [`Canvas::ring`]: an integer annulus. Per world row,
    /// the outer span `[cx−dxo, cx+dxo]` minus the inner span `[cx−dxi, cx+dxi]`
    /// — two spans where the hole is open on that row, one where it isn't.
    /// Integer radii from the same centreline+width the Exact path uses (outer
    /// `r+⌈w/2⌉`, inner `r−⌊w/2⌋` so width is preserved as an integer band).
    /// Returns `false` (rounded clip overlap) to route the caller to Exact.
    fn ring_draft(
        &mut self,
        cx: i32,
        cy: i32,
        radius: u32,
        width: u32,
        color: Rgb,
        alpha: u8,
    ) -> bool {
        let ro = radius as i32 + width.div_ceil(2) as i32;
        let ri = radius as i32 - (width / 2) as i32;
        let bbox = Rect {
            x: cx - ro,
            y: cy - ro,
            w: (ro as u32) * 2,
            h: (ro as u32) * 2,
        };
        match self.op_clip(bbox) {
            ClipFate::Skip => return true,
            ClipFate::Masked => return false,
            ClipFate::Unclipped | ClipFate::RectSpans(_) => {}
        }
        let Some((cx0, cy0, cx1, cy1)) = self.draft_world_clamp(bbox) else {
            return true;
        };
        let ro2 = (ro as i64) * (ro as i64);
        let ri2 = (ri.max(0) as i64) * (ri.max(0) as i64);
        let has_hole = ri > 0;
        let mut drawn = 0u64;
        for wy in cy0..cy1 {
            let dy = (wy - cy) as i64;
            let remo = ro2 - dy * dy;
            if remo < 0 {
                continue;
            }
            let dxo = isqrt_i64(remo) as i32;
            // Inner span exists only where the row crosses the hole.
            let remi = ri2 - dy * dy;
            if has_hole && remi >= 0 {
                let dxi = isqrt_i64(remi) as i32;
                // Left band [cx−dxo, cx−dxi) and right band (cx+dxi, cx+dxo].
                drawn += self.draft_span(wy, cx - dxo, cx - dxi, cx0, cx1, color, alpha);
                drawn += self.draft_span(wy, cx + dxi + 1, cx + dxo + 1, cx0, cx1, color, alpha);
            } else {
                drawn += self.draft_span(wy, cx - dxo, cx + dxo + 1, cx0, cx1, color, alpha);
            }
        }
        self.draft_tally(Self::class_for(alpha), drawn);
        true
    }

    /// Draft fast path for [`Canvas::line`]: the SAME butt-capped quad the
    /// Exact path builds (perpendicular half-width offset of both endpoints),
    /// rasterized as integer per-row spans. The quad's left and right x at a
    /// given world row come from its edges in 1/256-px FIXED POINT — a pure
    /// integer function of the world row (band-invariant). Pixel-centre rule:
    /// a pixel is in the span iff its centre x lies in the polygon span at its
    /// row centre, matching a no-AA scanline fill. Returns `false` (rounded
    /// clip overlap) to route the caller to Exact; `true` otherwise (incl. the
    /// honest zero-length no-op).
    #[allow(clippy::too_many_arguments)]
    fn line_draft(
        &mut self,
        x0: i32,
        y0: i32,
        x1: i32,
        y1: i32,
        width: u32,
        color: Rgb,
        alpha: u8,
    ) -> bool {
        let (fx0, fy0, fx1, fy1) = (x0 as f32, y0 as f32, x1 as f32, y1 as f32);
        let (dx, dy) = (fx1 - fx0, fy1 - fy0);
        let len = libm::sqrtf(dx * dx + dy * dy);
        if len <= 0.0 {
            return true; // zero-length: honest no-op (no direction to stroke)
        }
        let pad = width.div_ceil(2) as i32;
        let bbox = Rect {
            x: x0.min(x1) - pad,
            y: y0.min(y1) - pad,
            w: (x0.max(x1) - x0.min(x1)) as u32 + width,
            h: (y0.max(y1) - y0.min(y1)) as u32 + width,
        };
        match self.op_clip(bbox) {
            ClipFate::Skip => return true,
            ClipFate::Masked => return false,
            ClipFate::Unclipped | ClipFate::RectSpans(_) => {}
        }
        let Some((cx0, cy0, cx1, cy1)) = self.draft_world_clamp(bbox) else {
            return true;
        };
        // The quad corners (world space), same construction as the Exact line.
        let w2 = width as f32 / 2.0;
        let (px, py) = (-dy / len * w2, dx / len * w2);
        // 1/256-px fixed-point vertices — exact-integer per-band positioning of
        // the same world geometry (q() quantizes to 1/64; 1/256 here is finer
        // and still exact for our coordinate range).
        let v: [(i64, i64); 4] = [
            (fp(fx0 + px), fp(fy0 + py)),
            (fp(fx1 + px), fp(fy1 + py)),
            (fp(fx1 - px), fp(fy1 - py)),
            (fp(fx0 - px), fp(fy0 - py)),
        ];
        let mut drawn = 0u64;
        for wy in cy0..cy1 {
            // Sample the polygon at the row's pixel-centre y, in fixed point.
            let yc = (wy as i64) * 256 + 128;
            // Gather edge crossings of the scanline y = yc.
            let mut xmin = i64::MAX;
            let mut xmax = i64::MIN;
            let mut hits = 0;
            for e in 0..4 {
                let (ax, ay) = v[e];
                let (bx, by) = v[(e + 1) % 4];
                if (ay <= yc && by > yc) || (by <= yc && ay > yc) {
                    // Crossing x in fixed point: ax + (yc-ay)*(bx-ax)/(by-ay).
                    let t_num = (yc - ay) * (bx - ax);
                    let xc = ax + t_num / (by - ay);
                    xmin = xmin.min(xc);
                    xmax = xmax.max(xc);
                    hits += 1;
                }
            }
            if hits < 2 {
                continue;
            }
            // Pixel-centre rule: column wx is inside iff its centre
            // (wx*256+128) lies in [xmin, xmax]. Solve for integer wx range.
            // wx*256+128 >= xmin  ⇒ wx >= ceil((xmin-128)/256)
            // wx*256+128 <= xmax  ⇒ wx <= floor((xmax-128)/256)
            let sx0 = div_floor(xmin - 128 + 255, 256); // ceil
            let sx1 = div_floor(xmax - 128, 256) + 1; // exclusive upper
            drawn += self.draft_span(wy, sx0 as i32, sx1 as i32, cx0, cx1, color, alpha);
        }
        self.draft_tally(Self::class_for(alpha), drawn);
        true
    }

    fn fill(&mut self, contours: &[&[(f32, f32)]], color: Rgb, alpha: u8, bbox: Rect) {
        self.fill_into(contours, color, alpha, bbox, &[bbox]);
    }

    /// **The Fast tier's rounded-rect (#27).** Handing tiny-skia the WHOLE
    /// rounded rect is what made the first cut of this tier *slower than
    /// Exact*: a 456x44 header frame with radius 8 has 256 px of actual curve
    /// and 19,808 px of flat, and the naive route paid AA rasterization plus a
    /// scratch round-trip over all 20,064.
    ///
    /// So the shape is cut at INTEGER scanlines and columns into four corner
    /// squares (the only curved parts, AA'd through the Exact polygon path) and
    /// a straight remainder (integer spans). The cuts are:
    ///
    /// ```text
    ///        x        cx_l                 cx_r      x+w
    ///   y    +--AA----+--------spans--------+---AA---+
    ///   cy_t +--------+                     +--------+
    ///        |            spans (full width)         |
    ///   cy_b +--------+                     +--------+
    ///        +--AA----+--------spans--------+---AA---+   y+h
    /// ```
    ///
    /// `cx_l = ceil(x+rad)`, `cx_r = floor(x+w-rad)`, and likewise vertically.
    /// Because `rad <= cx_l - x`, every row of the rect is FULLY covered across
    /// `[cx_l, cx_r)` — the flat top/bottom starts at `x+rad` — so those spans
    /// are exact, not an approximation. The corner contour is the SAME arc
    /// `rrect_points` emits (same step rule, same 1/64-px quantization), closed
    /// against the two integer cut lines; a cut on an integer coordinate cannot
    /// change the coverage of any pixel on the curved side of it.
    ///
    /// Returns `false` (→ whole-shape AA) for a rounded clip overlap, and for
    /// the degenerate case where the cuts cross (`rad` == half an ODD extent),
    /// which would make two corner squares overlap and double-blend a
    /// translucent fill.
    fn fill_rrect_fast(&mut self, r: Rect, rad: f32, color: Rgb, alpha: u8) -> bool {
        match self.op_clip(r) {
            ClipFate::Skip => return true,
            ClipFate::Masked => return false,
            ClipFate::Unclipped | ClipFate::RectSpans(_) => {}
        }
        let (fx, fy) = (r.x as f32, r.y as f32);
        let (fw, fh) = (r.w as f32, r.h as f32);
        let cx_l = libm::ceilf(fx + rad) as i32;
        let cx_r = libm::floorf(fx + fw - rad) as i32;
        let cy_t = libm::ceilf(fy + rad) as i32;
        let cy_b = libm::floorf(fy + fh - rad) as i32;
        if cx_l > cx_r || cy_t > cy_b {
            return false;
        }
        let (x1, y1) = (r.x + r.w as i32, r.y + r.h as i32);
        // 1) The straight remainder, as Draft's integer spans.
        if let Some((bx0, by0, bx1, by1)) = self.draft_world_clamp(r) {
            let mut drawn = 0u64;
            for wy in by0..by1 {
                let (sx0, sx1) = if wy < cy_t || wy >= cy_b {
                    (cx_l, cx_r)
                } else {
                    (r.x, x1)
                };
                drawn += self.draft_span(wy, sx0, sx1, bx0, bx1, color, alpha);
            }
            self.draft_tally(Self::class_for(alpha), drawn);
        }
        // 2) The four curved corners, through the Exact AA path.
        let corners = [
            (r.x, r.y, cx_l, cy_t),
            (cx_r, r.y, x1, cy_t),
            (cx_r, cy_b, x1, y1),
            (r.x, cy_b, cx_l, y1),
        ];
        for (i, (qx0, qy0, qx1, qy1)) in corners.into_iter().enumerate() {
            if qx1 <= qx0 || qy1 <= qy0 {
                continue;
            }
            let bbox = Rect {
                x: qx0,
                y: qy0,
                w: (qx1 - qx0) as u32,
                h: (qy1 - qy0) as u32,
            };
            let pts = self.corner_pts(fx, fy, fw, fh, rad, i, cx_l, cx_r, cy_t, cy_b);
            self.fill(&[&pts], color, alpha, bbox);
            self.count(Self::class_for(alpha), bbox);
        }
        true
    }

    /// The world rects a `frame`-shaped op (a stroke, a ring) can actually ink:
    /// `outer` minus `hole`, as up to four disjoint strips. `hole` must be a
    /// rect the shape provably never touches, shrunk by an outward margin so no
    /// AA fringe escapes. Feeds [`Self::fill_into`]'s `carry`.
    fn frame_strips(outer: Rect, hole: Rect) -> [Rect; 4] {
        let (ox0, oy0) = (outer.x, outer.y);
        let (ox1, oy1) = (outer.x + outer.w as i32, outer.y + outer.h as i32);
        let hx0 = hole.x.clamp(ox0, ox1);
        let hy0 = hole.y.clamp(oy0, oy1);
        let hx1 = (hole.x + hole.w as i32).clamp(hx0, ox1);
        let hy1 = (hole.y + hole.h as i32).clamp(hy0, oy1);
        let mk = |x0: i32, y0: i32, x1: i32, y1: i32| Rect {
            x: x0,
            y: y0,
            w: (x1 - x0).max(0) as u32,
            h: (y1 - y0).max(0) as u32,
        };
        [
            mk(ox0, oy0, ox1, hy0), // top strip, full width
            mk(ox0, hy1, ox1, oy1), // bottom strip, full width
            mk(ox0, hy0, hx0, hy1), // left strip, between them
            mk(hx1, hy0, ox1, hy1), // right strip
        ]
    }

    /// [`Self::fill`], but the Draft/Fast scratch round-trip touches only
    /// `carry` — a set of DISJOINT world rects that must cover every pixel the
    /// path can ink. tiny-skia still rasterizes the whole path into the scratch
    /// pixmap (cheap: it blits only covered spans); what `carry` controls is
    /// the seed+demul, which is per-PIXEL-of-region and therefore the part
    /// worth trimming. A ring's hole and a frame stroke's interior ink nothing,
    /// so paying for them is pure waste. Under-covering `carry` would silently
    /// drop AA fringe — every caller derives it from the shape's own geometry
    /// with an outward margin, never from a guess.
    fn fill_into(
        &mut self,
        contours: &[&[(f32, f32)]],
        color: Rgb,
        alpha: u8,
        bbox: Rect,
        carry: &[Rect],
    ) {
        let fate = self.op_clip_fill(bbox);
        if fate == ClipFate::Skip {
            return;
        }
        // Draft/Fast: the output surface is `rgb`, but the AA polygon + clip
        // mask must go through tiny-skia. To stay BYTE-identical to the premul
        // path we reproduce its exact arithmetic: SEED the op's band region of
        // the scratch `pixmap` with the current opaque-RGB output, let
        // tiny-skia source-over the AA fill on top (the very same op Exact
        // runs), then demul that region straight back into `rgb`. Same dst,
        // same tiny-skia blend, same demul ⇒ identical bytes, z-order
        // preserved (composited the instant `fill()` runs, so a later
        // fast-path op over these pixels still wins).
        //
        // For **Draft** this is the rare rounded-clip fallback (~3.2 % of
        // pixels). For **Fast** it is the tier's whole point: every curved op
        // takes this route (#27), which is why Fast adds NO new rasteriser and
        // NO new flattening — the polygons, the 1/64-px quantization and the
        // exact-integer band translation are Exact's.
        if !self.rgb.is_empty() {
            // At most 4 regions in practice (frame strips); a fixed array keeps
            // this allocation-free on the MCU hot path.
            let mut regions = [(0i32, 0i32, 0i32, 0i32); 8];
            let mut n = 0usize;
            for c in carry.iter().take(regions.len()) {
                if let Some(rg) = self.rgb_scratch_region(*c) {
                    regions[n] = rg;
                    n += 1;
                }
            }
            if n == 0 {
                return;
            }
            for rg in &regions[..n] {
                self.seed_scratch(*rg);
            }
            {
                let mask = match fate {
                    ClipFate::Masked => self.clip_mask.as_ref(),
                    _ => None,
                };
                if let Some(path) = self.path_from(contours) {
                    let paint = Self::paint_for(color, alpha);
                    self.pixmap.fill_path(
                        &path,
                        &paint,
                        FillRule::Winding,
                        Transform::identity(),
                        mask,
                    );
                }
            }
            for rg in &regions[..n] {
                self.demul_scratch(*rg);
            }
            return;
        }
        let mask = match fate {
            ClipFate::Masked => self.clip_mask.as_ref(),
            _ => None,
        };
        if let Some(path) = self.path_from(contours) {
            let paint = Self::paint_for(color, alpha);
            self.pixmap.fill_path(
                &path,
                &paint,
                FillRule::Winding,
                Transform::identity(),
                mask,
            );
        }
    }

    fn count(&mut self, class: OpClass, r: Rect) {
        // Clamp the op's bbox to this band (and the clip bounds when a clip
        // is active) — counts approximate pixels this band actually touched
        // (exact span counting lands in F2).
        let r = if self.clips.is_empty() {
            r
        } else {
            r.intersect(self.clip_bounds)
        };
        let x0 = r.x.max(self.area.x);
        let y0 = r.y.max(self.area.y);
        let x1 = (r.x + r.w as i32).min(self.area.x + self.area.w as i32);
        let y1 = (r.y + r.h as i32).min(self.area.y + self.area.h as i32);
        if x1 <= x0 || y1 <= y0 {
            return;
        }
        let px = (x1 - x0) as u64 * (y1 - y0) as u64;
        self.stats.pixels_written += px;
        self.stats.pixels_by_class[class as usize] += px;
    }

    fn class_for(alpha: u8) -> OpClass {
        if alpha == 0xFF {
            OpClass::OpaqueFill
        } else {
            OpClass::AlphaBlend
        }
    }

    /// Clamp a radius to half the short side (max-radius box = disc/stadium,
    /// the IR disc lowering).
    fn clamp_radius(r: Rect, radius: u32) -> f32 {
        (radius as f32).min(r.w as f32 / 2.0).min(r.h as f32 / 2.0)
    }

    /// Demultiply + convert the band into the caller's RGB888 buffer.
    /// `buf`/`stride` describe the band's own buffer (row 0 = band row 0).
    /// Unwritten (transparent) pixels come out black — the render tree always
    /// paints a backdrop first (the screen's background is IR-authoritative),
    /// so a transparent pixel reaching here is a scene bug, not a default.
    pub fn finish_into_rgb888(mut self, buf: &mut [u8], stride: usize) -> RenderStats {
        let w = self.area.w as usize;
        let h = self.area.h as usize;
        let g = self.gutter as usize; // 0 for Draft (no overscan), GUTTER for Exact
        let pm_w = w + 2 * g;
        assert!(
            stride >= w * 3,
            "stride {stride} too small for band width {w}"
        );
        // The last row only needs w*3 bytes — a dirty-rect render into the
        // middle of a full frame hands a slice that ends at the rect's last
        // pixel, not at the end of its stride.
        assert!(
            h == 0 || buf.len() >= stride * (h - 1) + w * 3,
            "buffer too small for band"
        );
        if !self.rgb.is_empty() {
            // Draft: the output surface ALREADY IS straight RGB888 in `rgb`
            // (composited in draw order). No demul, no convert — a plain
            // row-blit honouring `stride` (the band may be a sub-rect of a
            // full frame). This is the win: the ~59 %-of-Draft demul+convert
            // pass and the premul-pixmap round-trip are gone.
            for row in 0..h {
                let src = &self.rgb[row * w * 3..row * w * 3 + w * 3];
                buf[row * stride..row * stride + w * 3].copy_from_slice(src);
            }
            self.stats.bands_rendered += 1;
            return self.stats;
        }
        let px = self.pixmap.pixels();
        for row in 0..h {
            let out = &mut buf[row * stride..row * stride + w * 3];
            let src_row = (row + g) * pm_w + g; // skip the gutter
            for col in 0..w {
                let p = px[src_row + col].demultiply();
                out[col * 3] = p.red();
                out[col * 3 + 1] = p.green();
                out[col * 3 + 2] = p.blue();
            }
        }
        self.stats.bands_rendered += 1;
        self.stats
    }
}

impl Canvas for TinySkiaCanvas {
    fn push_clip(&mut self, r: Rect, radius: u32) {
        self.clip_bounds = if self.clips.is_empty() {
            r
        } else {
            self.clip_bounds.intersect(r)
        };
        self.clips.push(Clip { rect: r, radius });
        self.clip_mask = None; // stale for the new stack; rebuilt lazily
    }

    fn pop_clip(&mut self) {
        assert!(
            self.clips.pop().is_some(),
            "pop_clip without a matching push_clip (walker bug)"
        );
        // Recompute the conservative bounds for the remaining stack.
        let mut bounds: Option<Rect> = None;
        for c in &self.clips {
            bounds = Some(match bounds {
                None => c.rect,
                Some(b) => b.intersect(c.rect),
            });
        }
        self.clip_bounds = bounds.unwrap_or(Rect {
            x: 0,
            y: 0,
            w: 0,
            h: 0,
        });
        self.clip_mask = None;
    }

    fn fill_rrect(&mut self, r: Rect, radius: u32, color: Rgb, alpha: u8) {
        let rad = Self::clamp_radius(r, radius);
        // F16 Draft fast path: integer span fill, no AA, no tiny-skia (struct
        // docs). radius-0 OPAQUE fills are the plain span store; radius>0 fills
        // (any alpha) and translucent radius-0 fills take the integer
        // rounded-rect span carve (hard corners, the documented fidelity loss).
        // A fast-path method returns `false` only when a rounded clip overlaps
        // (→ Masked) — then we fall through to the Exact path so the clip stays
        // exact. After v3 EVERY fill is a fast-path pixel except that case.
        // F16 Fast (#27): a rounded rect HAS curved geometry, so the whole op
        // goes to the AA path. A radius-0 rect has none — it stays an integer
        // span (and AA would be a no-op on it anyway: an axis-aligned edge on
        // an integer coordinate has coverage exactly 1).
        if self.curve_to_aa(rad) && self.fill_rrect_fast(r, rad, color, alpha) {
            return;
        }
        if self.integer_spans() && !self.curve_to_aa(rad) {
            let handled = if radius == 0 && alpha == 0xFF {
                self.fill_rrect_draft(r, color, alpha)
            } else {
                // rad is clamped to half the short side (clamp_radius); 0 here
                // means a translucent square fill, which the rounded path draws
                // as full-width spans (dy always 0) — still all fast-path.
                self.fill_rrect_rounded_draft(r, rad as u32, color, alpha)
            };
            if handled {
                return;
            }
        }
        let pts = self.rrect_pts(r.x as f32, r.y as f32, r.w as f32, r.h as f32, rad);
        self.fill(&[&pts], color, alpha, r);
        self.count(Self::class_for(alpha), r);
    }

    fn stroke_rrect(&mut self, r: Rect, radius: u32, width: u32, color: Rgb, alpha: u8) {
        let rad = Self::clamp_radius(r, radius);
        // F16 Draft fast path: integer rounded-rect outline (outer spans minus
        // inner spans), no AA, no tiny-skia (struct docs). Declines (→ Exact)
        // for a translucent stroke or a rounded-clip overlap. The radius-0 box
        // border never reaches here (lowered to fill_rrects in
        // `draw_inside_border`), but the fast path handles any rad uniformly.
        // Fast (#27) sends a ROUNDED stroke to the AA path; a radius-0 box
        // border is all straight edges and stays integer.
        if self.integer_spans()
            && !self.curve_to_aa(rad)
            && self.stroke_rrect_rounded_draft(r, rad as u32, width, color, alpha)
        {
            return;
        }
        // Stroke centred on the contour, built as outer+inner polygons.
        // Paint extends width/2 beyond r — the clip bbox covers it.
        let bbox = r.inflate(width.div_ceil(2));
        let w2 = width as f32 / 2.0;
        let (x, y, w, h) = (r.x as f32, r.y as f32, r.w as f32, r.h as f32);
        let outer_rad = if rad > 0.0 { rad + w2 } else { 0.0 };
        let outer = self.rrect_pts(x - w2, y - w2, w + 2.0 * w2, h + 2.0 * w2, outer_rad);
        let iw = w - 2.0 * w2;
        let ih = h - 2.0 * w2;
        if iw <= 0.0 || ih <= 0.0 {
            // Stroke swallows the interior — it's a fill of the outer shape.
            self.fill(&[&outer], color, alpha, bbox);
        } else {
            let mut inner = self.rrect_pts(x + w2, y + w2, iw, ih, (rad - w2).max(0.0));
            inner.reverse(); // opposite winding cuts the hole
            // A stroke inks only its own band, never the interior: carry the
            // four strips, not the bbox. On a 456x44 frame border that is
            // ~3,000 px of scratch round-trip instead of 20,064. The hole is
            // the inner contour's bbox pulled IN by 1 px so no fringe escapes.
            let hole = Rect {
                x: libm::ceilf(x + w2) as i32 + 1,
                y: libm::ceilf(y + w2) as i32 + 1,
                w: (libm::floorf(iw) as i32 - 2).max(0) as u32,
                h: (libm::floorf(ih) as i32 - 2).max(0) as u32,
            };
            self.fill_into(
                &[&outer, &inner],
                color,
                alpha,
                bbox,
                &Self::frame_strips(bbox, hole),
            );
        }
        self.count(Self::class_for(alpha), r);
    }

    fn disc(&mut self, cx: i32, cy: i32, radius: u32, color: Rgb, alpha: u8) {
        // F16 Draft fast path: integer filled circle, no AA (struct docs). It
        // declines (→ false) only when a rounded clip overlaps, keeping the
        // clip exact; otherwise the curve takes the integer span fill.
        // Fast (#27) never takes it — a disc IS the curve the tier exists to
        // anti-alias.
        if !self.aa_curves()
            && self.integer_spans()
            && self.disc_draft(cx, cy, radius, color, alpha)
        {
            return;
        }
        let d = radius * 2;
        let bbox = Rect {
            x: cx - radius as i32,
            y: cy - radius as i32,
            w: d,
            h: d,
        };
        let pts = self.circle_pts(cx, cy, radius as f32);
        self.fill(&[&pts], color, alpha, bbox);
        self.count(Self::class_for(alpha), bbox);
    }

    fn ring(&mut self, cx: i32, cy: i32, radius: u32, width: u32, color: Rgb, alpha: u8) {
        // F16 Draft fast path: integer annulus, no AA (struct docs).
        // Fast (#27) never takes it — the gauge ring is THE curve the tier
        // exists to anti-alias.
        if !self.aa_curves()
            && self.integer_spans()
            && self.ring_draft(cx, cy, radius, width, color, alpha)
        {
            return;
        }
        // Annulus: stroke centred on `radius`, as outer+inner contours.
        let pad = radius + width.div_ceil(2);
        let d = pad * 2;
        let bbox = Rect {
            x: cx - pad as i32,
            y: cy - pad as i32,
            w: d,
            h: d,
        };
        let w2 = width as f32 / 2.0;
        let outer_r = radius as f32 + w2;
        let inner_r = radius as f32 - w2;
        let outer = self.circle_pts(cx, cy, outer_r);
        if inner_r <= 0.0 {
            self.fill(&[&outer], color, alpha, bbox);
        } else {
            let mut inner = self.circle_pts(cx, cy, inner_r);
            inner.reverse();
            // The annulus never inks its hole. The largest axis-aligned square
            // inside a circle of radius `inner_r` has half-side inner_r/sqrt2;
            // take one px less so no AA fringe on the inner edge escapes the
            // carried strips. Gauge ring 110x110: ~8.5k px carried, not 12.1k.
            let hs = (libm::floorf(inner_r * core::f32::consts::FRAC_1_SQRT_2) as i32 - 1).max(0);
            let hole = Rect {
                x: cx - hs,
                y: cy - hs,
                w: (2 * hs) as u32,
                h: (2 * hs) as u32,
            };
            self.fill_into(
                &[&outer, &inner],
                color,
                alpha,
                bbox,
                &Self::frame_strips(bbox, hole),
            );
        }
        self.count(Self::class_for(alpha), bbox);
    }

    fn line(&mut self, x0: i32, y0: i32, x1: i32, y1: i32, width: u32, color: Rgb, alpha: u8) {
        // F16 Draft fast path: the same quad rasterized as integer per-row
        // spans, no AA (struct docs).
        // Fast keeps it: a butt-capped line is a straight-edged quad, not a
        // curve — "integer spans everywhere they apply" (#27). Its diagonal
        // edges therefore stay hard at Fast; a documented, measured divergence
        // from Exact, not an oversight.
        if self.integer_spans() && self.line_draft(x0, y0, x1, y1, width, color, alpha) {
            return;
        }
        // Butt-capped stroke as a quad: offset both endpoints by the
        // perpendicular half-width. Zero-length lines draw nothing (honest
        // no-op: there is no direction to stroke).
        let (fx0, fy0, fx1, fy1) = (x0 as f32, y0 as f32, x1 as f32, y1 as f32);
        let (dx, dy) = (fx1 - fx0, fy1 - fy0);
        let len = libm::sqrtf(dx * dx + dy * dy);
        if len <= 0.0 {
            return;
        }
        let pad = width.div_ceil(2) as i32;
        let bbox = Rect {
            x: x0.min(x1) - pad,
            y: y0.min(y1) - pad,
            w: (x0.max(x1) - x0.min(x1)) as u32 + width,
            h: (y0.max(y1) - y0.min(y1)) as u32 + width,
        };
        let w2 = width as f32 / 2.0;
        let (px, py) = (-dy / len * w2, dx / len * w2);
        let quad = [
            q2(fx0 + px, fy0 + py),
            q2(fx1 + px, fy1 + py),
            q2(fx1 - px, fy1 - py),
            q2(fx0 - px, fy0 - py),
        ];
        self.fill(&[&quad], color, alpha, bbox);
        self.count(Self::class_for(alpha), bbox);
    }

    fn fill_linear_gradient(
        &mut self,
        r: Rect,
        radius: u32,
        from: Rgb,
        to: Rgb,
        vertical: bool,
        alpha: u8,
    ) {
        let rad = Self::clamp_radius(r, radius);
        // F16 Draft fast path: integer per-pixel linear ramp, no AA, no
        // tiny-skia (struct docs). Declines (→ Exact) only for a translucent
        // gradient or a rounded-clip overlap.
        // Fast (#27) sends a ROUNDED gradient to the AA path (curved corners);
        // a square gradient keeps the integer ramp — its ramp is a colour
        // approximation, not an edge one, and Fast is an EDGE-quality tier.
        if self.integer_spans()
            && !self.curve_to_aa(rad)
            && self.fill_gradient_draft(r, rad as u32, from, to, vertical, alpha)
        {
            return;
        }
        let fate = self.op_clip_fill(r);
        if fate == ClipFate::Skip {
            return;
        }
        let pts = self.rrect_pts(r.x as f32, r.y as f32, r.w as f32, r.h as f32, rad);
        let Some(path) = self.path_from(&[&pts]) else {
            return;
        };
        // Shader anchors get the same exact-integer translation as vertices,
        // so the gradient field is band-invariant.
        let (ox, oy) = self.off();
        let (x, y) = (r.x as f32 + ox, r.y as f32 + oy);
        let (start, end) = if vertical {
            (Point::from_xy(x, y), Point::from_xy(x, y + r.h as f32))
        } else {
            (Point::from_xy(x, y), Point::from_xy(x + r.w as f32, y))
        };
        let a = alpha;
        let Some(shader) = LinearGradient::new(
            start,
            end,
            alloc::vec![
                GradientStop::new(0.0, tiny_skia::Color::from_rgba8(from.r, from.g, from.b, a)),
                GradientStop::new(1.0, tiny_skia::Color::from_rgba8(to.r, to.g, to.b, a)),
            ],
            SpreadMode::Pad,
            Transform::identity(),
        ) else {
            return;
        };
        let paint = Paint {
            shader,
            anti_alias: true,
            ..Paint::default()
        };
        // Draft/Fast: same scratch round-trip as `fill()` — seed the op's band
        // region from `rgb`, let tiny-skia shade on top, demul back. Without
        // this the shaded pixels would land in the scratch pixmap and never
        // reach the output surface (they did not, before #27 — a latent
        // Draft bug reachable only via a translucent gradient).
        if !self.rgb.is_empty() {
            let Some(region) = self.rgb_scratch_region(r) else {
                return;
            };
            self.seed_scratch(region);
            {
                let mask = match fate {
                    ClipFate::Masked => self.clip_mask.as_ref(),
                    _ => None,
                };
                self.pixmap.fill_path(
                    &path,
                    &paint,
                    FillRule::Winding,
                    Transform::identity(),
                    mask,
                );
            }
            self.demul_scratch(region);
            self.count(Self::class_for(alpha), r);
            return;
        }
        let mask = match fate {
            ClipFate::Masked => self.clip_mask.as_ref(),
            _ => None,
        };
        self.pixmap.fill_path(
            &path,
            &paint,
            FillRule::Winding,
            Transform::identity(),
            mask,
        );
        // Gradient spans both classes; attribute by alpha like plain fills.
        self.count(Self::class_for(alpha), r);
    }

    fn glyph_run(&mut self, glyphs: &[PlacedGlyph<'_>], color: Rgb, alpha: u8) -> Option<Rect> {
        // Manual deterministic source-over of A8 masks into the premultiplied
        // band pixmap (see module docs — glyph blits sit outside the polygon
        // rule). Integer world → gutter-local translation, per-pixel integer
        // blend with d255 rounding: bit-identical in every band.
        // The returned ink bbox is pure GEOMETRY (world coords, no band
        // clamp, no clip trim) — identical in every band, per the trait
        // contract; a FULLY clipped-out run inks nothing and returns None
        // (a world-space verdict, also band-invariant).
        //
        // Clip handling (module docs): run-level fate from the union bbox;
        // rect-only stacks clamp the integer spans exactly; rounded stacks
        // multiply the A8 clip mask into glyph coverage with d255 rounding.
        let mut bb: Option<(i32, i32, i32, i32)> = None;
        for g in glyphs {
            let (x1, y1) = (g.x + g.mask.w as i32, g.y + g.mask.h as i32);
            bb = Some(match bb {
                None => (g.x, g.y, x1, y1),
                Some((a, b, c, d)) => (a.min(g.x), b.min(g.y), c.max(x1), d.max(y1)),
            });
        }
        let (ux0, uy0, ux1, uy1) = bb?;
        let fate = self.op_clip(Rect {
            x: ux0,
            y: uy0,
            w: (ux1 - ux0) as u32,
            h: (uy1 - uy0) as u32,
        });
        if fate == ClipFate::Skip {
            return None;
        }
        let span_clip = match fate {
            ClipFate::RectSpans(cb) => Some(cb),
            _ => None,
        };
        // take() the clip mask so per-glyph count() (&mut self) stays legal;
        // restored after the loops.
        let cmask = match fate {
            ClipFate::Masked => self.clip_mask.take(),
            _ => None,
        };
        let cmdata = cmask.as_ref().map(|m| m.data());
        let mut ink: Option<(i32, i32, i32, i32)> = None;
        let (oxi, oyi) = (
            self.gutter as i32 - self.area.x,
            self.gutter as i32 - self.area.y,
        );
        let pm_w = self.pixmap.width() as i32;
        let pm_h = self.pixmap.height() as i32;
        // Draft/Fast: composite straight RGB888 into the output band directly.
        // Dst is opaque RGB (backdrop first), so na == 255 and the blend below
        // is byte-identical to the premul-store→demul. Indices: `ly·pm_w+lx` is
        // GUTTER-LOCAL (the clip mask's space, pixmap-sized); the `rgb` band
        // buffer has NO gutter, so its index subtracts it — 0 for Draft, GUTTER
        // for Fast. The bounds below are the band's own for the rgb path (a
        // gutter row has no `rgb` pixel to write), and the pixmap's otherwise;
        // for Draft the two coincide exactly, so Draft's bytes are unchanged.
        let draft = !self.rgb.is_empty();
        let gi = self.gutter as i32;
        let (bw, bh) = (self.area.w as i32, self.area.h as i32);
        let (lo_x, hi_x, lo_y, hi_y) = if draft {
            (gi, gi + bw, gi, gi + bh)
        } else {
            (0, pm_w, 0, pm_h)
        };
        for g in glyphs {
            let m = g.mask;
            // Span ranges in mask-local coords; exact integer clamping when
            // a rect-only clip is active.
            let (mut row0, mut row1) = (0i32, m.h as i32);
            let (mut col0, mut col1) = (0i32, m.w as i32);
            if let Some(cb) = span_clip {
                row0 = row0.max(cb.y - g.y);
                row1 = row1.min(cb.y + cb.h as i32 - g.y);
                col0 = col0.max(cb.x - g.x);
                col1 = col1.min(cb.x + cb.w as i32 - g.x);
            }
            for row in row0..row1 {
                let ly = g.y + row + oyi;
                if ly < lo_y || ly >= hi_y {
                    continue;
                }
                for col in col0..col1 {
                    let lx = g.x + col + oxi;
                    if lx < lo_x || lx >= hi_x {
                        continue;
                    }
                    let mut cov = m.a8[(row * m.w as i32 + col) as usize] as u32;
                    if cov == 0 {
                        continue;
                    }
                    if let Some(cd) = cmdata {
                        // Clip coverage × glyph coverage, d255 rounding —
                        // deterministic, identity where the mask is 255.
                        cov = d255(cov * cd[(ly * pm_w + lx) as usize] as u32);
                        if cov == 0 {
                            continue;
                        }
                    }
                    // Effective alpha = coverage × paint alpha.
                    let a = d255(cov * alpha as u32);
                    let i = (ly * pm_w + lx) as usize;
                    let ia = 255 - a;
                    if draft {
                        // Straight RGB source-over opaque dst (na == 255).
                        let i3 = ((ly - gi) * bw + (lx - gi)) as usize * 3;
                        let rgb = &mut self.rgb;
                        let nr = d255(color.r as u32 * a + rgb[i3] as u32 * ia);
                        let ng = d255(color.g as u32 * a + rgb[i3 + 1] as u32 * ia);
                        let nb = d255(color.b as u32 * a + rgb[i3 + 2] as u32 * ia);
                        rgb[i3] = nr as u8;
                        rgb[i3 + 1] = ng as u8;
                        rgb[i3 + 2] = nb as u8;
                        continue;
                    }
                    let px = self.pixmap.pixels_mut();
                    let dst = px[i];
                    let na = d255(255 * a + dst.alpha() as u32 * ia);
                    let nr = d255(color.r as u32 * a + dst.red() as u32 * ia);
                    let ng = d255(color.g as u32 * a + dst.green() as u32 * ia);
                    let nb = d255(color.b as u32 * a + dst.blue() as u32 * ia);
                    // Channels ≤ alpha holds by monotonicity of d255 (each
                    // numerator ≤ the alpha numerator); min() guards the
                    // premultiplied constructor anyway — no panic path.
                    if let Some(p) = PremultipliedColorU8::from_rgba(
                        nr.min(na) as u8,
                        ng.min(na) as u8,
                        nb.min(na) as u8,
                        na as u8,
                    ) {
                        px[i] = p;
                    }
                }
            }
            // Clamped-bbox pixel attribution, like every other op (F2
            // refines to exact span counts).
            self.count(
                OpClass::Glyph,
                Rect {
                    x: g.x,
                    y: g.y,
                    w: m.w,
                    h: m.h,
                },
            );
            let (x1, y1) = (g.x + m.w as i32, g.y + m.h as i32);
            ink = Some(match ink {
                None => (g.x, g.y, x1, y1),
                Some((a, b, c, d)) => (a.min(g.x), b.min(g.y), c.max(x1), d.max(y1)),
            });
        }
        if cmask.is_some() {
            self.clip_mask = cmask; // hand the cached mask back
        }
        ink.map(|(x0, y0, x1, y1)| Rect {
            x: x0,
            y: y0,
            w: (x1 - x0) as u32,
            h: (y1 - y0) as u32,
        })
    }

    fn blit_image(&mut self, dst: Rect, image: &RgbaImage, clip: Rect) {
        // Manual deterministic source-over of straight-alpha RGBA into the
        // premultiplied band pixmap — the glyph-blit pattern (module docs):
        // integer world → gutter-local translation, per-pixel integer blend
        // with d255 rounding, bit-identical in every band. The image is SCALED
        // into the FIT-TO-CELL `dst` rect (preserve aspect, centred — computed
        // by ir::fit_to_cell); `dst ⊆ clip` (the widget rect), so the letterbox
        // bands of `clip` outside `dst` paint nothing. The world-space walk
        // covers dst ∩ clip only.
        //
        // ## Band-exact SCALED resampling (the F6 invariant under scaling)
        //
        // NEAREST-NEIGHBOUR with FIXED-POINT integer source mapping: for a DEST
        // pixel at world column `wx`, the source column is
        //     sx = ((wx - dst.x) * W) / dst.w   (integer division)
        // and likewise sy. This is a PURE INTEGER FUNCTION of world position
        // and the (band-invariant) dst geometry — NO float in the per-pixel
        // map — so every band computes the SAME src pixel for the same world
        // pixel. Band equivalence then holds by the SAME induction as the 1:1
        // blit (each world pixel's output depends only on world position, the
        // mapped src RGBA, and existing dst). `wx - dst.x ∈ [0, dst.w-1]` ⇒
        // sx ∈ [0, (dst.w-1)·W/dst.w] ⊆ [0, W-1]; the clamp is belt-and-braces.
        // Qt uses SMOOTH (bilinear) — a future F16 quality knob; the GEOMETRY
        // (dst position/size) matches Qt now, resampling QUALITY is a separate
        // axis (the image_fit probe checks geometry).
        //
        // Clip-stack handling (module docs): rect-only stacks tighten the
        // exact integer walk bounds; rounded stacks fold the A8 clip mask
        // into the source alpha with d255 rounding.
        let (iw, ih) = (image.w() as i32, image.h() as i32);
        let dw = dst.w as i32;
        let dh = dst.h as i32;
        if dw <= 0 || dh <= 0 {
            return; // empty fit (degenerate box) — nothing to blit
        }
        let mut x0 = dst.x.max(clip.x);
        let mut y0 = dst.y.max(clip.y);
        let mut x1 = (dst.x + dw).min(clip.x + clip.w as i32);
        let mut y1 = (dst.y + dh).min(clip.y + clip.h as i32);
        if x1 <= x0 || y1 <= y0 {
            return;
        }
        let fate = self.op_clip(Rect {
            x: x0,
            y: y0,
            w: (x1 - x0) as u32,
            h: (y1 - y0) as u32,
        });
        if fate == ClipFate::Skip {
            return;
        }
        if let ClipFate::RectSpans(cb) = fate {
            x0 = x0.max(cb.x);
            y0 = y0.max(cb.y);
            x1 = x1.min(cb.x + cb.w as i32);
            y1 = y1.min(cb.y + cb.h as i32);
            if x1 <= x0 || y1 <= y0 {
                return;
            }
        }
        // take() the clip mask so count() (&mut self) stays legal below.
        let cmask = match fate {
            ClipFate::Masked => self.clip_mask.take(),
            _ => None,
        };
        let cmdata = cmask.as_ref().map(|m| m.data());
        let (oxi, oyi) = (
            self.gutter as i32 - self.area.x,
            self.gutter as i32 - self.area.y,
        );
        let pm_w = self.pixmap.width() as i32;
        let pm_h = self.pixmap.height() as i32;
        let rgba = image.rgba();
        // Draft/Fast: composite straight RGB888 into the output band directly.
        // Dst is opaque RGB (backdrop first), so na == 255 and the blend is
        // byte-identical to the premul-store→demul. `ly·pm_w+lx` stays
        // GUTTER-LOCAL for the clip mask; the `rgb` index subtracts the gutter
        // (0 for Draft, GUTTER for Fast) — see `glyph_run` for the same note.
        let draft = !self.rgb.is_empty();
        let gi = self.gutter as i32;
        let (bw, bh) = (self.area.w as i32, self.area.h as i32);
        let (lo_x, hi_x, lo_y, hi_y) = if draft {
            (gi, gi + bw, gi, gi + bh)
        } else {
            (0, pm_w, 0, pm_h)
        };
        for wy in y0..y1 {
            let ly = wy + oyi;
            if ly < lo_y || ly >= hi_y {
                continue;
            }
            // Nearest-neighbour source row — pure integer function of world wy.
            let sy = (((wy - dst.y) * ih) / dh).clamp(0, ih - 1);
            for wx in x0..x1 {
                let lx = wx + oxi;
                if lx < lo_x || lx >= hi_x {
                    continue;
                }
                // Nearest-neighbour source col — pure integer function of wx.
                let sx = (((wx - dst.x) * iw) / dw).clamp(0, iw - 1);
                // Source pixel (in-bounds by the clamp above; buffer length by
                // RgbaImage::new).
                let si = ((sy * iw + sx) * 4) as usize;
                let mut a = rgba[si + 3] as u32;
                if let Some(cd) = cmdata {
                    // Clip mask × source alpha, d255 rounding — identity
                    // where the mask is 255, so the opaque fast path below
                    // survives exactly where the clip is fully open.
                    a = d255(a * cd[(ly * pm_w + lx) as usize] as u32);
                }
                if a == 0 {
                    continue;
                }
                let (sr, sg, sb) = (rgba[si] as u32, rgba[si + 1] as u32, rgba[si + 2] as u32);
                let i = (ly * pm_w + lx) as usize;
                let ia = 255 - a;
                if draft {
                    let i3 = ((ly - gi) * bw + (lx - gi)) as usize * 3;
                    let buf = &mut self.rgb;
                    if a == 0xFF {
                        // Opaque copy (ia = 0 ⇒ channel = d255(255·s) = s).
                        buf[i3] = sr as u8;
                        buf[i3 + 1] = sg as u8;
                        buf[i3 + 2] = sb as u8;
                    } else {
                        // Straight-alpha source over opaque straight dst.
                        let nr = d255(sr * a + buf[i3] as u32 * ia);
                        let ng = d255(sg * a + buf[i3 + 1] as u32 * ia);
                        let nb = d255(sb * a + buf[i3 + 2] as u32 * ia);
                        buf[i3] = nr as u8;
                        buf[i3 + 1] = ng as u8;
                        buf[i3 + 2] = nb as u8;
                    }
                    continue;
                }
                let px = self.pixmap.pixels_mut();
                if a == 0xFF {
                    // Opaque copy — byte-identical to the blend below
                    // (ia = 0 ⇒ each channel = d255(255·s) = s), split out
                    // because opaque pixels dominate real assets.
                    if let Some(p) =
                        PremultipliedColorU8::from_rgba(sr as u8, sg as u8, sb as u8, 0xFF)
                    {
                        px[i] = p;
                    }
                    continue;
                }
                // Straight-alpha source over premultiplied dst: the glyph
                // formula with (sr, a) in place of (color × coverage).
                let dpx = px[i];
                let na = d255(255 * a + dpx.alpha() as u32 * ia);
                let nr = d255(sr * a + dpx.red() as u32 * ia);
                let ng = d255(sg * a + dpx.green() as u32 * ia);
                let nb = d255(sb * a + dpx.blue() as u32 * ia);
                // Channels ≤ alpha by monotonicity of d255 (sr ≤ 255 and
                // dst.red ≤ dst.alpha ⇒ each numerator ≤ the alpha
                // numerator); min() guards the constructor anyway.
                if let Some(p) = PremultipliedColorU8::from_rgba(
                    nr.min(na) as u8,
                    ng.min(na) as u8,
                    nb.min(na) as u8,
                    na as u8,
                ) {
                    px[i] = p;
                }
            }
        }
        if cmask.is_some() {
            self.clip_mask = cmask; // hand the cached mask back
        }
        // Exact blitted-area attribution (image ∩ clip), clamped to the band
        // by count() like every other op.
        self.count(
            OpClass::Blit,
            Rect {
                x: x0,
                y: y0,
                w: (x1 - x0) as u32,
                h: (y1 - y0) as u32,
            },
        );
    }

    fn stats(&self) -> RenderStats {
        self.stats
    }
}

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
/// Belt-and-braces for AA in the clip-adjacent rows (the polygon discipline
/// above is the load-bearing fix). Cost: (w+16)×(h+16) raster for a w×h
/// band — a per-band fixed cost the I4 scaling assertion tracks (F2).
const GUTTER: u32 = 8;

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

fn q2(x: f32, y: f32) -> (f32, f32) {
    (q(x), q(y))
}

/// Exact `round(x / 255)` in pure integer math — the deterministic blend
/// divisor for glyph blits (valid for x ≤ 2·255², well inside u32).
fn d255(x: u32) -> u32 {
    (2 * x + 255) / 510
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
/// `Quality::Draft` is the budgeted-MCU runtime tier: it recovers the
/// per-pixel cost the Exact tier spends on float-coverage AA. The hot ops it
/// accelerates with DIRECT INTEGER rasterization (no path, no coverage, no
/// tiny-skia) — the v2 set, now covering the CURVE primitives:
///
/// - **opaque axis-aligned `fill_rrect`** ([`fill_rrect_draft`]): one integer
///   span per row, premultiplied pixels straight into the pixmap (memset-class).
/// - **`disc`** ([`disc_draft`]): integer filled circle — per scanline the span
///   `[cx-dx, cx+dx]` from the exact integer test `dx² ≤ r²−dy²`
///   (`dx = isqrt(r²−dy²)`). Hard edge, no AA.
/// - **`ring`** ([`ring_draft`]): integer annulus — the outer filled-circle
///   spans minus the inner-circle spans per scanline (two spans per row where
///   the hole is open, one where it isn't). Hard edge, no AA.
/// - **`line`** ([`line_draft`]): the same butt-capped quad the Exact path
///   builds, rasterized as integer per-row spans from the quad's edges in
///   1/256-px fixed point (a pure integer function of world row). Hard edge.
///
/// All four take an integer per-pixel decision from WORLD coordinates and an
/// integer source-over for the pixel write ([`Self::draft_span`]) — they handle
/// the OPAQUE case (a plain premultiplied store) and the TRANSLUCENT case (the
/// d255 source-over the glyph/image blits use), so a translucent disc/line is
/// still a fast-path pixel, not an Exact fallback.
///
/// **Band-exact WITHIN the tier by the same induction as the glyph/image
/// blits** (module docs above): every Draft op's per-pixel verdict is a pure
/// function of WORLD position (op geometry ∩ the band ∩ the rect-only clip),
/// written with the exact-integer world→gutter-local offset. No AA, no fringe —
/// every band that touches a given world pixel writes the same byte.
/// Enforcement: `tests/draft_golden.rs` (Draft golden hash + even/uneven
/// band equivalence). Draft pixels DIFFER from Exact (hard edges, no AA) —
/// that is the tier's deliberate trade; Draft is NEVER asserted == Exact.
///
/// **What still falls back to the Exact path in Draft v2**, counted honestly:
/// radius>0 fills draw the rect SQUARE (corners are hard — the fidelity loss
/// the measurement reports), `stroke_rrect`, `fill_linear_gradient`, glyphs and
/// images take the float/Exact path unchanged. A Draft op that meets a rounded
/// clip overlap (`ClipFate::Masked`) also declines so the clip stays exact.
/// [`RenderStats::fastpath_pixels`] records exactly how many pixels the fast
/// path carried so the coverage % is honest (#21).
pub struct TinySkiaCanvas {
    pixmap: Pixmap,
    area: Rect,
    quality: Quality,
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
}

impl TinySkiaCanvas {
    /// A canvas for ONE band. `area` is the band's rectangle in world
    /// coordinates; the backing pixmap is `(area.w + 2·GUTTER) ×
    /// (area.h + 2·GUTTER)` — see [`GUTTER`].
    pub fn new(area: Rect) -> Option<Self> {
        Self::new_with_quality(area, Quality::Exact)
    }

    /// A canvas for ONE band at an explicit [`Quality`] tier (F16, #16).
    /// `Quality::Exact` is identical to [`Self::new`] (the oracle path);
    /// `Quality::Draft` enables the integer no-AA fast path for opaque
    /// axis-aligned fills (struct docs). The pixmap is still allocated either
    /// way — the gutter overscan stays (its removal is a future `Fast` knob,
    /// #16); Draft's win is per-pixel work, not the band's fixed cost.
    pub fn new_with_quality(area: Rect, quality: Quality) -> Option<Self> {
        let pixmap = Pixmap::new(area.w + 2 * GUTTER, area.h + 2 * GUTTER)?;
        Some(Self {
            pixmap,
            area,
            quality,
            stats: RenderStats::default(),
            clips: Vec::new(),
            clip_bounds: Rect {
                x: 0,
                y: 0,
                w: 0,
                h: 0,
            },
            clip_mask: None,
        })
    }

    /// This band's world-space rectangle (what the canvas will deliver) —
    /// the cull target for walk-level paint skipping.
    pub fn area(&self) -> Rect {
        self.area
    }

    /// The exact-integer world → gutter-local offset.
    fn off(&self) -> (f32, f32) {
        (
            (GUTTER as i32 - self.area.x) as f32,
            (GUTTER as i32 - self.area.y) as f32,
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
        for clip in &self.clips {
            let rad = Self::clamp_radius(clip.rect, clip.radius);
            let pts = rrect_points(
                clip.rect.x as f32,
                clip.rect.y as f32,
                clip.rect.w as f32,
                clip.rect.h as f32,
                rad,
            );
            let (ox, oy) = (
                (GUTTER as i32 - self.area.x) as f32,
                (GUTTER as i32 - self.area.y) as f32,
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
        // premultiplied colour (alpha 255 ⇒ premultiplied == straight). One
        // store per pixel; the inner loop is a straight slice write.
        let Some(src) = PremultipliedColorU8::from_rgba(color.r, color.g, color.b, 0xFF) else {
            return false;
        };
        let (oxi, oyi) = (GUTTER as i32 - self.area.x, GUTTER as i32 - self.area.y);
        let pm_w = self.pixmap.width() as i32;
        let px = self.pixmap.pixels_mut();
        for wy in wy0..wy1 {
            let ly = wy + oyi;
            let row = (ly * pm_w) as usize;
            let c0 = (wx0 + oxi) as usize;
            let c1 = (wx1 + oxi) as usize;
            px[row + c0..row + c1].fill(src);
        }
        // Exact span attribution — these pixels took the fast path.
        let drawn = (wx1 - wx0) as u64 * (wy1 - wy0) as u64;
        self.stats.pixels_written += drawn;
        self.stats.pixels_by_class[OpClass::OpaqueFill as usize] += drawn;
        self.stats.fastpath_pixels += drawn;
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
        let (oxi, oyi) = (GUTTER as i32 - self.area.x, GUTTER as i32 - self.area.y);
        let pm_w = self.pixmap.width() as i32;
        let ly = wy + oyi;
        let row = (ly * pm_w) as usize;
        let px = self.pixmap.pixels_mut();
        if alpha == 0xFF {
            // Opaque source-over = a plain store of the (premult==straight)
            // colour, one slice fill — identical to fill_rrect_draft.
            if let Some(src) = PremultipliedColorU8::from_rgba(color.r, color.g, color.b, 0xFF) {
                let c0 = (lo + oxi) as usize;
                let c1 = (hi + oxi) as usize;
                px[row + c0..row + c1].fill(src);
            }
        } else {
            // Translucent: integer d255 source-over per pixel (the glyph-blit
            // formula with full coverage; a = paint alpha).
            let a = alpha as u32;
            let ia = 255 - a;
            for wx in lo..hi {
                let i = row + (wx + oxi) as usize;
                let dst = px[i];
                let na = d255(255 * a + dst.alpha() as u32 * ia);
                let nr = d255(color.r as u32 * a + dst.red() as u32 * ia);
                let ng = d255(color.g as u32 * a + dst.green() as u32 * ia);
                let nb = d255(color.b as u32 * a + dst.blue() as u32 * ia);
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
        let fate = self.op_clip_fill(bbox);
        if fate == ClipFate::Skip {
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
        let g = GUTTER as usize;
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
        // F16 Draft fast path: an OPAQUE fill draws as an integer span fill,
        // no AA, no tiny-skia (struct docs). Radius>0 draws SQUARE in Draft —
        // corners are hard (the documented fidelity loss); the measurement
        // reports how many pixels that affects. Translucent fills, and any
        // op the fast path declines (rounded clip overlap → Masked), fall
        // through to the Exact path below.
        if self.quality == Quality::Draft && alpha == 0xFF && self.fill_rrect_draft(r, color, alpha)
        {
            return;
        }
        let pts = rrect_points(r.x as f32, r.y as f32, r.w as f32, r.h as f32, rad);
        self.fill(&[&pts], color, alpha, r);
        self.count(Self::class_for(alpha), r);
    }

    fn stroke_rrect(&mut self, r: Rect, radius: u32, width: u32, color: Rgb, alpha: u8) {
        // Stroke centred on the contour, built as outer+inner polygons.
        // Paint extends width/2 beyond r — the clip bbox covers it.
        let bbox = r.inflate(width.div_ceil(2));
        let w2 = width as f32 / 2.0;
        let rad = Self::clamp_radius(r, radius);
        let (x, y, w, h) = (r.x as f32, r.y as f32, r.w as f32, r.h as f32);
        let outer_rad = if rad > 0.0 { rad + w2 } else { 0.0 };
        let outer = rrect_points(x - w2, y - w2, w + 2.0 * w2, h + 2.0 * w2, outer_rad);
        let iw = w - 2.0 * w2;
        let ih = h - 2.0 * w2;
        if iw <= 0.0 || ih <= 0.0 {
            // Stroke swallows the interior — it's a fill of the outer shape.
            self.fill(&[&outer], color, alpha, bbox);
        } else {
            let mut inner = rrect_points(x + w2, y + w2, iw, ih, (rad - w2).max(0.0));
            inner.reverse(); // opposite winding cuts the hole
            self.fill(&[&outer, &inner], color, alpha, bbox);
        }
        self.count(Self::class_for(alpha), r);
    }

    fn disc(&mut self, cx: i32, cy: i32, radius: u32, color: Rgb, alpha: u8) {
        // F16 Draft fast path: integer filled circle, no AA (struct docs). It
        // declines (→ false) only when a rounded clip overlaps, keeping the
        // clip exact; otherwise the curve takes the integer span fill.
        if self.quality == Quality::Draft && self.disc_draft(cx, cy, radius, color, alpha) {
            return;
        }
        let d = radius * 2;
        let bbox = Rect {
            x: cx - radius as i32,
            y: cy - radius as i32,
            w: d,
            h: d,
        };
        let pts = circle_points(cx, cy, radius as f32);
        self.fill(&[&pts], color, alpha, bbox);
        self.count(Self::class_for(alpha), bbox);
    }

    fn ring(&mut self, cx: i32, cy: i32, radius: u32, width: u32, color: Rgb, alpha: u8) {
        // F16 Draft fast path: integer annulus, no AA (struct docs).
        if self.quality == Quality::Draft && self.ring_draft(cx, cy, radius, width, color, alpha) {
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
        let outer = circle_points(cx, cy, outer_r);
        if inner_r <= 0.0 {
            self.fill(&[&outer], color, alpha, bbox);
        } else {
            let mut inner = circle_points(cx, cy, inner_r);
            inner.reverse();
            self.fill(&[&outer, &inner], color, alpha, bbox);
        }
        self.count(Self::class_for(alpha), bbox);
    }

    fn line(&mut self, x0: i32, y0: i32, x1: i32, y1: i32, width: u32, color: Rgb, alpha: u8) {
        // F16 Draft fast path: the same quad rasterized as integer per-row
        // spans, no AA (struct docs).
        if self.quality == Quality::Draft && self.line_draft(x0, y0, x1, y1, width, color, alpha) {
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
        let fate = self.op_clip_fill(r);
        if fate == ClipFate::Skip {
            return;
        }
        let rad = Self::clamp_radius(r, radius);
        let pts = rrect_points(r.x as f32, r.y as f32, r.w as f32, r.h as f32, rad);
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
        let (oxi, oyi) = (GUTTER as i32 - self.area.x, GUTTER as i32 - self.area.y);
        let pm_w = self.pixmap.width() as i32;
        let pm_h = self.pixmap.height() as i32;
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
            let px = self.pixmap.pixels_mut();
            for row in row0..row1 {
                let ly = g.y + row + oyi;
                if ly < 0 || ly >= pm_h {
                    continue;
                }
                for col in col0..col1 {
                    let lx = g.x + col + oxi;
                    if lx < 0 || lx >= pm_w {
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
                    let dst = px[i];
                    let ia = 255 - a;
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
        let (oxi, oyi) = (GUTTER as i32 - self.area.x, GUTTER as i32 - self.area.y);
        let pm_w = self.pixmap.width() as i32;
        let pm_h = self.pixmap.height() as i32;
        let rgba = image.rgba();
        let px = self.pixmap.pixels_mut();
        for wy in y0..y1 {
            let ly = wy + oyi;
            if ly < 0 || ly >= pm_h {
                continue;
            }
            // Nearest-neighbour source row — pure integer function of world wy.
            let sy = (((wy - dst.y) * ih) / dh).clamp(0, ih - 1);
            for wx in x0..x1 {
                let lx = wx + oxi;
                if lx < 0 || lx >= pm_w {
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
                let dst = px[i];
                let ia = 255 - a;
                let na = d255(255 * a + dst.alpha() as u32 * ia);
                let nr = d255(sr * a + dst.red() as u32 * ia);
                let ng = d255(sg * a + dst.green() as u32 * ia);
                let nb = d255(sb * a + dst.blue() as u32 * ia);
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

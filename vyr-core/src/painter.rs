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
//! **Image blits (F6) follow the same pattern**: [`Canvas::blit_image`] is an
//! integer-positioned source-over of caller-decoded straight-alpha RGBA onto
//! the band — the glyph formula with the image's own per-pixel colour+alpha
//! in place of (paint colour × coverage). No scaling, no filtering, no float
//! — band-exact by the same induction. Enforcement:
//! `tests/image_golden.rs::image_band_equivalence`.
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

use crate::{Canvas, OpClass, PlacedGlyph, Rect, RenderStats, Rgb, RgbaImage};
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

pub struct TinySkiaCanvas {
    pixmap: Pixmap,
    area: Rect,
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
        let pixmap = Pixmap::new(area.w + 2 * GUTTER, area.h + 2 * GUTTER)?;
        Some(Self {
            pixmap,
            area,
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

    fn blit_image(&mut self, x: i32, y: i32, image: &RgbaImage, clip: Rect) {
        // Manual deterministic source-over of straight-alpha RGBA into the
        // premultiplied band pixmap — the glyph-blit pattern (module docs):
        // integer world → gutter-local translation, per-pixel integer blend
        // with d255 rounding, bit-identical in every band. The world-space
        // walk covers image ∩ clip only (the v1 natural-size policy: the
        // clip is the widget rect — see ir module docs).
        //
        // Clip-stack handling (module docs): rect-only stacks tighten the
        // exact integer walk bounds; rounded stacks fold the A8 clip mask
        // into the source alpha with d255 rounding.
        let (iw, ih) = (image.w() as i32, image.h() as i32);
        let mut x0 = x.max(clip.x);
        let mut y0 = y.max(clip.y);
        let mut x1 = (x + iw).min(clip.x + clip.w as i32);
        let mut y1 = (y + ih).min(clip.y + clip.h as i32);
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
            for wx in x0..x1 {
                let lx = wx + oxi;
                if lx < 0 || lx >= pm_w {
                    continue;
                }
                // Source pixel: image-local row/col (in-bounds by the
                // intersection above; buffer length by RgbaImage::new).
                let si = (((wy - y) * iw + (wx - x)) * 4) as usize;
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

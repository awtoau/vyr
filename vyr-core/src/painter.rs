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

use crate::{Canvas, OpClass, Rect, RenderStats, Rgb};
use tiny_skia::{
    FillRule, GradientStop, LinearGradient, Paint, PathBuilder, Pixmap, Point, SpreadMode,
    Transform,
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

pub struct TinySkiaCanvas {
    pixmap: Pixmap,
    area: Rect,
    stats: RenderStats,
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
        })
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

    fn fill(&mut self, contours: &[&[(f32, f32)]], color: Rgb, alpha: u8) {
        if let Some(path) = self.path_from(contours) {
            let paint = Self::paint_for(color, alpha);
            self.pixmap.fill_path(
                &path,
                &paint,
                FillRule::Winding,
                Transform::identity(),
                None,
            );
        }
    }

    fn count(&mut self, class: OpClass, r: Rect) {
        // Clamp the op's bbox to this band — counts approximate pixels this
        // band actually touched (exact span counting lands in F2).
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
        assert!(buf.len() >= stride * h, "buffer too small for band");
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
    fn fill_rrect(&mut self, r: Rect, radius: u32, color: Rgb, alpha: u8) {
        let rad = Self::clamp_radius(r, radius);
        let pts = rrect_points(r.x as f32, r.y as f32, r.w as f32, r.h as f32, rad);
        self.fill(&[&pts], color, alpha);
        self.count(Self::class_for(alpha), r);
    }

    fn stroke_rrect(&mut self, r: Rect, radius: u32, width: u32, color: Rgb, alpha: u8) {
        // Stroke centred on the contour, built as outer+inner polygons.
        let w2 = width as f32 / 2.0;
        let rad = Self::clamp_radius(r, radius);
        let (x, y, w, h) = (r.x as f32, r.y as f32, r.w as f32, r.h as f32);
        let outer_rad = if rad > 0.0 { rad + w2 } else { 0.0 };
        let outer = rrect_points(x - w2, y - w2, w + 2.0 * w2, h + 2.0 * w2, outer_rad);
        let iw = w - 2.0 * w2;
        let ih = h - 2.0 * w2;
        if iw <= 0.0 || ih <= 0.0 {
            // Stroke swallows the interior — it's a fill of the outer shape.
            self.fill(&[&outer], color, alpha);
        } else {
            let mut inner = rrect_points(x + w2, y + w2, iw, ih, (rad - w2).max(0.0));
            inner.reverse(); // opposite winding cuts the hole
            self.fill(&[&outer, &inner], color, alpha);
        }
        self.count(Self::class_for(alpha), r);
    }

    fn disc(&mut self, cx: i32, cy: i32, radius: u32, color: Rgb, alpha: u8) {
        let pts = circle_points(cx, cy, radius as f32);
        self.fill(&[&pts], color, alpha);
        let d = radius * 2;
        self.count(
            Self::class_for(alpha),
            Rect {
                x: cx - radius as i32,
                y: cy - radius as i32,
                w: d,
                h: d,
            },
        );
    }

    fn ring(&mut self, cx: i32, cy: i32, radius: u32, width: u32, color: Rgb, alpha: u8) {
        // Annulus: stroke centred on `radius`, as outer+inner contours.
        let w2 = width as f32 / 2.0;
        let outer_r = radius as f32 + w2;
        let inner_r = radius as f32 - w2;
        let outer = circle_points(cx, cy, outer_r);
        if inner_r <= 0.0 {
            self.fill(&[&outer], color, alpha);
        } else {
            let mut inner = circle_points(cx, cy, inner_r);
            inner.reverse();
            self.fill(&[&outer, &inner], color, alpha);
        }
        let pad = radius + width.div_ceil(2);
        let d = pad * 2;
        self.count(
            Self::class_for(alpha),
            Rect {
                x: cx - pad as i32,
                y: cy - pad as i32,
                w: d,
                h: d,
            },
        );
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
        let w2 = width as f32 / 2.0;
        let (px, py) = (-dy / len * w2, dx / len * w2);
        let quad = [
            q2(fx0 + px, fy0 + py),
            q2(fx1 + px, fy1 + py),
            q2(fx1 - px, fy1 - py),
            q2(fx0 - px, fy0 - py),
        ];
        self.fill(&[&quad], color, alpha);
        let pad = width.div_ceil(2) as i32;
        self.count(
            Self::class_for(alpha),
            Rect {
                x: x0.min(x1) - pad,
                y: y0.min(y1) - pad,
                w: (x0.max(x1) - x0.min(x1)) as u32 + width,
                h: (y0.max(y1) - y0.min(y1)) as u32 + width,
            },
        );
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
        self.pixmap.fill_path(
            &path,
            &paint,
            FillRule::Winding,
            Transform::identity(),
            None,
        );
        // Gradient spans both classes; attribute by alpha like plain fills.
        self.count(Self::class_for(alpha), r);
    }

    fn stats(&self) -> RenderStats {
        self.stats
    }
}

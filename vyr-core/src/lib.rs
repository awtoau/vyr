//! vyr-core — the IR-native render core.
//!
//! Pre-F1 skeleton: this crate pins the architecture (the types below are the
//! contract), not behaviour. See `docs/plan.md` — F1 implements the painter,
//! F3 the render tree. Invariants I1–I8 in the plan govern everything here;
//! the two encoded already:
//!
//! - **I1**: [`render`] takes a band (`area`) and a caller-provided buffer.
//!   There is no full-frame entry point; a full frame is `area = screen`.
//! - **I7**: `no_std + alloc`. No filesystem, clock, thread, or std-only deps.
//!   Frame *timing* therefore lives in the shell (cli/bench); core counts
//!   pixels and ops ([`RenderStats`]).

#![no_std]
#![forbid(unsafe_code)]

extern crate alloc;

pub mod assets;
pub mod demo;
pub mod dirty;
pub mod ir;
mod painter;
pub mod text;
pub use assets::{Assets, RgbaImage};
pub use dirty::dirty_rects;
pub use painter::TinySkiaCanvas;
pub use text::{Fonts, GlyphMask, PlacedGlyph};

/// Integer pixel rectangle. `x`/`y` may be negative (a band fully above or
/// left of the origin is valid during scrolling).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Rect {
    pub x: i32,
    pub y: i32,
    pub w: u32,
    pub h: u32,
}

impl Rect {
    /// Axis-aligned intersection; empty results come out with
    /// `w == 0 || h == 0` (positioned at the clamped corner).
    pub fn intersect(self, b: Rect) -> Rect {
        let x0 = self.x.max(b.x);
        let y0 = self.y.max(b.y);
        let x1 = (self.x + self.w as i32).min(b.x + b.w as i32);
        let y1 = (self.y + self.h as i32).min(b.y + b.h as i32);
        Rect {
            x: x0,
            y: y0,
            w: (x1 - x0).max(0) as u32,
            h: (y1 - y0).max(0) as u32,
        }
    }

    /// Smallest rect covering both. An empty rect still contributes its
    /// corner POSITION (callers unioning paint boxes treat a zero-size box
    /// as a point — conservative, never lossy).
    pub fn union(self, b: Rect) -> Rect {
        let x0 = self.x.min(b.x);
        let y0 = self.y.min(b.y);
        let x1 = (self.x + self.w as i32).max(b.x + b.w as i32);
        let y1 = (self.y + self.h as i32).max(b.y + b.h as i32);
        Rect {
            x: x0,
            y: y0,
            w: (x1 - x0) as u32,
            h: (y1 - y0) as u32,
        }
    }

    /// No pixels?
    pub fn is_empty(self) -> bool {
        self.w == 0 || self.h == 0
    }

    /// Grow by `m` px on every side.
    pub fn inflate(self, m: u32) -> Rect {
        Rect {
            x: self.x - m as i32,
            y: self.y - m as i32,
            w: self.w + 2 * m,
            h: self.h + 2 * m,
        }
    }

    /// Do the rects intersect or share an edge/corner? (The dirty-rect
    /// merge predicate: touching repaint regions coalesce.)
    pub fn touches(self, b: Rect) -> bool {
        self.x <= b.x + b.w as i32
            && b.x <= self.x + self.w as i32
            && self.y <= b.y + b.h as i32
            && b.y <= self.y + self.h as i32
    }
}

/// 24-bit colour. vyr renders RGB888; pixel-format conversion is a painter
/// concern, never a widget concern.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Rgb {
    pub r: u8,
    pub g: u8,
    pub b: u8,
}

/// Draw-op classes for the always-compiled per-class pixel counters
/// (invariant I3; feeds the op-cost-breakdown overlay, F10).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OpClass {
    OpaqueFill,
    AlphaBlend,
    Blit,
    Glyph,
    AaEdge,
}

/// Render quality tier (F16, #16): a SMALL DISCRETE ENUM, never a float, and
/// **every tier is individually deterministic** (same input + same tier =
/// same bytes, banding included). [`Quality::Exact`] is the DEFAULT and the
/// oracle/conformance mode — the float-AA tiny-skia path, byte-stable against
/// the F1 goldens. [`Quality::Draft`] is the **fully integer, no-AA** fast path
/// for budgeted MCU runtime: EVERY paint op rasterizes as direct integer spans
/// (no path, no coverage, no tiny-skia anywhere), recovering the per-pixel cost
/// AA spends — and because there is no AA fringe, Draft also drops the per-band
/// overscan gutter. Draft pixels DIFFER from Exact (hard edges, no AA) — that is
/// the tier's deliberate trade; Draft has its OWN goldens, never compared to
/// Exact. Room is left for a future `Fast` (half-density flattening) — not
/// built yet (#16).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Quality {
    /// Float-AA tiny-skia path. The default everywhere oracle-facing.
    #[default]
    Exact,
    /// Fully integer, no-AA fast path, gutter-less. ALL ops (opaque +
    /// translucent rects, rounded fills + rounded strokes, disc/ring/line,
    /// gradient, glyph + image blits) rasterize as direct integer spans — no
    /// tiny-skia on the paint path. The only fallback is an op meeting a ROUNDED
    /// clip overlap (kept exact via the mask path) and a translucent gradient/
    /// stroke (rare). Coverage is measurable ([`RenderStats::fastpath_pixels`]).
    Draft,
}

/// Always-compiled render counters (invariant I3): what the benches read,
/// what the farm reply reports, what the debug HUD displays. Pixel counts,
/// not timings — core has no clock (I7).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct RenderStats {
    pub pixels_written: u64,
    /// Indexed by [`OpClass`] discriminant order.
    pub pixels_by_class: [u64; 5],
    pub bands_rendered: u32,
    /// Total area (px) of the dirty regions an INCREMENTAL repaint covered:
    /// Σ of the merged [`dirty_rects`] areas, set by
    /// [`ir::Request::render_incremental`]. Plain band renders leave it 0
    /// (no frame diff was in play). The F10 HUD's "dirty-area %" numerator.
    pub dirty_area_px: u64,
    pub peak_alloc_bytes: u64,
    /// F5 glyph-cache counters (from the caller's [`Fonts`], cumulative over
    /// its lifetime): one-time outline→mask rasterizations, masks held, and
    /// A8 bytes held. `glyphs_rasterized` is the exactly-once proof the F5
    /// acceptance tests assert on; `glyph_cache_bytes` is the F9 RAM number.
    pub glyphs_rasterized: u64,
    pub glyph_cache_entries: u64,
    pub glyph_cache_bytes: u64,
    /// F16 (#16): pixels that took the [`Quality::Draft`] integer no-AA fast
    /// path (direct span fills) rather than the tiny-skia/Exact fallback.
    /// Always 0 under [`Quality::Exact`]. The honesty number for the Draft
    /// measurement: fast-path coverage % = `fastpath_pixels / pixels_written`
    /// (#21 honest-delta pattern — say exactly how much of the frame the fast
    /// path actually carried, and how much fell back).
    pub fastpath_pixels: u64,
}

/// Hard render failure (invariant I6): an unknown widget type or a missing
/// asset errors *before* pixels; a blank render is a bug, never a fallback.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RenderError {
    UnknownWidget(alloc::string::String),
    BadIr(alloc::string::String),
    MissingAsset(alloc::string::String),
    Unimplemented(&'static str),
    /// F5 honest text failures (I6): a family not in the caller's [`Fonts`]
    /// (message names the fonts that ARE registered), a codepoint the font
    /// cannot map (never a tofu box), or unparseable font bytes.
    UnknownFont(alloc::string::String),
    MissingGlyph(alloc::string::String),
    BadFont(alloc::string::String),
    /// F6 honest registration failure: pixel bytes that don't match their
    /// claimed dimensions, junk sizes, duplicate/empty names — rejected at
    /// [`Assets::register`] time, never discovered mid-blit.
    BadAsset(alloc::string::String),
}

/// The painter seam. Implementations: tiny-skia (F1, [`TinySkiaCanvas`]),
/// vello_cpu cross-check (F8), DMA2D/Chrom-ART hybrid (F13). Widgets paint
/// ONLY through this trait.
///
/// All coordinates are WORLD coordinates — a canvas instance represents one
/// band (invariant I1) and translates internally. The v1 primitive set per
/// the plan; `blit_image` joins the trait with its implementation (F6) — no
/// dead unimplementable surface before then.
pub trait Canvas {
    /// Push a rounded-rect clip (WORLD coords) onto the clip stack: every
    /// subsequent op is limited to its interior, intersected with all
    /// previously pushed clips — and with the band, which is the OUTERMOST
    /// clip by construction (I1). `radius` clamps like [`Canvas::fill_rrect`]
    /// (half the short side). Clipping must be band-deterministic: the same
    /// world pixel sees the same clip coverage in every band (the painter's
    /// mask is built from the same quantized-polygon discipline as paint
    /// geometry — see `painter` module docs).
    fn push_clip(&mut self, r: Rect, radius: u32);
    /// Pop the most recent [`Canvas::push_clip`]. Popping an empty stack is
    /// a caller bug (the tiny-skia painter panics on it — honest failure).
    fn pop_clip(&mut self);
    fn fill_rrect(&mut self, r: Rect, radius: u32, color: Rgb, alpha: u8);
    fn stroke_rrect(&mut self, r: Rect, radius: u32, width: u32, color: Rgb, alpha: u8);
    fn disc(&mut self, cx: i32, cy: i32, radius: u32, color: Rgb, alpha: u8);
    fn ring(&mut self, cx: i32, cy: i32, radius: u32, width: u32, color: Rgb, alpha: u8);
    // A line is naturally two endpoints + width + colour + alpha; grouping
    // them into structs would be ceremony, not clarity.
    #[allow(clippy::too_many_arguments)]
    fn line(&mut self, x0: i32, y0: i32, x1: i32, y1: i32, width: u32, color: Rgb, alpha: u8);
    /// Two-stop linear gradient fill of a (rounded) rect, horizontal or
    /// vertical — the IR's background-gradient shape.
    fn fill_linear_gradient(
        &mut self,
        r: Rect,
        radius: u32,
        from: Rgb,
        to: Rgb,
        vertical: bool,
        alpha: u8,
    );
    /// Blit a run of cached A8 glyph masks (F5) in `color`. Placements are
    /// INTEGER world positions (whole-pixel pens — the band-equivalence
    /// contract; see `text` module docs); blending must be deterministic
    /// integer math, counted into [`OpClass::Glyph`].
    ///
    /// Returns the run's INK bounding box in WORLD coordinates (`None` if
    /// nothing inked) — "you rendered text, here is exactly where it
    /// landed". Computed from glyph GEOMETRY, never from band clipping, so
    /// the same run reports the same box in every band.
    fn glyph_run(&mut self, glyphs: &[PlacedGlyph<'_>], color: Rgb, alpha: u8) -> Option<Rect>;
    /// Blit a caller-registered RGBA image (F6) SCALED into the world-space
    /// `dst` rect (the FIT-TO-CELL destination the IR computed — preserve
    /// aspect, centred; see `ir::fit_to_cell` + `ir` module docs), limited to
    /// the world-space `clip` rect (the widget rect; `dst ⊆ clip`, so the
    /// letterbox bands paint nothing). Resampling is NEAREST-NEIGHBOUR via
    /// fixed-point integer source mapping that depends ONLY on world position,
    /// so band equivalence holds by the same induction as the 1:1 glyph blit
    /// (Qt's SMOOTH/bilinear is a future F16 quality knob — geometry matches Qt
    /// now, resampling quality is a separate axis). Blending is deterministic
    /// integer source-over (straight-alpha source onto the band), counted into
    /// [`OpClass::Blit`].
    fn blit_image(&mut self, dst: Rect, image: &RgbaImage, clip: Rect);
    fn stats(&self) -> RenderStats;
}

/// THE entry point (invariant I1). Renders the IR request (JSON:
/// `{"w","h","root"}`, `root` in `vy_` vocabulary, verbatim — no lowering)
/// clipped to `area` into the caller-provided `buf` (RGB888, `stride` bytes
/// per row). A full frame is `area = whole screen` — there is no other code
/// path. See [`ir`] for the widget subset and the honest-failure rules.
///
/// `fonts` is the caller-owned registry + glyph cache (F5): register font
/// bytes once, keep the instance across renders so glyphs rasterize exactly
/// once per (font, size, codepoint). Text in the IR with no matching
/// registered font is a hard [`RenderError::UnknownFont`].
///
/// `assets` is the caller-owned image registry (F6): decoded RGBA pixels
/// registered under the IR's verbatim `src` names (decode lives in the
/// shell — invariant I7). A `src` not registered is a hard
/// [`RenderError::MissingAsset`].
pub fn render_with(
    ir_json: &str,
    fonts: &mut Fonts,
    assets: &Assets,
    area: Rect,
    buf: &mut [u8],
    stride: usize,
) -> Result<RenderStats, RenderError> {
    ir::Request::parse(ir_json)?.render_with(fonts, assets, area, buf, stride)
}

/// [`render_with`] at an explicit [`Quality`] tier (F16, #16). `Quality::Exact`
/// is byte-identical to [`render_with`] (the oracle path); `Quality::Draft`
/// takes the integer no-AA fast path where it applies — different bytes, its
/// own goldens.
pub fn render_with_quality(
    ir_json: &str,
    fonts: &mut Fonts,
    assets: &Assets,
    area: Rect,
    buf: &mut [u8],
    stride: usize,
    quality: Quality,
) -> Result<RenderStats, RenderError> {
    ir::Request::parse(ir_json)?.render_with_quality(fonts, assets, area, buf, stride, quality)
}

/// [`render_with`] with an empty asset registry — convenience for image-free
/// scenes. Image-bearing IR through this path hard-errors with
/// [`RenderError::MissingAsset`] (honest: there are no assets to blit).
pub fn render_with_fonts(
    ir_json: &str,
    fonts: &mut Fonts,
    area: Rect,
    buf: &mut [u8],
    stride: usize,
) -> Result<RenderStats, RenderError> {
    render_with(ir_json, fonts, &Assets::new(), area, buf, stride)
}

/// [`render_with`] with empty font AND asset registries — convenience for
/// text-free, image-free scenes. Text hard-errors
/// [`RenderError::UnknownFont`], images [`RenderError::MissingAsset`]
/// (honest: there is nothing to draw them with).
pub fn render(
    ir_json: &str,
    area: Rect,
    buf: &mut [u8],
    stride: usize,
) -> Result<RenderStats, RenderError> {
    let mut fonts = Fonts::new();
    render_with(ir_json, &mut fonts, &Assets::new(), area, buf, stride)
}

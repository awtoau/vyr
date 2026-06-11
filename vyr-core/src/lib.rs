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

pub mod demo;
pub mod ir;
mod painter;
pub mod text;
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

/// Always-compiled render counters (invariant I3): what the benches read,
/// what the farm reply reports, what the debug HUD displays. Pixel counts,
/// not timings — core has no clock (I7).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct RenderStats {
    pub pixels_written: u64,
    /// Indexed by [`OpClass`] discriminant order.
    pub pixels_by_class: [u64; 5],
    pub bands_rendered: u32,
    pub dirty_area_px: u64,
    pub peak_alloc_bytes: u64,
    /// F5 glyph-cache counters (from the caller's [`Fonts`], cumulative over
    /// its lifetime): one-time outline→mask rasterizations, masks held, and
    /// A8 bytes held. `glyphs_rasterized` is the exactly-once proof the F5
    /// acceptance tests assert on; `glyph_cache_bytes` is the F9 RAM number.
    pub glyphs_rasterized: u64,
    pub glyph_cache_entries: u64,
    pub glyph_cache_bytes: u64,
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
    fn glyph_run(&mut self, glyphs: &[PlacedGlyph<'_>], color: Rgb, alpha: u8);
    fn stats(&self) -> RenderStats;
}

/// THE entry point (invariant I1). Renders the IR request (JSON:
/// `{"w","h","root"}`, `root` in `vy_` vocabulary, verbatim — no lowering)
/// clipped to `area` into the caller-provided `buf` (RGB888, `stride` bytes
/// per row). A full frame is `area = whole screen` — there is no other code
/// path. See [`ir`] for the widget subset and the honest-failure rules
/// (image pre-F6 = hard error).
///
/// `fonts` is the caller-owned registry + glyph cache (F5): register font
/// bytes once, keep the instance across renders so glyphs rasterize exactly
/// once per (font, size, codepoint). Text in the IR with no matching
/// registered font is a hard [`RenderError::UnknownFont`].
pub fn render_with_fonts(
    ir_json: &str,
    fonts: &mut Fonts,
    area: Rect,
    buf: &mut [u8],
    stride: usize,
) -> Result<RenderStats, RenderError> {
    ir::Request::parse(ir_json)?.render_with_fonts(fonts, area, buf, stride)
}

/// [`render_with_fonts`] with an empty font registry — convenience for
/// text-free scenes. Text-bearing IR through this path hard-errors with
/// [`RenderError::UnknownFont`] (honest: there are no fonts to draw with).
pub fn render(
    ir_json: &str,
    area: Rect,
    buf: &mut [u8],
    stride: usize,
) -> Result<RenderStats, RenderError> {
    let mut fonts = Fonts::new();
    render_with_fonts(ir_json, &mut fonts, area, buf, stride)
}

//! F5 — runtime text: ONE font format (TTF/OTF) via skrifa, rasterized once
//! into an A8 glyph cache, then rendered as pure cached blits.
//!
//! ## Architecture (docs/plan.md §F5)
//!
//! [`Fonts`] is the caller-owned font registry + glyph cache. Invariant I7:
//! core never touches the filesystem — callers (vyr-cli, tests, an MCU boot
//! path reading flash) hand in raw font BYTES via [`Fonts::register`]. Each
//! `(font, size_px, codepoint)` is rasterized ONCE ([`Fonts::prepare_run`])
//! to a [`GlyphMask`]; rendering ([`Fonts::placed_run`] →
//! [`crate::Canvas::glyph_run`]) only ever blits cached masks.
//!
//! ## Band equivalence (why this is safe next to the painter's polygon rule)
//!
//! The painter feeds tiny-skia ONLY polygons because clipping subdivides
//! curves and perturbs AA (see `painter.rs`). Glyph outlines ARE curves —
//! and that is fine HERE because rasterization happens in glyph-local space
//! on a dedicated mask pixmap sized to the outline's bounds: no band, no
//! clip, no transform ever touches it, so the cached mask is bit-identical
//! wherever the glyph lands. Bands only see the mask blitted at INTEGER pen
//! positions with deterministic integer blending (`painter::glyph_run`).
//! Whole-pixel pens are load-bearing: subpixel positioning would need a
//! per-phase mask (breaking cache sharing) and fractional band offsets
//! (breaking band equivalence).
//!
//! ## Metrics model (the classic baked-font model)
//!
//! Advances, ascent and descent are quantized to whole pixels at cache time
//! — integer pen accumulation, integer run widths, integer baselines.
//! Identical to how baked bitmap fonts (LVGL `lv_font_conv`, TGX
//! typographies) behave, and exactly what keeps every glyph on the pixel
//! grid. Outlines are drawn UNHINTED (deterministic; no hinting VM state).
//!
//! ## Scope (deliberately narrow)
//!
//! Single-style, single-direction, single-line runs: charmap lookup +
//! advances. NO shaping, NO bidi, NO wrapping (parley/HarfBuzz territory
//! until the IR grows rich text). Kerning is skipped: Roboto carries its
//! pair kerning in GPOS, which is shaping territory — skrifa exposes no
//! flat kern API. Recorded on issue #5.
//!
//! ## Honest failure (invariant I6)
//!
//! A codepoint the font cannot map is [`RenderError::MissingGlyph`] — never
//! a tofu box the IR didn't ask for. An unregistered family is
//! [`RenderError::UnknownFont`] naming the fonts that ARE available.

use alloc::collections::BTreeMap;
use alloc::format;
use alloc::string::String;
use alloc::vec::Vec;

use skrifa::instance::{LocationRef, Size};
use skrifa::outline::{DrawSettings, OutlinePen};
use skrifa::{FontRef, MetadataProvider};
use tiny_skia::{FillRule, Paint, PathBuilder, Pixmap, Transform};

use crate::{Rect, RenderError};

/// One cached, rasterized glyph: an A8 coverage mask plus integer placement
/// metrics. `left`/`top` position the mask relative to the pen point on the
/// BASELINE (`top` is negative above it); `advance` moves the pen to the
/// next glyph. All whole pixels (see module docs).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GlyphMask {
    pub left: i32,
    pub top: i32,
    pub w: u32,
    pub h: u32,
    pub advance: i32,
    /// Row-major coverage, `w * h` bytes (empty for blank glyphs like space).
    pub a8: Vec<u8>,
}

/// A cached glyph placed at an INTEGER world position (`x`/`y` = mask
/// top-left). What [`crate::Canvas::glyph_run`] consumes.
#[derive(Clone, Copy, Debug)]
pub struct PlacedGlyph<'a> {
    pub x: i32,
    pub y: i32,
    pub mask: &'a GlyphMask,
}

/// What [`Fonts::measure`] returns: the layout box AND the ink box of a text
/// run, both in whole pixels, both relative to the pen origin / baseline.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TextMeasure {
    /// Layout width: the pen advance (trailing spaces included).
    pub advance: i32,
    /// Above the baseline, positive.
    pub ascent: i32,
    /// Below the baseline, NEGATIVE (font convention).
    pub descent: i32,
    /// Where paint actually lands, relative to (pen origin, baseline) —
    /// `x`/`y` may be negative (left side bearing / above baseline). `None`
    /// when nothing inks.
    pub ink: Option<Rect>,
}

impl TextMeasure {
    /// Layout line height: ascent to descent (no leading — single-line runs).
    pub fn height(&self) -> i32 {
        self.ascent - self.descent
    }
}

/// Whole-pixel metrics of a prepared run: `width` = sum of advances;
/// `ascent` (positive, above baseline) and `descent` (negative, below) come
/// from the font's global metrics at this size.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RunMetrics {
    pub width: i32,
    pub ascent: i32,
    pub descent: i32,
}

impl RunMetrics {
    /// Line height: ascent to descent (no leading — single-line runs).
    pub fn height(&self) -> i32 {
        self.ascent - self.descent
    }
}

/// (font index, size px, codepoint) — the cache key. Size is part of the key
/// because masks are rasterized per pixel size (no scaling of cached masks).
type GlyphKey = (u16, u32, u32);

struct Font {
    /// Registry name: lowercased family (`"roboto"`).
    name: String,
    /// The raw TTF/OTF bytes. `FontRef` borrows these per use — re-parsing
    /// the table directory is cheap and keeps `Fonts` self-contained
    /// (no self-referential lifetimes).
    data: Vec<u8>,
}

/// The caller-owned font registry + glyph cache (see module docs).
///
/// Long-lived by design: keep ONE `Fonts` across renders so the
/// once-per-(font,size,cp) rasterization guarantee spans frames, not just
/// bands. Counters ([`Fonts::rasterized`], [`Fonts::cache_entries`],
/// [`Fonts::cache_bytes`]) are surfaced into [`crate::RenderStats`] by the
/// IR render path.
#[derive(Default)]
pub struct Fonts {
    fonts: Vec<Font>,
    cache: BTreeMap<GlyphKey, GlyphMask>,
    rasterized: u64,
    cache_bytes: u64,
}

impl Fonts {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register a font under `name` (matched case-insensitively; stored
    /// lowercased). `bytes` must be a parseable single-font TTF/OTF —
    /// validated here so a corrupt file fails at registration, not mid-render.
    pub fn register(&mut self, name: &str, bytes: Vec<u8>) -> Result<(), RenderError> {
        let name = name.to_lowercase();
        if name.is_empty() {
            return Err(RenderError::BadFont("empty font name".into()));
        }
        if self.fonts.iter().any(|f| f.name == name) {
            return Err(RenderError::BadFont(format!(
                "font {name:?} already registered"
            )));
        }
        if self.fonts.len() >= u16::MAX as usize {
            return Err(RenderError::BadFont("font registry full".into()));
        }
        FontRef::new(&bytes)
            .map_err(|e| RenderError::BadFont(format!("font {name:?} unparseable: {e}")))?;
        self.fonts.push(Font { name, data: bytes });
        Ok(())
    }

    /// Registered family names (lowercase, registration order).
    pub fn names(&self) -> impl Iterator<Item = &str> {
        self.fonts.iter().map(|f| f.name.as_str())
    }

    /// One-time outline→mask rasterizations performed so far (cumulative —
    /// the exactly-once counter the F5 acceptance tests assert on).
    pub fn rasterized(&self) -> u64 {
        self.rasterized
    }

    /// Cached masks currently held.
    pub fn cache_entries(&self) -> u64 {
        self.cache.len() as u64
    }

    /// A8 bytes currently held by cached masks (the RAM-budget number the
    /// F9 embedded spike reads).
    pub fn cache_bytes(&self) -> u64 {
        self.cache_bytes
    }

    fn index(&self, family: &str) -> Result<usize, RenderError> {
        let family = family.to_lowercase();
        self.fonts
            .iter()
            .position(|f| f.name == family)
            .ok_or_else(|| {
                let available: Vec<&str> = self.fonts.iter().map(|f| f.name.as_str()).collect();
                RenderError::UnknownFont(format!(
                    "font {family:?} not registered (available: {available:?})"
                ))
            })
    }

    /// Ensure every glyph of `text` at `size_px` is cached (rasterizing each
    /// missing `(font,size,cp)` exactly once) and return the run's
    /// whole-pixel metrics. Call before [`Fonts::placed_run`].
    pub fn prepare_run(
        &mut self,
        family: &str,
        size_px: u32,
        text: &str,
    ) -> Result<RunMetrics, RenderError> {
        let idx = self.index(family)?;
        // Field-level borrows: `self.fonts` immutably (FontRef + error
        // names), `self.cache`/counters mutably — disjoint by construction.
        let fname = self.fonts[idx].name.as_str();
        let font = FontRef::new(&self.fonts[idx].data)
            .map_err(|e| RenderError::BadFont(format!("font {fname:?} failed to re-parse: {e}")))?;
        let size = Size::new(size_px as f32);
        let metrics = font.metrics(size, LocationRef::default());
        let charmap = font.charmap();
        let glyph_metrics = font.glyph_metrics(size, LocationRef::default());
        let outlines = font.outline_glyphs();

        let mut width = 0i64;
        for cp in text.chars() {
            if cp.is_control() {
                // Single-line runs only (module docs): a control char has no
                // glyph and no layout meaning here — honest error, not a
                // silently swallowed newline.
                return Err(RenderError::BadIr(format!(
                    "control character U+{:04X} in text run (F5 renders single-line runs)",
                    cp as u32
                )));
            }
            let key: GlyphKey = (idx as u16, size_px, cp as u32);
            if let Some(mask) = self.cache.get(&key) {
                width += mask.advance as i64;
                continue;
            }
            let gid = charmap.map(cp).ok_or_else(|| {
                RenderError::MissingGlyph(format!(
                    "font {fname:?} has no glyph for U+{:04X} {cp:?} (invariant I6: no tofu)",
                    cp as u32
                ))
            })?;
            let advance = glyph_metrics.advance_width(gid).ok_or_else(|| {
                RenderError::MissingGlyph(format!(
                    "font {fname:?} has no advance for U+{:04X} {cp:?}",
                    cp as u32
                ))
            })?;
            let mask = raster_glyph(&outlines, size, gid, advance)?;
            width += mask.advance as i64;
            self.cache_bytes += mask.a8.len() as u64;
            self.rasterized += 1;
            self.cache.insert(key, mask);
        }
        Ok(RunMetrics {
            width: width as i32,
            ascent: libm::roundf(metrics.ascent) as i32,
            descent: libm::roundf(metrics.descent) as i32,
        })
    }

    /// Measure `text` WITHOUT rendering: the caller-facing sizing API.
    ///
    /// Returns both boxes a caller needs:
    /// - the **layout box** (`advance` × `ascent`..`descent`) — what you
    ///   reserve / centre on; trailing spaces count, overhangs don't;
    /// - the **ink box** (`ink`) — where paint would actually land,
    ///   relative to the pen origin (x) and the BASELINE (y, so `ink.y` is
    ///   typically negative). `None` when nothing inks (empty / all-space).
    ///
    /// Measuring warms the glyph cache, so a subsequent render of the same
    /// text rasterizes nothing new — measure-then-render is the intended
    /// cheap pattern.
    pub fn measure(
        &mut self,
        family: &str,
        size_px: u32,
        text: &str,
    ) -> Result<TextMeasure, RenderError> {
        let m = self.prepare_run(family, size_px, text)?;
        let placed = self.placed_run(family, size_px, text, 0, 0)?;
        let mut ink: Option<(i32, i32, i32, i32)> = None;
        for g in &placed {
            let (x0, y0) = (g.x, g.y);
            let (x1, y1) = (g.x + g.mask.w as i32, g.y + g.mask.h as i32);
            ink = Some(match ink {
                None => (x0, y0, x1, y1),
                Some((a, b, c, d)) => (a.min(x0), b.min(y0), c.max(x1), d.max(y1)),
            });
        }
        Ok(TextMeasure {
            advance: m.width,
            ascent: m.ascent,
            descent: m.descent,
            ink: ink.map(|(x0, y0, x1, y1)| Rect {
                x: x0,
                y: y0,
                w: (x1 - x0) as u32,
                h: (y1 - y0) as u32,
            }),
        })
    }

    /// Lay out `text` as cached-mask placements: integer pen starting at
    /// `(origin_x, baseline_y)`, advancing by each mask's whole-pixel
    /// advance. Requires a prior [`Fonts::prepare_run`] for the same
    /// (family, size, text) — an uncached glyph here is a hard error, never
    /// a silent skip. Blank glyphs (space) advance the pen but emit nothing.
    pub fn placed_run<'a>(
        &'a self,
        family: &str,
        size_px: u32,
        text: &str,
        origin_x: i32,
        baseline_y: i32,
    ) -> Result<Vec<PlacedGlyph<'a>>, RenderError> {
        let idx = self.index(family)?;
        let mut placed = Vec::with_capacity(text.len());
        let mut pen_x = origin_x;
        for cp in text.chars() {
            let key: GlyphKey = (idx as u16, size_px, cp as u32);
            let mask = self.cache.get(&key).ok_or_else(|| {
                RenderError::MissingGlyph(format!(
                    "U+{:04X} not cached — placed_run without prepare_run is a bug",
                    cp as u32
                ))
            })?;
            if mask.w > 0 && mask.h > 0 {
                placed.push(PlacedGlyph {
                    x: pen_x + mask.left,
                    y: baseline_y + mask.top,
                    mask,
                });
            }
            pen_x += mask.advance;
        }
        Ok(placed)
    }
}

/// tiny-skia path sink for skrifa outlines. Fonts are y-UP; raster space is
/// y-DOWN — flip y here so the path is already in mask-local orientation.
struct FlipPen {
    pb: PathBuilder,
}

impl OutlinePen for FlipPen {
    fn move_to(&mut self, x: f32, y: f32) {
        self.pb.move_to(x, -y);
    }
    fn line_to(&mut self, x: f32, y: f32) {
        self.pb.line_to(x, -y);
    }
    fn quad_to(&mut self, cx0: f32, cy0: f32, x: f32, y: f32) {
        self.pb.quad_to(cx0, -cy0, x, -y);
    }
    fn curve_to(&mut self, cx0: f32, cy0: f32, cx1: f32, cy1: f32, x: f32, y: f32) {
        self.pb.cubic_to(cx0, -cy0, cx1, -cy1, x, -y);
    }
    fn close(&mut self) {
        self.pb.close();
    }
}

/// Rasterize ONE glyph outline to an A8 mask — the once-per-(font,size,cp)
/// step. The outline (curves and all) goes to tiny-skia HERE, in glyph-local
/// space on a pixmap sized to the outline bounds: no band, no clip — see the
/// module's band-equivalence note. Coverage is the alpha channel of a white
/// AA fill (non-zero winding, the TrueType/CFF rule).
fn raster_glyph(
    outlines: &skrifa::OutlineGlyphCollection,
    size: Size,
    gid: skrifa::GlyphId,
    advance: f32,
) -> Result<GlyphMask, RenderError> {
    let advance_px = libm::roundf(advance) as i32;
    let blank = |adv: i32| GlyphMask {
        left: 0,
        top: 0,
        w: 0,
        h: 0,
        advance: adv,
        a8: Vec::new(),
    };
    let Some(outline) = outlines.get(gid) else {
        // Mapped by cmap but no outline entry: a blank glyph (space behaves
        // this way in some fonts) — advance-only, nothing inked.
        return Ok(blank(advance_px));
    };
    let mut pen = FlipPen {
        pb: PathBuilder::new(),
    };
    outline
        .draw(
            DrawSettings::unhinted(size, LocationRef::default()),
            &mut pen,
        )
        .map_err(|e| RenderError::BadFont(format!("outline draw failed for {gid}: {e}")))?;
    let Some(path) = pen.pb.finish() else {
        return Ok(blank(advance_px)); // empty outline (space)
    };
    let b = path.bounds();
    // Integer mask rect containing the outline, +1px pad as belt-and-braces
    // for edge AA (pad pixels carry zero coverage; the blit skips them).
    let left = libm::floorf(b.left()) as i32 - 1;
    let top = libm::floorf(b.top()) as i32 - 1;
    let right = libm::ceilf(b.right()) as i32 + 1;
    let bottom = libm::ceilf(b.bottom()) as i32 + 1;
    let (w, h) = ((right - left) as u32, (bottom - top) as u32);
    let Some(mut pixmap) = Pixmap::new(w, h) else {
        return Err(RenderError::BadFont(format!(
            "glyph mask allocation failed for {gid} ({w}x{h})"
        )));
    };
    let mut paint = Paint::default();
    paint.set_color_rgba8(255, 255, 255, 255);
    paint.anti_alias = true;
    pixmap.fill_path(
        &path,
        &paint,
        FillRule::Winding,
        Transform::from_translate(-(left as f32), -(top as f32)),
        None,
    );
    let a8: Vec<u8> = pixmap.pixels().iter().map(|p| p.alpha()).collect();
    Ok(GlyphMask {
        left,
        top,
        w,
        h,
        advance: advance_px,
        a8,
    })
}

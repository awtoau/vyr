//! F3-lite — IR ingestion + the non-text widget subset, rendered natively.
//!
//! vyr speaks the `vy_` vocabulary directly (IR JSON verbatim, no lowering).
//! The request is what the vyvanse farm's `_prepare` sends:
//!
//! ```json
//! {"w": 120, "h": 120, "root": {"name": "view", "attrs": {...}, "children": [...]}}
//! ```
//!
//! `root` is the screen's view element (`XmlElement.to_dict()` shape); child
//! x/y are relative to the parent (accumulated to absolute here, matching the
//! TGX client's `_emit_element`).
//!
//! ## What renders vs errors (honest failure, invariant I6)
//!
//! F5 brings glyphs: `vy_label` / `vy_lcd` / `vy_button` render their `text`
//! through the caller's [`Fonts`] (registry + glyph cache — see
//! [`crate::text`]). F6 brings images: `vy_image` / `vy_imagebutton` blit
//! their `src` from the caller's [`Assets`] registry (decoded RGBA — see
//! [`crate::assets`]; decode lives in the shell, invariant I7). F4 brings
//! composites: `vy_chart` paints a single-series line/bar chart from the IR's
//! declared `points` (NOT invented data — see [`paint_chart`]). Widgets still
//! needing marks or structure beyond a single text run
//! (dropdown/table/toggle_label) stay hard errors — the farm surfaces them as
//! honest skips, exactly like TGX's `unsupported` set.
//! Unknown `vy_` names are `UnknownWidget` errors before any pixel. A `text`
//! whose font isn't registered, a codepoint the font can't map, a `src` not
//! in the registry (`MissingAsset`), or a `vy_image` with NO `src` at all
//! (`BadIr` — an image widget with nothing to show is junk IR, not an empty
//! box) are hard errors — never a tofu box or a blank.
//!
//! ## Image model (F6 — tunable CSS object-fit, default contain = the spec)
//!
//! `vy_image` / `vy_imagebutton` map the asset W×H into the widget box
//! (bx,by,bw,bh) by a CSS-style **object-fit** mode — the IR `fit` attr (alias
//! `object_fit`), DEFAULT **contain** when absent. Five modes ([`Fit`] /
//! [`object_fit`]), each a different dest-rect + clip; the painter's nearest
//! blit takes any dst + clip and stays band-exact, so the modes need NO painter
//! change:
//!
//! - **contain** (DEFAULT): `scale = min(bw/W, bh/H)`, centred, letterboxed
//!   (`dst ⊆ box`). THE canonical spec behaviour — matches **Qt** exactly
//!   (KeepAspectRatio + centre), the #325/#6 LOCKED REFERENCE (the only backend
//!   already fit-to-cell correctly; the #325 premise was inverted). Default
//!   when the IR names no `fit` — so existing IR renders byte-identical.
//! - **cover**: `scale = max(bw/W, bh/H)`, centred, CROPPED — `dst ⊇ box`, the
//!   clip (= box) trims the overflow. Fills the box, preserves aspect.
//! - **none**: natural size (scale 1), centred — crop if bigger than the box,
//!   letterbox if smaller. The OLD pre-fit behaviour, now explicit.
//! - **fill**: stretch to the box exactly, IGNORING aspect (non-uniform x/y
//!   scale — the painter's source map already supports it).
//! - **scale-down**: `min(contain, none)` — natural size unless it exceeds the
//!   box, then contain (`scale = min(1.0, contain_scale)`).
//!
//! An UNKNOWN `fit` value is a hard `BadIr` naming the accepted set (I6 — never
//! silently default). Centre rule (all but fill): `dst_x = bx + (bw − dst_w)/2`
//! — MAY be negative for cover / oversized-none, which is correct (the clip
//! trims). **LVGL renders native-size** (no `lv_image_set_scale` → the
//! contain-diverger, filed #331 — theirs); TGX/Flutter paint placeholders.
//! Resampling is NEAREST-NEIGHBOUR (fixed-point integer source map — band-exact,
//! painter docs); Qt's SMOOTH/bilinear is a future **F16 quality knob**. The
//! fit MODE (which rect) and the resampling QUALITY (which filter) are
//! ORTHOGONAL axes — F16 composes with any fit mode. Paint order: declared
//! `background` fill UNDER, the fitted blit, then declared border ON TOP. The
//! `src` string is looked up in [`Assets`] VERBATIM — name resolution/decode is
//! the shell's job (see `crate::assets` module docs).
//!
//! ## Clip model (F3 clip stack)
//!
//! **Containers clip their children** — the LVGL / TouchGFX / Qt default
//! (LVGL: `LV_OBJ_FLAG_OVERFLOW_VISIBLE` is off by default; TGX/Qt clip
//! children to the parent). Concretely:
//!
//! - A **BOXES-family node** (frame/container/scroll/stack/dialog/…) with
//!   children pushes a clip of its own rect + `radius` before walking them,
//!   pops after — radius-aware, so a child overflowing a rounded corner is
//!   trimmed along the arc. `vy_scroll` therefore clips by construction.
//! - **`vy_button` clips its children too** (LVGL clips; the centred label
//!   chrome fits inside anyway, so this only shows when IR places an
//!   oversized child in a button).
//! - The **screen root never pushes** — the band IS the outermost clip by
//!   construction (invariant I1); clipping composes with banding because
//!   both are world-space region intersections.
//! - Leaf and composite widgets (slider/switch/gauge/radio/…) don't clip:
//!   they have no children in the v1 vocabulary. Revisit when composites
//!   grow real child nodes.
//!
//! The push happens for culled (out-of-band) containers as well — a clip is
//! world-space state, and a band that cannot see the container must still
//! clip the container's children identically, or banded and full-frame
//! renders would disagree (the band-equivalence contract).
//! [`crate::dirty::dirty_rects`] shares the same `clips_children` predicate,
//! so repaint regions and paint reality cannot drift apart.
//!
//! ## Text model (F5, deliberately minimal)
//!
//! Single-style single-line runs, integer pens (see [`crate::text`]). Font
//! selection: backend-neutral `font_family` + `font_size` attrs first, else
//! the LVGL-lowered `style_text_font` name (`roboto_14` / `spleen_8` —
//! `<family>_<size>`), else the documented default **roboto 14** (the
//! vyvanse IR default: FontSpec(family="Roboto", size=14)). Plain labels ink
//! from the node's top-left content corner (baseline = y + ascent — the
//! LVGL/TGX top-left convention and the #318 tight-box anchor);
//! `align="center"` centres the run in the PARENT rect (the cases.py
//! button-label contract). A label's `width`/`height` are box geometry only
//! — runs are still never wrapped, and a label does NOT clip its own run to
//! its rect, but runs are now **clipped by parent containers** (the F3 clip
//! stack upgrade): a long label inside a frame trims at the frame edge
//! instead of painting past it. Rich text stays out of scope.
//!
//! ## Box model — fills, opacity, borders (F8 Gate 1, awto-vyvanse#321)
//!
//! A box fill is painted at the IR's declared opacity, NOT forced opaque: the
//! walk threads [`Node::fill_alpha`] (the `opacity` / 8-hex `#RRGGBBAA` /
//! `style_bg_opa` attr) into the fill alpha, source-over the
//! backdrop/card painted first. (This fixes the FADE-FLAT bug — vyr used to
//! hardcode `0xFF` and drop every partial alpha.)
//!
//! Borders are drawn **INSIDE** the box so the OUTER edge lands ON the
//! declared bound (the TGX/Qt model — see [`draw_inside_border`]): an
//! axis-aligned (radius-0) border is four crisp filled edge-rects (no AA
//! wash, no straddle); a rounded border is the inset AA stroke. (This fixes
//! the border-STRADDLE — a 2px border used to grow the box to 102×102 — and
//! the thin-border AA-WASH — a 1px border used to blend fill+border instead
//! of reading its declared colour.)
//!
//! ## Chrome policy
//!
//! Plain boxes are IR-authoritative (paint nothing the IR didn't say — I5).
//! REAL widgets (slider/switch/arc…) carry widget-default chrome like every
//! backend's real widgets do (the #281 neutral-theme rule: real widgets keep
//! their defaults). The defaults below are LVGL-flavoured and are F4's
//! refinement surface against geometry_measure/colour_check:
//! track `#E6E6E6`, accent `#2196F3`, knob white + 1px `#B0B0B0` ring.
//! Text ink defaults to **black `#000000`** when neither the node nor an
//! ancestor button carries `color` — a widget-default chrome entry like the
//! slider chrome: every backend has a compiled-in default text colour (LVGL
//! theme ink, Qt palette text, TGX typography default), and black-on-paper
//! is the neutral reading. A `vy_button`'s `color` is its TEXT colour,
//! inherited by its label children (the cases.py contract).
//! The SCREEN backdrop defaults to near-white `(250,250,250)` when the root
//! carries no `background` — mirroring the TGX render server's per-render
//! backdrop wipe so an empty screen reads as paper, not black.

use alloc::format;
use alloc::string::{String, ToString};
use alloc::vec::Vec;
use serde::Deserialize;

use crate::{Assets, Canvas, Fonts, Quality, Rect, RenderError, RenderStats, Rgb, TinySkiaCanvas};

#[derive(Debug, Deserialize)]
pub struct Node {
    pub name: String,
    #[serde(default)]
    pub attrs: serde_json::Map<String, serde_json::Value>,
    #[serde(default)]
    pub children: Vec<Node>,
}

/// IR schema versions vyr knows how to render. The machine contract with
/// vyvanse (its `vyvanse/ir/model.py SCHEMA_VERSION`, coordinated on the
/// awto-vyvanse#321 handoff board): the field is OPTIONAL on the wire (older
/// senders omit it), but when present it must match — a mismatched version
/// is a hard `BadIr` naming what vyr accepts, never a silent best-effort
/// render of a vocabulary vyr may misread.
pub const ACCEPTED_SCHEMA_VERSIONS: &[&str] = &["0.6-vyvanse"];

#[derive(Debug, Deserialize)]
pub struct Request {
    pub w: u32,
    pub h: u32,
    pub root: Node,
    #[serde(default)]
    pub schema_version: Option<String>,
}

impl Request {
    pub fn parse(ir_json: &str) -> Result<Request, RenderError> {
        let req: Request = serde_json::from_str(ir_json)
            .map_err(|e| RenderError::BadIr(format!("request parse: {e}")))?;
        if let Some(v) = &req.schema_version
            && !ACCEPTED_SCHEMA_VERSIONS.contains(&v.as_str())
        {
            return Err(RenderError::BadIr(format!(
                "schema_version {v:?} not accepted (vyr renders {ACCEPTED_SCHEMA_VERSIONS:?})"
            )));
        }
        Ok(req)
    }

    /// Render one band (`area`, world coords within the `w×h` screen) into
    /// the caller's RGB888 buffer — THE entry point shape (invariant I1).
    /// `fonts` is the caller-owned registry + glyph cache (F5), `assets` the
    /// caller-owned decoded-image registry (F6); font counters are surfaced
    /// into the returned stats.
    pub fn render_with(
        &self,
        fonts: &mut Fonts,
        assets: &Assets,
        area: Rect,
        buf: &mut [u8],
        stride: usize,
    ) -> Result<RenderStats, RenderError> {
        self.render_with_quality(fonts, assets, area, buf, stride, Quality::Exact)
    }

    /// [`Request::render_with`] at an explicit [`Quality`] tier (F16, #16).
    /// `Quality::Exact` is the oracle path (byte-identical to `render_with`);
    /// `Quality::Draft` builds a Draft canvas — the walk is unchanged, the
    /// per-op fast/slow decision lives in the painter (see its struct docs).
    pub fn render_with_quality(
        &self,
        fonts: &mut Fonts,
        assets: &Assets,
        area: Rect,
        buf: &mut [u8],
        stride: usize,
        quality: Quality,
    ) -> Result<RenderStats, RenderError> {
        let mut canvas = TinySkiaCanvas::new_with_quality(area, quality)
            .ok_or_else(|| RenderError::BadIr("pixmap allocation failed".into()))?;
        // Screen backdrop: the root's background, else the near-white paper
        // default (see module docs) — painted across the whole screen.
        let backdrop = self.root.color("background").unwrap_or(Rgb {
            r: 250,
            g: 250,
            b: 250,
        });
        let screen = Rect {
            x: 0,
            y: 0,
            w: self.w,
            h: self.h,
        };
        canvas.fill_rrect(screen, 0, backdrop, 0xFF);
        for child in &self.root.children {
            walk(child, screen, &mut canvas, fonts, assets, None)?;
        }
        let mut stats = canvas.finish_into_rgb888(buf, stride);
        stats.glyphs_rasterized = fonts.rasterized();
        stats.glyph_cache_entries = fonts.cache_entries();
        stats.glyph_cache_bytes = fonts.cache_bytes();
        Ok(stats)
    }

    /// [`Request::render_with`] with an empty asset registry — image-free
    /// scenes only (images hard-error `MissingAsset`, honestly).
    pub fn render_with_fonts(
        &self,
        fonts: &mut Fonts,
        area: Rect,
        buf: &mut [u8],
        stride: usize,
    ) -> Result<RenderStats, RenderError> {
        self.render_with(fonts, &Assets::new(), area, buf, stride)
    }

    /// [`Request::render_with`] with empty registries — text-free,
    /// image-free scenes only (text hard-errors `UnknownFont`, images
    /// `MissingAsset`, honestly).
    pub fn render(
        &self,
        area: Rect,
        buf: &mut [u8],
        stride: usize,
    ) -> Result<RenderStats, RenderError> {
        let mut fonts = Fonts::new();
        self.render_with(&mut fonts, &Assets::new(), area, buf, stride)
    }
}

// Widget-default chrome (REAL widgets only — see module docs).
const TRACK: Rgb = Rgb {
    r: 0xE6,
    g: 0xE6,
    b: 0xE6,
};
const ACCENT: Rgb = Rgb {
    r: 0x21,
    g: 0x96,
    b: 0xF3,
};
const KNOB: Rgb = Rgb {
    r: 0xFF,
    g: 0xFF,
    b: 0xFF,
};
const KNOB_RING: Rgb = Rgb {
    r: 0xB0,
    g: 0xB0,
    b: 0xB0,
};
/// vy_chart grid div-line colour (light neutral, the lv_chart look).
const GRID: Rgb = Rgb {
    r: 0xD0,
    g: 0xD0,
    b: 0xD0,
};
/// vy_chart plot background when the IR declares no `background`.
const CHART_BG: Rgb = Rgb {
    r: 0xFF,
    g: 0xFF,
    b: 0xFF,
};
/// vy_chart frame/border colour (the plot box outline).
const CHART_FRAME: Rgb = Rgb {
    r: 0x90,
    g: 0x90,
    b: 0x90,
};
/// Default text ink when the IR names no `color` — black-on-paper, a
/// documented widget-default chrome entry (see module docs).
const INK: Rgb = Rgb { r: 0, g: 0, b: 0 };
/// Default font request when the IR names none — the vyvanse IR default
/// (FontSpec family="Roboto" size=14; see module docs).
const DEFAULT_FONT: &str = "roboto";
const DEFAULT_FONT_SIZE: u32 = 14;

impl Node {
    fn raw(&self, key: &str) -> Option<&serde_json::Value> {
        self.attrs.get(key)
    }

    /// String view of an attr (numbers stringified, like the JSON the farm
    /// emits). Public so the shell's asset pre-scan reads `src` with EXACTLY
    /// the semantics the render path will use (vyr-cli `load_assets`).
    pub fn str_attr(&self, key: &str) -> Option<String> {
        match self.raw(key)? {
            serde_json::Value::String(s) => Some(s.clone()),
            serde_json::Value::Number(n) => Some(n.to_string()),
            _ => None,
        }
    }

    fn f32_attr(&self, key: &str, default: f32) -> f32 {
        self.str_attr(key)
            .and_then(|s| s.trim().parse::<f32>().ok())
            .unwrap_or(default)
    }

    // i32/u32 attr views are pub(crate): the dirty-rect diff (crate::dirty)
    // must resolve geometry with EXACTLY the walk's semantics.
    pub(crate) fn i32_attr(&self, key: &str, default: i32) -> i32 {
        self.f32_attr(key, default as f32) as i32
    }

    pub(crate) fn u32_attr(&self, key: &str, default: u32) -> u32 {
        self.f32_attr(key, default as f32).max(0.0) as u32
    }

    /// `#RRGGBB` / `0xRRGGBB` / `RRGGBB` attr → colour.
    fn color(&self, key: &str) -> Option<Rgb> {
        let s = self.str_attr(key)?;
        let hexs = s.trim();
        let hexs = hexs
            .strip_prefix('#')
            .or_else(|| hexs.strip_prefix("0x"))
            .or_else(|| hexs.strip_prefix("0X"))
            .unwrap_or(hexs);
        // 6-hex `#RRGGBB` or 8-hex `#RRGGBBAA` (alpha read separately by
        // color_alpha — here we take only the RGB triple).
        let rgb = match hexs.len() {
            6 => u32::from_str_radix(hexs, 16).ok()?,
            8 => u32::from_str_radix(hexs, 16).ok()? >> 8,
            _ => return None,
        };
        Some(Rgb {
            r: (rgb >> 16) as u8,
            g: (rgb >> 8) as u8,
            b: rgb as u8,
        })
    }

    /// Corner radius: semantic `radius` or already-lowered `style_radius`.
    pub(crate) fn radius(&self) -> u32 {
        self.u32_attr("radius", self.u32_attr("style_radius", 0))
    }

    /// The FILL alpha (0..=255) for a node's `background`. The IR expresses
    /// partial opacity three ways, in precedence order (the farm's `_prepare`
    /// + the conformance fade fixtures, awto-vyvanse#321):
    ///
    /// 1. **`opacity`** — the backend-neutral semantic attr, `0..=255` (int)
    ///    OR `0.0..=1.0` (float); the fade fixtures use `"opacity": "128"`.
    /// 2. **8-hex `background`** (`#RRGGBBAA` / `0xRRGGBBAA`) — alpha in the
    ///    colour literal itself.
    /// 3. **`style_bg_opa`** — the LVGL-lowered fill opacity, `0..=255`.
    ///
    /// Default 255 (opaque) when none is present. NOTE: `style_bg_opa` is only
    /// consulted as the LVGL fill-opacity when it is the *only* alpha signal;
    /// the card fixtures carry `style_bg_opa: 255` (opaque) and the fade
    /// fixtures carry both `style_bg_opa: 255` AND `opacity: 128` — `opacity`
    /// wins, so the fade is honoured (the card stays opaque).
    fn fill_alpha(&self) -> u8 {
        if let Some(s) = self.str_attr("opacity") {
            let t = s.trim();
            if let Ok(f) = t.parse::<f32>() {
                // 0..=1 floats scale to 0..=255; values >1 are already 0..=255.
                let v = if f <= 1.0 {
                    (f * 255.0 + 0.5) as i32
                } else {
                    f as i32
                };
                return v.clamp(0, 255) as u8;
            }
        }
        if let Some(a) = self.color_alpha("background") {
            return a;
        }
        self.u32_attr("style_bg_opa", 255).min(255) as u8
    }

    /// The alpha byte of an 8-hex `#RRGGBBAA` / `0xRRGGBBAA` colour attr, if
    /// the literal carries one (6-hex literals have no embedded alpha).
    fn color_alpha(&self, key: &str) -> Option<u8> {
        let s = self.str_attr(key)?;
        let h = s.trim();
        let h = h
            .strip_prefix('#')
            .or_else(|| h.strip_prefix("0x"))
            .or_else(|| h.strip_prefix("0X"))
            .unwrap_or(h);
        if h.len() != 8 {
            return None;
        }
        let v = u32::from_str_radix(h, 16).ok()?;
        Some(v as u8)
    }

    /// The CSS object-fit mode for a `vy_image` / `vy_imagebutton` — the IR
    /// `fit` attr (or its `object_fit` alias), [`Fit::Contain`] when absent
    /// (the canonical spec default). An UNKNOWN value is a hard `BadIr` (I6 —
    /// never silently default to contain on junk).
    fn fit(&self) -> Result<Fit, RenderError> {
        match self.str_attr("fit").or_else(|| self.str_attr("object_fit")) {
            Some(s) => Fit::parse(&s),
            None => Ok(Fit::Contain),
        }
    }

    /// value/min/max → fraction of range, clamped 0..=1.
    fn fraction(&self) -> f32 {
        let min = self.f32_attr("min", 0.0);
        let max = self.f32_attr("max", 100.0);
        let v = self.f32_attr("value", 0.0);
        if max <= min {
            return 0.0;
        }
        ((v - min) / (max - min)).clamp(0.0, 1.0)
    }

    /// The chart series: `points` parsed as a CSV of numbers (the single
    /// y-series). Empty tokens (a trailing comma, an empty string) are
    /// skipped; a non-empty non-numeric token is junk IR — a hard error, not
    /// a silently-dropped point. Absent `points` ⇒ empty series (the chart
    /// renders just its frame + grid). vyr paints what the IR declares — it
    /// never invents demo data the way each backend currently does.
    fn chart_points(&self) -> Result<Vec<f32>, RenderError> {
        let Some(s) = self.str_attr("points") else {
            return Ok(Vec::new());
        };
        let mut out = Vec::new();
        for tok in s.split(',') {
            let t = tok.trim();
            if t.is_empty() {
                continue;
            }
            match t.parse::<f32>() {
                Ok(v) => out.push(v),
                Err(_) => {
                    return Err(RenderError::BadIr(format!(
                        "vy_chart points has a non-numeric value {t:?}"
                    )));
                }
            }
        }
        Ok(out)
    }
}

/// Box-family names: plain containers/shapes — IR-authoritative chrome only.
const BOXES: &[&str] = &[
    "view",
    "vy_frame",
    "vy_shape",
    "vy_container",
    "vy_scroll",
    "vy_stack",
    "vy_dialog",
    "vy_list",
    "vy_roller",
];
/// Widgets that need STRUCTURE beyond marks + a text run (option lists,
/// row/column layout, plot geometry): still hard errors — F4's
/// labelled-placeholder wave covers them (coordinated on awto-vyvanse#321,
/// because flipping error→placeholder changes the farm contract the fixture
/// generator and smoke encode). `vy_radio`/`vy_checkbox` graduated to real
/// composites in F4 wave 1.
const NEEDS_STRUCTURE: &[&str] = &["vy_toggle_label", "vy_dropdown", "vy_table"];

// F4 wave-1 mark palette — matches vyvanse's #313 primitive composites
// (cases.py `_RADIO_RING`/`_RADIO_DOT`), NOT vyr's LVGL-flavoured widget
// accent: the #313 premise is cross-backend consistency, and the four
// backends already render this exact ring+dot at 1.000 SSIM. Using the same
// ink means vyr's NATIVE vy_radio agrees with the lowered composite the
// other backends draw.
const MARK_ACCENT: Rgb = Rgb {
    r: 0x1E,
    g: 0x5A,
    b: 0xA8,
};

/// Band-culling safety margin, in pixels: a widget whose rect, inflated by
/// this, misses the band gets its PAINT skipped (path building is the cost
/// the F2 scaling table exposed — the per-band fixed cost). The margin
/// covers everything a widget may paint outside its declared rect: centred
/// border strokes (half a border width), AA, knob/ring overhang, glyph side
/// bearings. VALIDATION never culls — a band that can't see a broken widget
/// still errors on it, so banded and full-frame renders agree on errors —
/// and the band-equivalence goldens enforce that culling never changes a
/// delivered byte. 32 px is deliberately generous; tightening it is a
/// measured change, not a guess.
const CULL_MARGIN: i32 = 32;

/// CSS-style `object-fit` mode for `vy_image` / `vy_imagebutton` (the IR `fit`
/// attr, default [`Fit::Contain`]). Selects how the asset W×H maps into the
/// widget box; the painter's nearest blit already handles an arbitrary dst rect
/// plus clip (over-frame dst LARGER than the box, non-uniform x/y scale), so
/// every mode stays band-exact with no painter change — only the dest-rect/clip
/// geometry differs per mode (see [`object_fit`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fit {
    /// Scale to fit INSIDE preserving aspect, centred, letterboxed (`dst ⊆
    /// box`). THE canonical spec behaviour (Qt KeepAspectRatio, #325/#6) — the
    /// DEFAULT when the IR names no `fit`.
    Contain,
    /// Scale to COVER the box preserving aspect, centred, cropped (`dst ⊇ box`,
    /// overflow trimmed by the clip).
    Cover,
    /// Natural size (scale 1), centred — crop if bigger than the box,
    /// letterbox if smaller. The OLD pre-fit behaviour, now explicit.
    None,
    /// Stretch to the box exactly, IGNORING aspect (non-uniform x/y scale).
    Fill,
    /// `min(contain, none)` — natural size UNLESS it exceeds the box, then
    /// contain. `scale = min(1.0, contain_scale)`.
    ScaleDown,
}

impl Fit {
    /// Parse the IR `fit` / `object_fit` attr (CSS spellings, case-insensitive).
    /// An UNKNOWN value is a hard [`RenderError::BadIr`] naming the accepted set
    /// (I6 — never silently default). The DEFAULT (attr absent) is resolved by
    /// the caller, not here.
    fn parse(s: &str) -> Result<Fit, RenderError> {
        match s.trim().to_ascii_lowercase().as_str() {
            "contain" => Ok(Fit::Contain),
            "cover" => Ok(Fit::Cover),
            "none" => Ok(Fit::None),
            "fill" => Ok(Fit::Fill),
            "scale-down" | "scale_down" | "scaledown" => Ok(Fit::ScaleDown),
            other => Err(RenderError::BadIr(format!(
                "unknown fit {other:?} (accepted: contain, cover, none, fill, scale-down)"
            ))),
        }
    }
}

/// The geometry of a [`Fit`] mode: the destination rect AND clip for an asset
/// `aw`×`ah` drawn into widget box `b`. The painter's nearest blit takes
/// `(dst, image, clip)` and resamples band-exact for ANY dst + clip (the
/// per-pixel source map `sx = (wx-dst.x)·W/dst.w` is a pure integer function of
/// world position, painter docs), so every mode below is band-exact with NO
/// painter change — only the dst/clip differs:
///
/// - **Contain** (default): `scale = min(b.w/aw, b.h/ah)`, centred, `dst ⊆ b`,
///   clip = `b` (letterboxed — the bands paint nothing).
/// - **Cover**: `scale = max(b.w/aw, b.h/ah)`, centred, `dst ⊇ b` (LARGER than
///   the box), clip = `b` (the blit trims the overflow).
/// - **None**: natural size (scale 1), centred, clip = `b` (crop if bigger,
///   letterbox if smaller).
/// - **Fill**: `dst = b` exactly (non-uniform x/y scale — aspect ignored),
///   clip = `b`.
/// - **ScaleDown**: `scale = min(1.0, contain_scale)`, centred, clip = `b`.
///
/// Centre rule for all (except Fill): `dst.x = b.x + (b.w − dst.w)/2`, which
/// MAY be negative for cover / oversized-none — that is correct, the clip
/// (always `b`) trims it. The scale uses deterministic [`libm`] float for the
/// min/max-and-round, but the RESULT is an integer rect — band-invariant input
/// to the painter. Degenerate boxes / assets (0 area) yield an empty dst (the
/// blit no-ops). Resampling QUALITY (nearest vs the future F16 bilinear) is
/// ORTHOGONAL to the fit mode — fit picks the rect, quality picks the filter.
fn object_fit(fit: Fit, b: Rect, aw: u32, ah: u32) -> (Rect, Rect) {
    let empty = Rect {
        x: b.x,
        y: b.y,
        w: 0,
        h: 0,
    };
    if b.w == 0 || b.h == 0 || aw == 0 || ah == 0 {
        return (empty, b);
    }
    // Fill: stretch to the box exactly — no aspect, no centring.
    if fit == Fit::Fill {
        return (b, b);
    }
    let (bw, bh) = (b.w as f32, b.h as f32);
    let (afw, afh) = (aw as f32, ah as f32);
    let scale = match fit {
        Fit::Contain => libm::fminf(bw / afw, bh / afh),
        Fit::Cover => libm::fmaxf(bw / afw, bh / afh),
        Fit::None => 1.0,
        Fit::ScaleDown => libm::fminf(1.0, libm::fminf(bw / afw, bh / afh)),
        Fit::Fill => unreachable!("Fill handled above"),
    };
    let dw = libm::roundf(afw * scale).max(0.0) as u32;
    let dh = libm::roundf(afh * scale).max(0.0) as u32;
    // Contain / ScaleDown can never exceed the box (scale ≤ box/asset); cap the
    // rounding overshoot so they stay dst ⊆ b. Cover / None may exceed it — the
    // clip (always b) trims, so DON'T cap those.
    let (dw, dh) = match fit {
        Fit::Contain | Fit::ScaleDown => (dw.min(b.w), dh.min(b.h)),
        _ => (dw, dh),
    };
    let dst = Rect {
        // Centre — (b.w − dw) is signed, so an oversized dst centres NEGATIVE
        // (cover / oversized-none); the clip = b trims it. Integer /2 truncates
        // toward zero, deterministic + band-invariant.
        x: b.x + (b.w as i32 - dw as i32) / 2,
        y: b.y + (b.h as i32 - dh as i32) / 2,
        w: dw,
        h: dh,
    };
    (dst, b)
}

/// Does `r`, inflated by [`CULL_MARGIN`], intersect the band `area`?
fn visible_in(r: Rect, area: Rect) -> bool {
    let x0 = r.x - CULL_MARGIN;
    let y0 = r.y - CULL_MARGIN;
    let x1 = r.x + r.w as i32 + CULL_MARGIN;
    let y1 = r.y + r.h as i32 + CULL_MARGIN;
    x1 > area.x && x0 < area.x + area.w as i32 && y1 > area.y && y0 < area.y + area.h as i32
}

/// Does this node clip its children? — the single source of truth, shared by
/// the paint walk and [`crate::dirty`]'s diff (repaint regions and paint
/// reality must agree). BOXES-family containers and `vy_button` clip when
/// they HAVE children; the screen root is handled by the band itself (module
/// docs, clip model).
pub(crate) fn clips_children(n: &Node) -> bool {
    !n.children.is_empty() && (BOXES.contains(&n.name.as_str()) || n.name == "vy_button")
}

/// Walk one node: `parent` is the parent's ABSOLUTE rect (child x/y are
/// relative to it; `align="center"` text centres within it), `ink` the
/// inherited text colour (a `vy_button`'s `color` flows to its labels).
fn walk(
    n: &Node,
    parent: Rect,
    c: &mut TinySkiaCanvas,
    fonts: &mut Fonts,
    assets: &Assets,
    ink: Option<Rgb>,
) -> Result<(), RenderError> {
    let x = parent.x + n.i32_attr("x", 0);
    let y = parent.y + n.i32_attr("y", 0);
    let w = n.u32_attr("width", 0);
    let h = n.u32_attr("height", 0);
    let r = Rect { x, y, w, h };
    let name = n.name.as_str();
    let mut child_ink = ink;
    // Band-bbox culling (the F2-recorded optimization): skip PAINT for
    // widgets that cannot touch this band; never skip validation. Children
    // still walk — they may lie outside the parent's rect.
    let visible = visible_in(r, c.area());

    if NEEDS_STRUCTURE.contains(&name) {
        return Err(RenderError::Unimplemented(
            "widget needs marks/structure beyond a text run (F4 composites pending)",
        ));
    }

    match name {
        _ if !visible => match name {
            // Validation parity for invisible nodes: the same checks a
            // visible render performs, minus the painting. Text still
            // PREPARES (font/glyph errors + cache warmth are band-invariant
            // state); images still resolve their src.
            "vy_label" | "vy_lcd" | "vy_radio" | "vy_checkbox" => {
                draw_text_prepare_only(n, ink, fonts)?;
            }
            "vy_button" => {
                child_ink = n.color("color").or(ink);
            }
            "vy_image" | "vy_imagebutton" => {
                let Some(src) = n.str_attr("src") else {
                    return Err(RenderError::BadIr(format!(
                        "{name} {:?} has no src attr (an image with nothing to show is junk IR)",
                        n.str_attr("name").unwrap_or_default()
                    )));
                };
                // Validation parity: an unknown `fit` must error in EVERY band,
                // visible or culled (banded and full-frame agree on errors).
                n.fit()?;
                assets.get(&src)?;
            }
            "vy_video" | "vy_widget" | "vy_canvas" => {
                return Err(RenderError::Unimplemented(
                    "no primitive for this widget yet (placeholder lands with F4 captions)",
                ));
            }
            _ if BOXES.contains(&name) => {}
            "vy_circle" | "vy_ellipse" | "vy_line" | "vy_slider" | "vy_progress" | "vy_bar"
            | "vy_toggle" | "vy_switch" | "vy_gauge" | "vy_arc" => {}
            // Parse parity: a junk points/chart_type/range errors in every band.
            "vy_chart" => {
                chart_params(n)?;
            }
            other => {
                return Err(RenderError::UnknownWidget(other.to_string()));
            }
        },
        _ if BOXES.contains(&name) => paint_box(n, r, c),
        "vy_label" | "vy_lcd" => {
            // Box chrome only if the IR declared it (I5), then the run.
            // vy_lcd: a *text* lcd renders as a plain run (the vyvanse
            // gallery lcd arrives as a primitive 7-seg composite and never
            // hits this arm; an IR that says `text` gets text).
            paint_box(n, r, c);
            draw_text(n, r, parent, ink, c, fonts)?;
        }
        "vy_button" => {
            // The box exactly like a frame (background/radius/border as
            // declared); `color` is the button's TEXT colour, inherited by
            // its label children (see module docs).
            paint_box(n, r, c);
            child_ink = n.color("color").or(ink);
        }
        "vy_circle" | "vy_ellipse" => {
            // Disc/stadium: max-radius box (the IR disc lowering). Fill from
            // `background`; nothing painted if the IR gave no fill (I5).
            if let Some(fill) = n.color("background") {
                let rad = h.min(w) / 2;
                c.fill_rrect(r, rad, fill, n.fill_alpha());
            }
            paint_border(n, r, h.min(w) / 2, c);
        }
        "vy_line" => {
            if let Some(fill) = n.color("background") {
                c.fill_rrect(r, 0, fill, n.fill_alpha());
            }
        }
        "vy_slider" | "vy_progress" | "vy_bar" => {
            let rad = h / 2;
            c.fill_rrect(r, rad, TRACK, 0xFF);
            let fillw = (w as f32 * n.fraction()) as u32;
            if fillw > 0 {
                c.fill_rrect(Rect { x, y, w: fillw, h }, rad, ACCENT, 0xFF);
            }
            if name == "vy_slider" && w > h {
                // Knob centred on the fill end, kept fully inside the track.
                let kr = rad;
                let kx = (x + fillw as i32).clamp(x + kr as i32, x + w as i32 - kr as i32);
                c.disc(kx, y + rad as i32, kr, KNOB, 0xFF);
                c.ring(kx, y + rad as i32, kr, 1, KNOB_RING, 0xFF);
            }
        }
        "vy_toggle" | "vy_switch" => {
            let rad = h / 2;
            let on = n.f32_attr("value", 0.0) != 0.0;
            c.fill_rrect(r, rad, if on { ACCENT } else { TRACK }, 0xFF);
            let kr = rad.saturating_sub(2).max(2);
            let kx = if on {
                x + w as i32 - rad as i32
            } else {
                x + rad as i32
            };
            c.disc(kx, y + rad as i32, kr, KNOB, 0xFF);
            c.ring(kx, y + rad as i32, kr, 1, KNOB_RING, 0xFF);
        }
        "vy_radio" | "vy_checkbox" => {
            draw_mark_widget(n, r, name == "vy_radio", ink, c, fonts)?;
        }
        "vy_chart" => paint_chart(n, r, c)?,
        "vy_gauge" | "vy_arc" => {
            // Full ring: the circular track (the TGX gauge shape). Sweep arcs
            // need an arc primitive — F4. IR `color` wins over the track grey.
            let d = w.min(h);
            // ring stroke ~ d/10, floored at 4 so small gauges stay visible.
            let stroke = (d / 10).max(4);
            let radius = d / 2 - stroke / 2;
            let col = n.color("color").unwrap_or(TRACK);
            c.ring(
                x + w as i32 / 2,
                y + h as i32 / 2,
                radius,
                stroke,
                col,
                0xFF,
            );
        }
        "vy_image" | "vy_imagebutton" => {
            // F6: the asset FIT-TO-CELL — scaled to fit INSIDE the widget rect
            // preserving aspect (scale = min(bw/W, bh/H)), CENTRED, the empty
            // bands LETTERBOXED (painted nothing — the box background/card
            // shows through). Qt is the locked reference (#325/#6: KeepAspect +
            // centre; LVGL native-size is theirs, #331). Declared fill under,
            // declared border over. No `src` = junk IR (I6): an image widget
            // with nothing to show is a BadIr, not an empty box.
            let Some(src) = n.str_attr("src") else {
                return Err(RenderError::BadIr(format!(
                    "{name} {:?} has no src attr (an image with nothing to show is junk IR)",
                    n.str_attr("name").unwrap_or_default()
                )));
            };
            let rad = n.radius();
            let fit = n.fit()?;
            if let Some(fill) = n.color("background") {
                c.fill_rrect(r, rad, fill, n.fill_alpha());
            }
            let img = assets.get(&src)?;
            let (dst, clip) = object_fit(fit, r, img.w(), img.h());
            // clip = r always; dst ⊆ r for contain/scale-down (letterbox), dst
            // may exceed r for cover / oversized-none (the blit trims to clip).
            c.blit_image(dst, img, clip);
            paint_border(n, r, rad, c);
        }
        "vy_video" | "vy_widget" | "vy_canvas" => {
            return Err(RenderError::Unimplemented(
                "no primitive for this widget yet (placeholder lands with F4 captions)",
            ));
        }
        other => {
            return Err(RenderError::UnknownWidget(other.to_string()));
        }
    }

    // Containers clip their children (module docs, clip model). The push is
    // UNCONDITIONAL on band visibility: clips are world-space state, and a
    // band that can't see the container must still clip the container's
    // children identically (band equivalence). Own chrome above was painted
    // unclipped — a node's own rounded fill/border IS the shape, not a
    // clippee. A child error breaks out so the pop still pairs its push
    // (the canvas may outlive a failed walk in direct-Canvas callers).
    let clipping = clips_children(n);
    if clipping {
        c.push_clip(r, n.radius());
    }
    let mut result = Ok(());
    for child in &n.children {
        result = walk(child, r, c, fonts, assets, child_ink);
        if result.is_err() {
            break;
        }
    }
    if clipping {
        c.pop_clip();
    }
    result
}

/// The font a node requests: `font_family` (+ `font_size`) first, else the
/// LVGL-lowered `style_text_font` `<family>_<size>` name, else the default
/// roboto 14 (module docs). Family is matched case-insensitively by the
/// registry. A zero size is junk IR — hard error, not an invisible run.
fn font_request(n: &Node) -> Result<(String, u32), RenderError> {
    let checked = |fam: String, size: u32| {
        if size == 0 {
            return Err(RenderError::BadIr(format!(
                "font {fam:?} requested at size 0 (an invisible run is a bug, not a render)"
            )));
        }
        Ok((fam, size))
    };
    if let Some(fam) = n.str_attr("font_family") {
        return checked(fam, n.u32_attr("font_size", DEFAULT_FONT_SIZE));
    }
    if let Some(ltf) = n.str_attr("style_text_font") {
        if let Some((fam, size)) = ltf.rsplit_once('_')
            && let Ok(size) = size.parse::<u32>()
        {
            return checked(String::from(fam), size);
        }
        // No trailing _<size>: a bare family name at the default size (the
        // registry will honest-error if it isn't registered).
        return checked(ltf, DEFAULT_FONT_SIZE);
    }
    Ok((String::from(DEFAULT_FONT), DEFAULT_FONT_SIZE))
}

/// F4 wave 1: `vy_radio` / `vy_checkbox` as native composites — a square MARK
/// the widget's height on the left, the `text` label to its right,
/// vertically centred. Geometry + ink match vyvanse's #313
/// primitive-composite lowering (cases.py `_radio_composite`: ring border
/// `max(2, d/10)`, selected dot 44% of the ring diameter, accent `#1E5AA8`)
/// so vyr's native rendering and the other backends' lowered composites
/// agree by construction. `value`/`checked` (either attr) drives the mark.
fn draw_mark_widget(
    n: &Node,
    r: Rect,
    radio: bool,
    ink: Option<Rgb>,
    c: &mut TinySkiaCanvas,
    fonts: &mut Fonts,
) -> Result<(), RenderError> {
    let d = r.h.min(r.w);
    let on = n.f32_attr("value", n.f32_attr("checked", 0.0)) != 0.0;
    let (mx, my) = (r.x, r.y + (r.h as i32 - d as i32) / 2);
    if radio {
        // Outline ring at full mark size; painter strokes centred on the
        // radius, so radius = (d - bw) / 2 keeps the outer edge inside d.
        let bw = (d / 10).max(2);
        let (cx, cy) = (mx + d as i32 / 2, my + d as i32 / 2);
        c.ring(cx, cy, (d - bw) / 2, bw, MARK_ACCENT, 0xFF);
        if on {
            // The #313 dot: 44% of the ring diameter, concentric.
            let dot_d = (d * 44 / 100).max(4);
            c.disc(cx, cy, dot_d / 2, MARK_ACCENT, 0xFF);
        }
    } else {
        let mark = Rect {
            x: mx,
            y: my,
            w: d,
            h: d,
        };
        let rad = (d / 8).max(2);
        if on {
            c.fill_rrect(mark, rad, MARK_ACCENT, 0xFF);
            // The check: two strokes through the box's tick anchor points.
            let lw = (d / 8).max(2);
            let p = |fx: u32, fy: u32| (mx + (d * fx / 100) as i32, my + (d * fy / 100) as i32);
            let (ax, ay) = p(24, 52);
            let (bx, by) = p(42, 70);
            let (ex, ey) = p(76, 30);
            let white = Rgb {
                r: 0xFF,
                g: 0xFF,
                b: 0xFF,
            };
            c.line(ax, ay, bx, by, lw, white, 0xFF);
            c.line(bx, by, ex, ey, lw, white, 0xFF);
        } else {
            // Unchecked: outline only — the same accent border weight as the
            // radio ring, interior transparent (the #313 outline primitive).
            // Drawn INSIDE the mark box (the box-border model — paint_border)
            // so the outline's outer edge sits on the mark bound.
            draw_inside_border(mark, rad, (d / 10).max(2), MARK_ACCENT, c);
        }
    }
    // Label: `text` to the right of the mark, vertically centred via the
    // measurement API (gap = 6 px, the LVGL checkbox text gap).
    if let Some(text) = n.str_attr("text")
        && !text.is_empty()
    {
        let color = n.color("color").or(ink).unwrap_or(INK);
        let (family, size) = font_request(n)?;
        let m = fonts.prepare_run(&family, size, &text)?;
        let baseline = r.y + (r.h as i32 - m.height()) / 2 + m.ascent;
        let placed = fonts.placed_run(&family, size, &text, r.x + d as i32 + 6, baseline)?;
        let _ = c.glyph_run(&placed, color, 0xFF);
    }
    Ok(())
}

/// The validation-only half of [`draw_text`], for band-culled nodes: resolve
/// the font request and PREPARE the run (font/glyph errors + cache warmth
/// are band-invariant state) without placing or inking anything. Keeps
/// banded and full-frame renders agreeing on every error.
fn draw_text_prepare_only(
    n: &Node,
    _ink: Option<Rgb>,
    fonts: &mut Fonts,
) -> Result<(), RenderError> {
    let Some(text) = n.str_attr("text") else {
        return Ok(());
    };
    if text.is_empty() {
        return Ok(());
    }
    let (family, size) = font_request(n)?;
    fonts.prepare_run(&family, size, &text)?;
    Ok(())
}

/// Ink a node's `text` run (F5). No `text` attr / empty text = nothing to
/// ink (IR-authoritative absence, not an error). Placement: top-left of the
/// node's rect with baseline at `y + ascent`, or centred in `parent` when
/// `align="center"` (module docs). Glyphs come from the cache via
/// `prepare_run` (rasterize-once) + `placed_run` (integer pens).
fn draw_text(
    n: &Node,
    r: Rect,
    parent: Rect,
    ink: Option<Rgb>,
    c: &mut TinySkiaCanvas,
    fonts: &mut Fonts,
) -> Result<(), RenderError> {
    let Some(text) = n.str_attr("text") else {
        return Ok(());
    };
    if text.is_empty() {
        return Ok(());
    }
    let ink = n.color("color").or(ink).unwrap_or(INK);
    let (family, size) = font_request(n)?;
    let m = fonts.prepare_run(&family, size, &text)?;
    let (origin_x, baseline_y) = if n.str_attr("align").as_deref() == Some("center") {
        (
            parent.x + (parent.w as i32 - m.width) / 2,
            parent.y + (parent.h as i32 - m.height()) / 2 + m.ascent,
        )
    } else {
        (r.x, r.y + m.ascent)
    };
    let placed = fonts.placed_run(&family, size, &text, origin_x, baseline_y)?;
    // The returned ink bbox serves direct-Canvas callers (editor caret /
    // selection / autosize); the IR walk has nowhere to put it yet — a
    // per-widget report can ride RenderStats later if a caller needs it.
    let _ = c.glyph_run(&placed, ink, 0xFF);
    Ok(())
}

/// Plain box paint: fill only if the IR declared `background`; border only if
/// `border_width > 0`, in `border_color` (else the fill colour — never a
/// default black). The I5 discipline, same as the TGX instrument.
fn paint_box(n: &Node, r: Rect, c: &mut TinySkiaCanvas) {
    let rad = n.radius();
    if let Some(fill) = n.color("background") {
        // The IR's fill opacity (F8 fade fix, awto-vyvanse#321): the walk used
        // to hardcode 0xFF and drop every partial alpha — the FADE-FLAT bug.
        // The painter's source-over already blends correctly (F6 proves the
        // d255/premultiplied path); we just feed it the alpha the IR declared,
        // composited over the backdrop/card painted first.
        c.fill_rrect(r, rad, fill, n.fill_alpha());
    }
    paint_border(n, r, rad, c);
}

/// vy_chart parse + validation, shared by the paint and the band-invisible
/// validation arm so a junk `points`/`chart_type`/`range` errors in EVERY band
/// (banded and full-frame agree on errors, like every other widget). Returns
/// `(bar?, range_min, range_max, series)`.
fn chart_params(n: &Node) -> Result<(bool, f32, f32, Vec<f32>), RenderError> {
    let bar = match n.str_attr("chart_type").as_deref() {
        None | Some("line") => false,
        Some("bar") => true,
        Some(other) => {
            return Err(RenderError::BadIr(format!(
                "vy_chart chart_type {other:?} is not line|bar"
            )));
        }
    };
    // LVGL's default y-range is 0..100; declared range_min/range_max win.
    let rmin = n.f32_attr("range_min", 0.0);
    let rmax = n.f32_attr("range_max", 100.0);
    if !(rmax > rmin) {
        return Err(RenderError::BadIr(format!(
            "vy_chart range_max ({rmax}) must exceed range_min ({rmin})"
        )));
    }
    Ok((bar, rmin, rmax, n.chart_points()?))
}

/// vy_chart (F4 composite) — a single-series line/bar chart painted from the
/// IR's declared `points`, NOT invented demo data. Plot box (declared
/// `background` else white, + a 1 px frame), an optional grid (`div_count_x`
/// vertical / `div_count_y` horizontal interior div-lines), then the series
/// mapped into an interior inset by the marker radius: index → x across the
/// width, value → y by the `[range_min,range_max]` scale (default LVGL 0..100,
/// value clamped). `chart_type` line (default: polyline + a marker disc per
/// point — the only diagonal, hence AA in Exact / hard in Draft) or bar (a
/// column per point up from the baseline). Series colour `line_color`/`color`
/// (default ACCENT), width `line_width`. Frame + grid are axis-aligned, so they
/// are crisp 1 px FILL-rects (the `draw_inside_border` rule — a stroke would
/// straddle + AA-wash). The whole composite is integer-mapped inside the frame,
/// so it is band-exact like every other widget — no clip needed.
fn paint_chart(n: &Node, r: Rect, c: &mut TinySkiaCanvas) -> Result<(), RenderError> {
    let Rect { x, y, w, h } = r;
    if w == 0 || h == 0 {
        return Ok(());
    }
    let (bar, rmin, rmax, pts) = chart_params(n)?;

    // Plot background under everything.
    c.fill_rrect(
        r,
        0,
        n.color("background").unwrap_or(CHART_BG),
        n.fill_alpha(),
    );

    // Series stroke + marker radius; the interior is inset by the marker
    // radius (+1 to clear the 1 px frame) so points/strokes sit fully inside.
    let col = n
        .color("line_color")
        .or_else(|| n.color("color"))
        .unwrap_or(ACCENT);
    let lw = n.u32_attr("line_width", 2).max(1);
    let mr = (lw + 1).max(2) as i32;
    let pad = mr + 1;
    let px0 = x + pad;
    let py0 = y + pad;
    let pw = (w as i32 - 2 * pad).max(1);
    let ph = (h as i32 - 2 * pad).max(1);

    // Grid — evenly-spaced interior div-lines, crisp axis-aligned fill-rects.
    let vdiv = n.u32_attr("div_count_x", 0);
    for i in 1..=vdiv {
        let gx = px0 + (i as i32 * (pw - 1)) / (vdiv as i32 + 1);
        c.fill_rrect(
            Rect {
                x: gx,
                y: py0,
                w: 1,
                h: ph as u32,
            },
            0,
            GRID,
            0xFF,
        );
    }
    let hdiv = n.u32_attr("div_count_y", 0);
    for i in 1..=hdiv {
        let gy = py0 + (i as i32 * (ph - 1)) / (hdiv as i32 + 1);
        c.fill_rrect(
            Rect {
                x: px0,
                y: gy,
                w: pw as u32,
                h: 1,
            },
            0,
            GRID,
            0xFF,
        );
    }

    // The series.
    if !pts.is_empty() {
        let span = rmax - rmin;
        let np = pts.len() as i32;
        let map_x = |i: i32| -> i32 {
            if np == 1 {
                px0 + (pw - 1) / 2
            } else {
                px0 + (i * (pw - 1)) / (np - 1)
            }
        };
        let map_y = |v: f32| -> i32 {
            let t = ((v - rmin) / span).clamp(0.0, 1.0);
            // Round-half-up by integer truncation (t·(ph−1) ≥ 0) — no libm
            // `round`, deterministic across ISAs like the rest of the core.
            py0 + (ph - 1) - (t * (ph as f32 - 1.0) + 0.5) as i32
        };
        if bar {
            let base = py0 + ph - 1;
            let slot = (pw / np).max(1);
            let bw = (slot * 6 / 10).max(1) as u32;
            for (i, &v) in pts.iter().enumerate() {
                let cx = map_x(i as i32);
                let top = map_y(v);
                let bh = (base - top + 1).max(0) as u32;
                if bh > 0 {
                    c.fill_rrect(
                        Rect {
                            x: cx - bw as i32 / 2,
                            y: top,
                            w: bw,
                            h: bh,
                        },
                        0,
                        col,
                        0xFF,
                    );
                }
            }
        } else {
            // Polyline first, then a marker disc per point on top (clean joints).
            let mut prev: Option<(i32, i32)> = None;
            for (i, &v) in pts.iter().enumerate() {
                let p = (map_x(i as i32), map_y(v));
                if let Some((ax, ay)) = prev {
                    c.line(ax, ay, p.0, p.1, lw, col, 0xFF);
                }
                prev = Some(p);
            }
            for (i, &v) in pts.iter().enumerate() {
                c.disc(map_x(i as i32), map_y(v), mr as u32, col, 0xFF);
            }
        }
    }

    // The 1 px frame on top — four crisp axis-aligned edge-rects.
    c.fill_rrect(Rect { x, y, w, h: 1 }, 0, CHART_FRAME, 0xFF);
    c.fill_rrect(
        Rect {
            x,
            y: y + h as i32 - 1,
            w,
            h: 1,
        },
        0,
        CHART_FRAME,
        0xFF,
    );
    c.fill_rrect(Rect { x, y, w: 1, h }, 0, CHART_FRAME, 0xFF);
    c.fill_rrect(
        Rect {
            x: x + w as i32 - 1,
            y,
            w: 1,
            h,
        },
        0,
        CHART_FRAME,
        0xFF,
    );
    Ok(())
}

fn paint_border(n: &Node, r: Rect, rad: u32, c: &mut TinySkiaCanvas) {
    let bw = n.u32_attr("border_width", n.u32_attr("style_border_width", 0));
    if bw == 0 {
        return;
    }
    let col = n
        .color("border_color")
        .or_else(|| n.color("style_border_color"))
        .or_else(|| n.color("background"));
    if let Some(col) = col {
        draw_inside_border(r, rad, bw, col, c);
    }
}

/// The TGX/Qt INSIDE-border model (F8 Gate 1 fix, awto-vyvanse#321). vyr used
/// to `stroke_rrect` CENTRED on the contour, which (a) made an axis-aligned
/// box border STRADDLE the edge — a 2px border grew the outer extent to
/// 102×102 vs the canonical 100×100 — and (b) AA-washed a 1px border to a
/// blend of fill+border instead of the declared colour. Both are fixed by
/// drawing the border INSIDE so the outer edge lands ON the declared bound:
///
/// - **Axis-aligned (radius 0):** four FILLED edge-rects (radius-0 fills),
///   inset so the outer edge sits on the bound and the interior starts at
///   `x+bw` — crisp, full-colour, no AA wash. This is the box border every
///   backend draws.
/// - **Rounded (radius > 0):** the inset AA stroke — stroke the contour inset
///   by `bw/2` (radius `rad − bw/2`) so the painter's centred stroke lands its
///   OUTER edge on the declared bound, fixing the straddle for rounded boxes
///   too while keeping the arc's anti-aliasing.
fn draw_inside_border(r: Rect, rad: u32, bw: u32, col: Rgb, c: &mut TinySkiaCanvas) {
    if rad == 0 {
        // Degenerate: a border thicker than half the box is the whole box.
        if bw * 2 >= r.w || bw * 2 >= r.h {
            c.fill_rrect(r, 0, col, 0xFF);
            return;
        }
        let (x, y, w, h) = (r.x, r.y, r.w, r.h);
        let bwi = bw as i32;
        // top, bottom (full width); left, right (between the top/bottom bands).
        c.fill_rrect(Rect { x, y, w, h: bw }, 0, col, 0xFF);
        c.fill_rrect(
            Rect {
                x,
                y: y + h as i32 - bwi,
                w,
                h: bw,
            },
            0,
            col,
            0xFF,
        );
        c.fill_rrect(
            Rect {
                x,
                y: y + bwi,
                w: bw,
                h: h - 2 * bw,
            },
            0,
            col,
            0xFF,
        );
        c.fill_rrect(
            Rect {
                x: x + w as i32 - bwi,
                y: y + bwi,
                w: bw,
                h: h - 2 * bw,
            },
            0,
            col,
            0xFF,
        );
    } else {
        // Inset the stroked contour by bw/2 so its outer edge sits on the
        // bound; outer radius stays `rad` ⇒ stroked radius `rad − bw/2`.
        let half = (bw / 2) as i32;
        let inset = Rect {
            x: r.x + half,
            y: r.y + half,
            w: r.w.saturating_sub(bw),
            h: r.h.saturating_sub(bw),
        };
        let inner_rad = rad.saturating_sub(bw / 2);
        c.stroke_rrect(inset, inner_rad, bw, col, 0xFF);
    }
}

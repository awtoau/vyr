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
//! [`crate::assets`]; decode lives in the shell, invariant I7). Widgets
//! needing marks or structure beyond a single text run
//! (radio/checkbox/dropdown/table/chart) stay hard errors — the farm
//! surfaces them as honest skips, exactly like TGX's `unsupported` set.
//! Unknown `vy_` names are `UnknownWidget` errors before any pixel. A `text`
//! whose font isn't registered, a codepoint the font can't map, a `src` not
//! in the registry (`MissingAsset`), or a `vy_image` with NO `src` at all
//! (`BadIr` — an image widget with nothing to show is junk IR, not an empty
//! box) are hard errors — never a tofu box or a blank.
//!
//! ## Image model (F6, scaling policy v1)
//!
//! `vy_image` / `vy_imagebutton` blit the asset at its NATURAL size,
//! top-left anchored at the widget's x/y, CLIPPED to the widget rect — no
//! resampling. This matches LVGL's `lv_image` default (no zoom:
//! `LV_IMAGE_ALIGN_DEFAULT` paints the source 1:1 from the top-left) and
//! TGX's `Image` (setBitmap draws natural-size). The Qt backend DIVERGES: it
//! scales into the widget rect (KeepAspectRatio, smooth) — recorded on issue
//! #6; nearest-neighbour scale-to-fit is a candidate follow-up once the
//! pixel spec picks one truth. Paint order: declared `background` fill, the
//! blit, then declared border ON TOP (a border frames the image — the LVGL
//! reading). The `src` string is looked up in [`Assets`] VERBATIM — name
//! resolution/decode is the shell's job (see `crate::assets` module docs).
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
        if hexs.len() != 6 {
            return None;
        }
        let v = u32::from_str_radix(hexs, 16).ok()?;
        Some(Rgb {
            r: (v >> 16) as u8,
            g: (v >> 8) as u8,
            b: v as u8,
        })
    }

    /// Corner radius: semantic `radius` or already-lowered `style_radius`.
    pub(crate) fn radius(&self) -> u32 {
        self.u32_attr("radius", self.u32_attr("style_radius", 0))
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
const NEEDS_STRUCTURE: &[&str] = &["vy_toggle_label", "vy_dropdown", "vy_table", "vy_chart"];

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
                c.fill_rrect(r, rad, fill, 0xFF);
            }
            paint_border(n, r, h.min(w) / 2, c);
        }
        "vy_line" => {
            if let Some(fill) = n.color("background") {
                c.fill_rrect(r, 0, fill, 0xFF);
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
            // F6: the asset at NATURAL size, top-left anchored, clipped to
            // the widget rect (scaling policy — module docs). Declared fill
            // under, declared border over. No `src` = junk IR (I6): an image
            // widget with nothing to show is a BadIr, not an empty box.
            let Some(src) = n.str_attr("src") else {
                return Err(RenderError::BadIr(format!(
                    "{name} {:?} has no src attr (an image with nothing to show is junk IR)",
                    n.str_attr("name").unwrap_or_default()
                )));
            };
            let rad = n.radius();
            if let Some(fill) = n.color("background") {
                c.fill_rrect(r, rad, fill, 0xFF);
            }
            c.blit_image(r.x, r.y, assets.get(&src)?, r);
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
            c.stroke_rrect(mark, rad, (d / 10).max(2), MARK_ACCENT, 0xFF);
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
        c.fill_rrect(r, rad, fill, 0xFF);
    }
    paint_border(n, r, rad, c);
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
        c.stroke_rrect(r, rad, bw, col, 0xFF);
    }
}

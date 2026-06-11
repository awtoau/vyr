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
//! Pre-F5/F6 there are no glyphs and no image decode, so **text- and
//! image-bearing widgets are hard errors** (`Unimplemented`, named) — the
//! farm surfaces them as honest skips, exactly like TGX's `unsupported` set.
//! Unknown `vy_` names are `UnknownWidget` errors before any pixel.
//!
//! ## Chrome policy
//!
//! Plain boxes are IR-authoritative (paint nothing the IR didn't say — I5).
//! REAL widgets (slider/switch/arc…) carry widget-default chrome like every
//! backend's real widgets do (the #281 neutral-theme rule: real widgets keep
//! their defaults). The defaults below are LVGL-flavoured and are F4's
//! refinement surface against geometry_measure/colour_check:
//! track `#E6E6E6`, accent `#2196F3`, knob white + 1px `#B0B0B0` ring.
//! The SCREEN backdrop defaults to near-white `(250,250,250)` when the root
//! carries no `background` — mirroring the TGX render server's per-render
//! backdrop wipe so an empty screen reads as paper, not black.

use alloc::format;
use alloc::string::{String, ToString};
use alloc::vec::Vec;
use serde::Deserialize;

use crate::{Canvas, Rect, RenderError, RenderStats, Rgb, TinySkiaCanvas};

#[derive(Debug, Deserialize)]
pub struct Node {
    pub name: String,
    #[serde(default)]
    pub attrs: serde_json::Map<String, serde_json::Value>,
    #[serde(default)]
    pub children: Vec<Node>,
}

#[derive(Debug, Deserialize)]
pub struct Request {
    pub w: u32,
    pub h: u32,
    pub root: Node,
}

impl Request {
    pub fn parse(ir_json: &str) -> Result<Request, RenderError> {
        serde_json::from_str(ir_json).map_err(|e| RenderError::BadIr(format!("request parse: {e}")))
    }

    /// Render one band (`area`, world coords within the `w×h` screen) into
    /// the caller's RGB888 buffer — THE entry point shape (invariant I1).
    pub fn render(
        &self,
        area: Rect,
        buf: &mut [u8],
        stride: usize,
    ) -> Result<RenderStats, RenderError> {
        let mut canvas = TinySkiaCanvas::new(area)
            .ok_or_else(|| RenderError::BadIr("pixmap allocation failed".into()))?;
        // Screen backdrop: the root's background, else the near-white paper
        // default (see module docs) — painted across the whole screen.
        let backdrop = self.root.color("background").unwrap_or(Rgb {
            r: 250,
            g: 250,
            b: 250,
        });
        canvas.fill_rrect(
            Rect {
                x: 0,
                y: 0,
                w: self.w,
                h: self.h,
            },
            0,
            backdrop,
            0xFF,
        );
        for child in &self.root.children {
            walk(child, 0, 0, &mut canvas)?;
        }
        Ok(canvas.finish_into_rgb888(buf, stride))
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

impl Node {
    fn raw(&self, key: &str) -> Option<&serde_json::Value> {
        self.attrs.get(key)
    }

    fn str_attr(&self, key: &str) -> Option<String> {
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

    fn i32_attr(&self, key: &str, default: i32) -> i32 {
        self.f32_attr(key, default as f32) as i32
    }

    fn u32_attr(&self, key: &str, default: u32) -> u32 {
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
    fn radius(&self) -> u32 {
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
/// Text-bearing widgets: hard error until F5 (no glyphs to be honest with).
const NEEDS_TEXT: &[&str] = &[
    "vy_label",
    "vy_lcd",
    "vy_button",
    "vy_toggle_label",
    "vy_radio",
    "vy_checkbox",
    "vy_dropdown",
    "vy_table",
    "vy_chart",
];

fn walk(n: &Node, ox: i32, oy: i32, c: &mut TinySkiaCanvas) -> Result<(), RenderError> {
    let x = ox + n.i32_attr("x", 0);
    let y = oy + n.i32_attr("y", 0);
    let w = n.u32_attr("width", 0);
    let h = n.u32_attr("height", 0);
    let r = Rect { x, y, w, h };
    let name = n.name.as_str();

    if NEEDS_TEXT.contains(&name) {
        return Err(RenderError::Unimplemented(
            "text-bearing widget pre-F5 (no glyph path yet)",
        ));
    }

    match name {
        _ if BOXES.contains(&name) => paint_box(n, r, c),
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
            return Err(RenderError::Unimplemented(
                "image widget pre-F6 (no decode path yet)",
            ));
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

    for child in &n.children {
        walk(child, x, y, c)?;
    }
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

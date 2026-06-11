//! The F1 demo/golden scene — one of each v1 primitive.
//!
//! Shared by `tests/golden.rs` (hash + band equivalence) and `vyr-cli
//! selftest-png` so the binary demonstrably renders EXACTLY the golden
//! pixels. Geometry deliberately crosses the 30/60/90 band seams of a
//! 120×120 frame, with AA edges and an alpha blend in play.

use crate::{Canvas, Rect, Rgb};

/// Canonical demo frame size.
pub const DEMO_W: u32 = 120;
/// Canonical demo frame size.
pub const DEMO_H: u32 = 120;

/// The F3 IR fixture — one of each F3-lite widget, geometry crossing the
/// 30/60/90 band seams. Shared by `tests/ir_golden.rs` (hash + band
/// equivalence) and `vyr-bench` (the scene-level ns/px benches) so the golden
/// and the perf baseline measure the SAME pixels.
pub const DEMO_IR: &str = r##"{
  "w": 120, "h": 120,
  "root": {"name": "view", "children": [
    {"name": "vy_frame", "attrs": {"x": "6", "y": "6", "width": "52", "height": "40",
      "background": "#DCE6F5", "border_width": "2", "border_color": "#1E5AA8", "radius": "6"}},
    {"name": "vy_circle", "attrs": {"x": "66", "y": "8", "width": "36", "height": "36",
      "background": "#1E5AA8"}},
    {"name": "vy_slider", "attrs": {"x": "8", "y": "56", "width": "104", "height": "18",
      "value": "60", "min": "0", "max": "100"}},
    {"name": "vy_toggle", "attrs": {"x": "8", "y": "80", "width": "44", "height": "22",
      "value": "1"}},
    {"name": "vy_gauge", "attrs": {"x": "62", "y": "76", "width": "40", "height": "40",
      "value": "65", "min": "0", "max": "100"}},
    {"name": "vy_line", "attrs": {"x": "8", "y": "110", "width": "104", "height": "3",
      "background": "#FF8C00"}}
  ]}
}"##;

/// The F5 text fixture — label / button-with-centred-label / lcd, all three
/// runs crossing the 30/60/90 band seams of the 120×120 frame (the 17-row
/// uneven split cuts them elsewhere again). Exercises: the default font
/// (roboto 14), explicit `color`, button `color`→label ink inheritance +
/// `align="center"`, and a `style_text_font` size parse (roboto_20).
/// Shared by `tests/text_golden.rs` and `vyr-bench` (golden and baseline
/// measure the SAME pixels). Needs `fonts/roboto.ttf` registered as
/// "roboto" by the caller.
pub const TEXT_IR: &str = r##"{
  "w": 120, "h": 120,
  "root": {"name": "view", "children": [
    {"name": "vy_label", "attrs": {"x": "8", "y": "18", "width": "104", "height": "18",
      "text": "Vyr text 14px", "color": "#1E5AA8"}},
    {"name": "vy_button", "attrs": {"x": "10", "y": "46", "width": "100", "height": "28",
      "background": "#1E5AA8", "radius": "6", "color": "#FFFFFF"},
      "children": [{"name": "vy_label", "attrs": {"text": "Button", "align": "center"}}]},
    {"name": "vy_lcd", "attrs": {"x": "8", "y": "82", "width": "104", "height": "24",
      "text": "12:34", "color": "#222222", "style_text_font": "roboto_20"}}
  ]}
}"##;

/// The F6 image fixture — the committed 24×24 RGBA checker asset blitted
/// four ways: natural size inside a BIGGER widget rect (no scaling), CLIPPED
/// by a smaller widget rect, over a frame fill (source-over onto a widget,
/// not just the backdrop), and through `vy_imagebutton` (same arm). The
/// asset's semi-transparent quadrant + transparent centre hole land on the
/// coloured backdrop/frame so alpha blending is visible in the golden.
/// Every blit crosses 30-row seams (30/60/90) and 17-row seams of the
/// 120×120 frame. Shared by `tests/image_golden.rs` and `vyr-bench`; needs
/// the committed `vyr-core/tests/assets/checker-24.png` registered under
/// [`IMAGE_ASSET`] (the verbatim-src name contract — `assets` module docs).
pub const IMAGE_IR: &str = r##"{
  "w": 120, "h": 120,
  "root": {"name": "view", "attrs": {"background": "#3A6EA5"}, "children": [
    {"name": "vy_frame", "attrs": {"x": "40", "y": "70", "width": "64", "height": "44",
      "background": "#E0D8C8"}},
    {"name": "vy_image", "attrs": {"x": "12", "y": "14", "width": "40", "height": "40",
      "src": "checker-24.png"}},
    {"name": "vy_image", "attrs": {"x": "70", "y": "50", "width": "16", "height": "12",
      "src": "checker-24.png"}},
    {"name": "vy_image", "attrs": {"x": "56", "y": "80", "width": "24", "height": "24",
      "src": "checker-24.png"}},
    {"name": "vy_imagebutton", "attrs": {"x": "12", "y": "76", "width": "30", "height": "24",
      "src": "checker-24.png"}}
  ]}
}"##;

/// The registry name [`IMAGE_IR`]'s `src` attrs reference.
pub const IMAGE_ASSET: &str = "checker-24.png";

/// Draw the demo scene. The first op paints the backdrop — the render tree's
/// screen-background discipline, mirrored here.
pub fn demo_scene(c: &mut dyn Canvas) {
    c.fill_rrect(
        Rect {
            x: 0,
            y: 0,
            w: DEMO_W,
            h: DEMO_H,
        },
        0,
        Rgb {
            r: 250,
            g: 250,
            b: 250,
        },
        0xFF,
    );
    c.fill_rrect(
        Rect {
            x: 8,
            y: 14,
            w: 60,
            h: 32,
        },
        8,
        Rgb {
            r: 0x1E,
            g: 0x5A,
            b: 0xA8,
        },
        0xFF,
    );
    c.stroke_rrect(
        Rect {
            x: 14,
            y: 50,
            w: 90,
            h: 24,
        },
        6,
        2,
        Rgb {
            r: 0xA8,
            g: 0x32,
            b: 0x1E,
        },
        0xFF,
    );
    c.disc(
        60,
        90,
        18,
        Rgb {
            r: 0x2E,
            g: 0x8B,
            b: 0x57,
        },
        0xC0,
    );
    c.ring(
        88,
        36,
        14,
        4,
        Rgb {
            r: 0xFF,
            g: 0x8C,
            b: 0x00,
        },
        0xFF,
    );
    c.line(
        4,
        4,
        116,
        116,
        3,
        Rgb {
            r: 0x44,
            g: 0x44,
            b: 0x44,
        },
        0xFF,
    );
    c.fill_linear_gradient(
        Rect {
            x: 6,
            y: 100,
            w: 108,
            h: 14,
        },
        4,
        Rgb {
            r: 0x10,
            g: 0x10,
            b: 0x80,
        },
        Rgb {
            r: 0xE0,
            g: 0x20,
            b: 0x20,
        },
        false,
        0xFF,
    );
}

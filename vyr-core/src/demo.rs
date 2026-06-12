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

/// The F4 chart fixture — a line chart (grid + polyline + per-point markers)
/// over a bar chart, both single-series and painted from IR-declared `points`
/// (NOT invented demo data), geometry crossing the 30/60/90 band seams of the
/// 120×120 frame so the composite is proven band-exact at Exact and Draft.
/// Shared by `tests/chart_golden.rs`. The series is the deterministic data the
/// oracle scores backends against — see vyr-coordination's proposed vy_chart
/// attribute surface (`points`/`chart_type`/`range_*`/`div_count_*`).
pub const CHART_IR: &str = r##"{
  "w": 120, "h": 120,
  "root": {"name": "view", "children": [
    {"name": "vy_frame", "attrs": {"x": "0", "y": "0", "width": "120", "height": "120",
      "background": "#F0F0F0"}},
    {"name": "vy_chart", "attrs": {"x": "8", "y": "8", "width": "104", "height": "48",
      "points": "10,60,30,85,45,90,55", "range_min": "0", "range_max": "100",
      "div_count_x": "6", "div_count_y": "4", "line_color": "#1E5AA8", "line_width": "2"}},
    {"name": "vy_chart", "attrs": {"x": "8", "y": "64", "width": "104", "height": "48",
      "chart_type": "bar", "points": "25,55,40,75,50,88", "range_min": "0", "range_max": "100",
      "div_count_y": "4", "line_color": "#1E5AA8"}}
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

/// The F3 clip fixture — children deliberately OVERFLOWING their containers,
/// so every op class meets the clip stack:
///
/// - container 1 (radius 18): a nested rounded frame whose `vy_line` child
///   overflows it on both sides (clip-stack depth 2 — mask intersection); a
///   disc overflowing through the bottom-right corner ARC (the rounded-trim
///   money shot); a label whose run crosses the right edge into the corner
///   zone (masked glyph blits); an image poking through the top-right arc
///   (masked image blits).
/// - container 2 (radius 0): frame fill, image and label all overflowing a
///   PURE-RECT clip — the exact integer span fast path.
///
/// Shared by `tests/clip_golden.rs` (hash + band equivalence) and
/// `vyr-bench` (`scene/clip_full` — golden and baseline measure the SAME
/// pixels). Needs `fonts/roboto.ttf` as "roboto" and the committed checker
/// under [`IMAGE_ASSET`].
pub const CLIP_IR: &str = r##"{
  "schema_version": "0.6-vyvanse",
  "w": 120, "h": 120,
  "root": {"name": "view", "attrs": {"background": "#F0EEE8"}, "children": [
    {"name": "vy_container", "attrs": {"x": "10", "y": "8", "width": "72", "height": "72",
      "radius": "18", "background": "#DCE6F5", "border_width": "2", "border_color": "#1E5AA8"},
      "children": [
        {"name": "vy_frame", "attrs": {"x": "4", "y": "4", "width": "34", "height": "26",
          "radius": "8", "background": "#FFFFFF", "border_width": "1", "border_color": "#1E5AA8"},
          "children": [
            {"name": "vy_line", "attrs": {"x": "-8", "y": "18", "width": "56", "height": "4",
              "background": "#FF8C00"}}
          ]},
        {"name": "vy_circle", "attrs": {"x": "36", "y": "34", "width": "52", "height": "52",
          "background": "#E0501E"}},
        {"name": "vy_label", "attrs": {"x": "4", "y": "44", "width": "60", "height": "16",
          "text": "clip me please", "color": "#222222"}},
        {"name": "vy_image", "attrs": {"x": "56", "y": "-6", "width": "24", "height": "24",
          "src": "checker-24.png"}}
      ]},
    {"name": "vy_container", "attrs": {"x": "10", "y": "88", "width": "64", "height": "24",
      "background": "#FFFFFF", "border_width": "1", "border_color": "#888888"},
      "children": [
        {"name": "vy_frame", "attrs": {"x": "40", "y": "6", "width": "44", "height": "30",
          "radius": "4", "background": "#2E8B57"}},
        {"name": "vy_image", "attrs": {"x": "52", "y": "4", "width": "24", "height": "24",
          "src": "checker-24.png"}},
        {"name": "vy_label", "attrs": {"x": "4", "y": "2", "width": "56", "height": "16",
          "text": "rect clipped", "color": "#1E5AA8"}}
      ]}
  ]}
}"##;

/// [`CLIP_IR`]'s widgets FLATTENED to root children at the same absolute
/// positions — identical op mix, zero clip pushes (the root never pushes;
/// children overflow freely). The bench pair `scene/clip_full` vs
/// `scene/clip_flat` prices the clip stack: mask builds + masked draws +
/// world-space fate checks are the entire delta.
pub const CLIP_FLAT_IR: &str = r##"{
  "schema_version": "0.6-vyvanse",
  "w": 120, "h": 120,
  "root": {"name": "view", "attrs": {"background": "#F0EEE8"}, "children": [
    {"name": "vy_container", "attrs": {"x": "10", "y": "8", "width": "72", "height": "72",
      "radius": "18", "background": "#DCE6F5", "border_width": "2", "border_color": "#1E5AA8"}},
    {"name": "vy_frame", "attrs": {"x": "14", "y": "12", "width": "34", "height": "26",
      "radius": "8", "background": "#FFFFFF", "border_width": "1", "border_color": "#1E5AA8"}},
    {"name": "vy_line", "attrs": {"x": "6", "y": "30", "width": "56", "height": "4",
      "background": "#FF8C00"}},
    {"name": "vy_circle", "attrs": {"x": "46", "y": "42", "width": "52", "height": "52",
      "background": "#E0501E"}},
    {"name": "vy_label", "attrs": {"x": "14", "y": "52", "width": "60", "height": "16",
      "text": "clip me please", "color": "#222222"}},
    {"name": "vy_image", "attrs": {"x": "66", "y": "2", "width": "24", "height": "24",
      "src": "checker-24.png"}},
    {"name": "vy_container", "attrs": {"x": "10", "y": "88", "width": "64", "height": "24",
      "background": "#FFFFFF", "border_width": "1", "border_color": "#888888"}},
    {"name": "vy_frame", "attrs": {"x": "50", "y": "94", "width": "44", "height": "30",
      "radius": "4", "background": "#2E8B57"}},
    {"name": "vy_image", "attrs": {"x": "62", "y": "92", "width": "24", "height": "24",
      "src": "checker-24.png"}},
    {"name": "vy_label", "attrs": {"x": "14", "y": "90", "width": "56", "height": "16",
      "text": "rect clipped", "color": "#1E5AA8"}}
  ]}
}"##;

/// The two panel states differ ONLY in the toggle's `value` — one macro,
/// one substitution, so the pair cannot drift apart.
macro_rules! panel_ir {
    ($toggle:literal) => {
        concat!(
            r##"{
  "schema_version": "0.6-vyvanse",
  "w": 480, "h": 320,
  "root": {"name": "view", "attrs": {"background": "#22262B"}, "children": [
    {"name": "vy_frame", "attrs": {"x": "12", "y": "12", "width": "456", "height": "48",
      "background": "#2E3440", "radius": "8", "border_width": "1", "border_color": "#4C566A"}},
    {"name": "vy_label", "attrs": {"x": "28", "y": "26", "width": "220", "height": "20",
      "text": "Compressor 2 - line B", "color": "#ECEFF4"}},
    {"name": "vy_gauge", "attrs": {"x": "24", "y": "84", "width": "120", "height": "120",
      "value": "65", "color": "#88C0D0"}},
    {"name": "vy_lcd", "attrs": {"x": "48", "y": "216", "width": "90", "height": "24",
      "text": "1480", "color": "#A3BE8C", "style_text_font": "roboto_20"}},
    {"name": "vy_slider", "attrs": {"x": "180", "y": "100", "width": "260", "height": "18",
      "value": "62"}},
    {"name": "vy_slider", "attrs": {"x": "180", "y": "140", "width": "260", "height": "18",
      "value": "35"}},
    {"name": "vy_progress", "attrs": {"x": "180", "y": "180", "width": "260", "height": "12",
      "value": "80"}},
    {"name": "vy_toggle", "attrs": {"x": "180", "y": "220", "width": "56", "height": "28",
      "value": ""##,
            $toggle,
            r##""}},
    {"name": "vy_label", "attrs": {"x": "248", "y": "226", "width": "120", "height": "18",
      "text": "bypass", "color": "#D8DEE9"}},
    {"name": "vy_line", "attrs": {"x": "12", "y": "282", "width": "456", "height": "2",
      "background": "#4C566A"}},
    {"name": "vy_label", "attrs": {"x": "16", "y": "294", "width": "200", "height": "16",
      "text": "awto / vyr runtime", "color": "#7A869A"}}
  ]}
}"##
        )
    };
}

/// The F3 dirty-rect bench pair, PREV half — a representative 480×320
/// instrument panel (the embedded-panel resolution class the runtime story
/// is about): header frame + title, gauge, lcd readout, two sliders, a
/// progress bar, a toggle with caption, footer rule + credit. Needs
/// `fonts/roboto.ttf` as "roboto"; image-free.
///
/// [`PANEL_NEXT_IR`] differs in exactly ONE attr (the bypass toggle flips
/// on) — the retained-mode frame step. `vyr-bench` renders PREV once, then
/// measures `render_incremental(NEXT)` against a full NEXT render:
/// `scene/panel_dirty_incr` vs `scene/panel_full` is the headline
/// incremental win. `tests/dirty_rects.rs` proves the same pair
/// byte-identical to the full render.
pub const PANEL_PREV_IR: &str = panel_ir!("0");

/// [`PANEL_PREV_IR`] with the bypass toggle ON — see there.
pub const PANEL_NEXT_IR: &str = panel_ir!("1");

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

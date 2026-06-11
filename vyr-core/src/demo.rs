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

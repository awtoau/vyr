//! The text-measurement API (F5 follow-up): `Fonts::measure` returns BOTH
//! boxes — layout (advance × ascent..descent) and ink (where paint lands) —
//! and `Canvas::glyph_run` reports the rendered run's world-space ink bbox.

use vyr_core::{Canvas, Fonts, Rect, TinySkiaCanvas};

fn roboto() -> Fonts {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../fonts/roboto.ttf");
    let bytes = std::fs::read(&path).expect("vendored fonts/roboto.ttf");
    let mut fonts = Fonts::new();
    fonts.register("roboto", bytes).expect("roboto registers");
    fonts
}

#[test]
fn measure_returns_layout_and_ink() {
    let mut fonts = roboto();
    let m = fonts.measure("roboto", 14, "Vyr").expect("measures");
    assert!(m.advance > 0, "advance: {m:?}");
    assert!(m.ascent > 0 && m.descent < 0, "metrics: {m:?}");
    assert_eq!(m.height(), m.ascent - m.descent);
    let ink = m.ink.expect("inked run has an ink box");
    // Ink sits on/above the baseline for "Vyr" except the y descender below.
    assert!(ink.y < 0, "cap height above baseline: {ink:?}");
    assert!(ink.w > 0 && ink.h > 0);
    // Ink can never be wider than layout by more than glyph overhang; sanity:
    // it must overlap the layout span at all.
    assert!(ink.x < m.advance, "ink inside the advance span: {ink:?}");
}

#[test]
fn trailing_space_grows_advance_not_ink() {
    let mut fonts = roboto();
    let a = fonts.measure("roboto", 14, "Vyr").expect("measures");
    let b = fonts.measure("roboto", 14, "Vyr ").expect("measures");
    assert!(b.advance > a.advance, "trailing space advances the pen");
    assert_eq!(a.ink, b.ink, "trailing space adds no ink");
}

#[test]
fn empty_text_has_no_ink() {
    let mut fonts = roboto();
    let m = fonts.measure("roboto", 14, "").expect("measures");
    assert_eq!(m.advance, 0);
    assert_eq!(m.ink, None);
}

#[test]
fn glyph_run_reports_world_ink_bbox() {
    let mut fonts = roboto();
    let (ox, by) = (30, 50); // pen origin / baseline in world coords
    let m = fonts.measure("roboto", 14, "Vyr").expect("measures");
    fonts.prepare_run("roboto", 14, "Vyr").expect("prepared");
    let placed = fonts
        .placed_run("roboto", 14, "Vyr", ox, by)
        .expect("placed");
    let mut canvas = TinySkiaCanvas::new(Rect {
        x: 0,
        y: 0,
        w: 120,
        h: 120,
    })
    .expect("pixmap");
    let bb = canvas
        .glyph_run(&placed, vyr_core::Rgb { r: 0, g: 0, b: 0 }, 0xFF)
        .expect("inked run reports a bbox");
    // The painter's world bbox == measure's origin-relative ink, translated.
    let ink = m.ink.expect("ink");
    assert_eq!(
        (bb.x, bb.y, bb.w, bb.h),
        (ink.x + ox, ink.y + by, ink.w, ink.h),
        "glyph_run bbox must equal measure().ink translated to the pen"
    );
}

#[test]
fn glyph_run_bbox_is_band_invariant() {
    // The bbox is geometry, not clipping: a band that sees only half the run
    // still reports the SAME world bbox.
    let mut fonts = roboto();
    fonts.prepare_run("roboto", 14, "Vyr").expect("prepared");
    let placed = fonts
        .placed_run("roboto", 14, "Vyr", 30, 50)
        .expect("placed");
    let mut full = TinySkiaCanvas::new(Rect {
        x: 0,
        y: 0,
        w: 120,
        h: 120,
    })
    .expect("pixmap");
    let mut band = TinySkiaCanvas::new(Rect {
        x: 0,
        y: 45,
        w: 120,
        h: 5,
    })
    .expect("pixmap");
    let ink = vyr_core::Rgb { r: 0, g: 0, b: 0 };
    let bb_full = full.glyph_run(&placed, ink, 0xFF);
    let bb_band = band.glyph_run(&placed, ink, 0xFF);
    assert_eq!(bb_full, bb_band, "ink bbox must not depend on the band");
}

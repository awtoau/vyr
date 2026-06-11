//! F3 dirty-rect acceptance — THE retained-mode proof: render request A in
//! full; mutate to request B; compute `dirty_rects`; re-render ONLY the
//! dirty regions (as bands, through the one entry point) onto a copy of A's
//! frame — the result must be BYTE-IDENTICAL to a full render of B.
//!
//! Covers: a move (WAS + NOW regions, provably separate), an attr restyle,
//! a removal, an addition, a clipped-subtree change (dirty stays inside the
//! container), a text move (the run-bound margins hold), a root-background
//! change (whole screen), and the no-change case (empty list, untouched
//! buffer). Also the first ARBITRARY-rect band-equivalence exercise: dirty
//! rects have x-offsets and vertical seams, unlike the full-width test
//! bands.

use vyr_core::ir::Request;
use vyr_core::{Assets, Fonts, Rect, dirty_rects};

const W: u32 = 120;
const H: u32 = 120;

/// Base scene A: frame, slider, toggle, rule — geometry crossing the band
/// seams, text-free (the text case carries its own fonts).
const SCENE_A: &str = r##"{
  "w": 120, "h": 120,
  "root": {"name": "view", "attrs": {"background": "#F0F0F0"}, "children": [
    {"name": "vy_frame", "attrs": {"x": "8", "y": "8", "width": "50", "height": "30",
      "background": "#DCE6F5", "border_width": "2", "border_color": "#1E5AA8", "radius": "4"}},
    {"name": "vy_slider", "attrs": {"x": "8", "y": "48", "width": "104", "height": "16",
      "value": "30"}},
    {"name": "vy_toggle", "attrs": {"x": "8", "y": "72", "width": "40", "height": "20",
      "value": "0"}},
    {"name": "vy_line", "attrs": {"x": "8", "y": "104", "width": "104", "height": "3",
      "background": "#FF8C00"}}
  ]}
}"##;

fn full_render(req: &Request, fonts: &mut Fonts) -> Vec<u8> {
    let mut buf = vec![0u8; (req.w * req.h * 3) as usize];
    let stats = req
        .render_with(
            fonts,
            &Assets::new(),
            Rect {
                x: 0,
                y: 0,
                w: req.w,
                h: req.h,
            },
            &mut buf,
            (req.w * 3) as usize,
        )
        .expect("scene renders");
    assert!(stats.pixels_written > 0);
    buf
}

/// The acceptance harness: incremental(B over A's frame) == full(B), to the
/// byte. Returns the stats for extra assertions.
fn prove_incremental(a: &str, b: &str, fonts: &mut Fonts) -> vyr_core::RenderStats {
    let req_a = Request::parse(a).expect("A parses");
    let req_b = Request::parse(b).expect("B parses");
    let frame_a = full_render(&req_a, fonts);
    let frame_b = full_render(&req_b, fonts);
    let mut incremental = frame_a.clone();
    let stats = req_b
        .render_incremental(
            &req_a,
            fonts,
            &Assets::new(),
            &mut incremental,
            (req_b.w * 3) as usize,
        )
        .expect("incremental renders");
    assert_eq!(
        incremental, frame_b,
        "incremental repaint must be byte-identical to the full render of B"
    );
    stats
}

fn covers(rects: &[Rect], r: Rect) -> bool {
    // Sufficient for these tests: some single dirty rect contains r.
    rects.iter().any(|d| {
        d.x <= r.x
            && d.y <= r.y
            && d.x + d.w as i32 >= r.x + r.w as i32
            && d.y + d.h as i32 >= r.y + r.h as i32
    })
}

fn contains_point(rects: &[Rect], x: i32, y: i32) -> bool {
    rects
        .iter()
        .any(|d| x >= d.x && x < d.x + d.w as i32 && y >= d.y && y < d.y + d.h as i32)
}

#[test]
fn no_change_is_empty_and_untouched() {
    let a = Request::parse(SCENE_A).expect("parses");
    let b = Request::parse(SCENE_A).expect("parses");
    assert!(dirty_rects(&a, &b).is_empty(), "identical trees = no dirt");
    let mut fonts = Fonts::new();
    let frame = full_render(&a, &mut fonts);
    let mut buf = frame.clone();
    let stats = b
        .render_incremental(&a, &mut fonts, &Assets::new(), &mut buf, (W * 3) as usize)
        .expect("no-op incremental");
    assert_eq!(stats.dirty_area_px, 0);
    assert_eq!(stats.bands_rendered, 0);
    assert_eq!(buf, frame, "no-change incremental must not touch a byte");
}

#[test]
fn move_dirties_was_and_now() {
    // Toggle moves x 8 -> 64: WAS around (8,72,40,20), NOW around
    // (64,72,40,20) — two separate regions with a clean gap between.
    let b_ir = SCENE_A.replace(
        r#"{"name": "vy_toggle", "attrs": {"x": "8", "y": "72""#,
        r#"{"name": "vy_toggle", "attrs": {"x": "64", "y": "72""#,
    );
    let a = Request::parse(SCENE_A).expect("parses");
    let b = Request::parse(&b_ir).expect("parses");
    let rects = dirty_rects(&a, &b);
    assert!(
        covers(
            &rects,
            Rect {
                x: 8,
                y: 72,
                w: 40,
                h: 20
            }
        ),
        "WAS region covered: {rects:?}"
    );
    assert!(
        covers(
            &rects,
            Rect {
                x: 64,
                y: 72,
                w: 40,
                h: 20
            }
        ),
        "NOW region covered: {rects:?}"
    );
    // The gap between old and new (x ~56) stays clean — WAS+NOW, not a
    // whole-row smear.
    assert!(
        !contains_point(&rects, 56, 82),
        "gap between WAS and NOW must stay clean: {rects:?}"
    );
    // And nothing above the toggle row got dirtied.
    assert!(!contains_point(&rects, 60, 20), "unrelated area dirtied");

    let mut fonts = Fonts::new();
    let stats = prove_incremental(SCENE_A, &b_ir, &mut fonts);
    let total: u64 = rects.iter().map(|r| r.w as u64 * r.h as u64).sum();
    assert_eq!(stats.dirty_area_px, total, "stats wire the merged area");
    assert!(
        stats.dirty_area_px < (W * H) as u64 / 2,
        "a small move must not repaint half the screen"
    );
}

#[test]
fn attr_change_dirties_node() {
    let b_ir = SCENE_A.replace("#DCE6F5", "#FFE0E0");
    let a = Request::parse(SCENE_A).expect("parses");
    let b = Request::parse(&b_ir).expect("parses");
    let rects = dirty_rects(&a, &b);
    assert!(
        covers(
            &rects,
            Rect {
                x: 8,
                y: 8,
                w: 50,
                h: 30
            }
        ),
        "restyled frame covered: {rects:?}"
    );
    assert!(
        !contains_point(&rects, 60, 110),
        "rule far below must stay clean: {rects:?}"
    );
    let mut fonts = Fonts::new();
    prove_incremental(SCENE_A, &b_ir, &mut fonts);
}

#[test]
fn removal_dirties_old_bbox() {
    let b_ir = SCENE_A.replace(
        r##",
    {"name": "vy_line", "attrs": {"x": "8", "y": "104", "width": "104", "height": "3",
      "background": "#FF8C00"}}"##,
        "",
    );
    let a = Request::parse(SCENE_A).expect("parses");
    let b = Request::parse(&b_ir).expect("parses");
    assert_eq!(
        b.root.children.len(),
        3,
        "removal fixture really dropped the rule"
    );
    let rects = dirty_rects(&a, &b);
    assert!(
        covers(
            &rects,
            Rect {
                x: 8,
                y: 104,
                w: 104,
                h: 3
            }
        ),
        "removed rule's WAS bbox covered: {rects:?}"
    );
    let mut fonts = Fonts::new();
    prove_incremental(SCENE_A, &b_ir, &mut fonts);
}

#[test]
fn addition_dirties_new_bbox() {
    let b_ir = SCENE_A.replace(
        r#"{"name": "vy_line""#,
        r##"{"name": "vy_circle", "attrs": {"x": "70", "y": "70", "width": "30", "height": "30",
      "background": "#2E8B57"}},
    {"name": "vy_line""##,
    );
    let a = Request::parse(SCENE_A).expect("parses");
    let b = Request::parse(&b_ir).expect("parses");
    let rects = dirty_rects(&a, &b);
    assert!(
        covers(
            &rects,
            Rect {
                x: 70,
                y: 70,
                w: 30,
                h: 30
            }
        ),
        "added disc's NOW bbox covered: {rects:?}"
    );
    let mut fonts = Fonts::new();
    prove_incremental(SCENE_A, &b_ir, &mut fonts);
}

#[test]
fn root_background_change_dirties_whole_screen() {
    let b_ir = SCENE_A.replace("#F0F0F0", "#101010");
    let a = Request::parse(SCENE_A).expect("parses");
    let b = Request::parse(&b_ir).expect("parses");
    assert_eq!(
        dirty_rects(&a, &b),
        vec![Rect {
            x: 0,
            y: 0,
            w: W,
            h: H
        }],
        "backdrop change = full repaint"
    );
    let mut fonts = Fonts::new();
    prove_incremental(SCENE_A, &b_ir, &mut fonts);
}

/// A change INSIDE a clipping container dirties at most the container's
/// rect (the clip-context tightening) — even though the child overflows it.
#[test]
fn clipped_subtree_change_stays_inside_container() {
    let a_ir = r##"{
      "w": 120, "h": 120,
      "root": {"name": "view", "attrs": {"background": "#EEEEEE"}, "children": [
        {"name": "vy_container", "attrs": {"x": "30", "y": "30", "width": "60", "height": "40",
          "radius": "10", "background": "#FFFFFF"}, "children": [
            {"name": "vy_circle", "attrs": {"x": "30", "y": "10", "width": "50", "height": "50",
              "background": "#E0501E"}}
          ]}
      ]}
    }"##;
    let b_ir = a_ir.replace("#E0501E", "#1E5AA8");
    let a = Request::parse(a_ir).expect("parses");
    let b = Request::parse(&b_ir).expect("parses");
    let rects = dirty_rects(&a, &b);
    assert!(!rects.is_empty());
    let container = Rect {
        x: 30,
        y: 30,
        w: 60,
        h: 40,
    };
    for r in &rects {
        assert_eq!(
            r.intersect(container),
            *r,
            "dirty {r:?} must stay inside the clipping container"
        );
    }
    let mut fonts = Fonts::new();
    prove_incremental(a_ir, &b_ir, &mut fonts);
}

/// Text move: the conservative run bounds must cover real ink (glyphs can
/// ink outside the declared label rect).
#[test]
fn text_move_repaints_exactly() {
    let a_ir = r##"{
      "w": 120, "h": 120,
      "root": {"name": "view", "attrs": {"background": "#FAFAF5"}, "children": [
        {"name": "vy_label", "attrs": {"x": "10", "y": "20", "width": "60", "height": "16",
          "text": "moving text", "color": "#1E5AA8"}},
        {"name": "vy_frame", "attrs": {"x": "10", "y": "90", "width": "100", "height": "20",
          "background": "#DCE6F5"}}
      ]}
    }"##;
    let b_ir = a_ir.replace(r#""x": "10", "y": "20""#, r#""x": "24", "y": "52""#);
    let mut fonts = Fonts::new();
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../fonts/roboto.ttf");
    fonts
        .register("roboto", std::fs::read(&path).expect("roboto.ttf"))
        .expect("registers");
    prove_incremental(a_ir, &b_ir, &mut fonts);
}

/// The bench pair (480×320 panel, one toggle flips) proves byte-exact too —
/// the headline number's correctness certificate.
#[test]
fn panel_pair_incremental_is_exact() {
    let mut fonts = Fonts::new();
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../fonts/roboto.ttf");
    fonts
        .register("roboto", std::fs::read(&path).expect("roboto.ttf"))
        .expect("registers");
    let stats = prove_incremental(
        vyr_core::demo::PANEL_PREV_IR,
        vyr_core::demo::PANEL_NEXT_IR,
        &mut fonts,
    );
    // The whole point: a toggle flip repaints a sliver of the panel.
    let full = 480u64 * 320;
    assert!(
        stats.dirty_area_px > 0 && stats.dirty_area_px < full / 20,
        "one toggle flip should dirty <5% of the panel, got {} of {full}",
        stats.dirty_area_px
    );
}

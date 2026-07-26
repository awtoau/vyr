//! #55 acceptance — the persistent typed tree renders BYTE-FOR-BYTE like a
//! fresh re-parse of the same frame.
//!
//! `#34` resolved a scene once into a typed `Node` tree so the render path
//! parses no strings per band. `#55` extends that to animation: instead of
//! `scene_ir` (stringify) → `Request::parse` (deserialise + whole-tree
//! `prepare`) every frame, hold ONE typed `Request` and mutate only the
//! animating fields per frame via [`vyr_scene::animate`], writing the typed
//! `Geom`/`Resolved` caches directly.
//!
//! This test is the guard that the mutation path cannot drift from the parse
//! path. For a frame sweep chosen to cross every driver's period boundary
//! (block/theme at 50, panel slot at 25, toggle at 30, gauge triangle at 24,
//! progress at 50, slider at 100), BOTH detail levels and ALL THREE quality
//! tiers:
//!
//! - **A** = `Request::parse(scene_ir(f))` then full-frame `render_with_quality`
//!   — the oracle.
//! - **B** = a persistent `scene_tree(...)` advanced with `animate(_, f)` then
//!   the same full-frame render.
//!
//! `A == B` byte-for-byte. The persistent tree is reused across ASCENDING
//! frames, so a match also proves `animate` is a pure function of the frame
//! index (no residue from the previous frame) — the invariant the whole
//! persistent-tree optimisation rests on. A failure here means a resolved field
//! is stale or the shared resolve math diverged; fix the setter, never weaken
//! this test.

use vyr_core::ir::Request;
use vyr_core::{Assets, Fonts, Quality, Rect};
use vyr_rig::{rig_assets, rig_fonts};
use vyr_scene::{Detail, animate, scene_ir, scene_tree};

const W: u32 = 480;
const H: u32 = 270;

/// Frames crossing every driver's period boundary within one `Full` period
/// (1200), plus the endpoints — where a stale cache or a diverged resolve
/// would first show.
const FRAMES: &[u32] = &[
    0, 1, 2, 3, 7, 13, 24, 25, 29, 30, 49, 50, 51, 99, 100, 299, 300, 599, 1199,
];

const TIERS: &[(Quality, &str)] = &[
    (Quality::Exact, "Exact"),
    (Quality::Fast, "Fast"),
    (Quality::Draft, "Draft"),
];

/// Full-frame render of `req` at `quality` into a fresh RGB888 buffer.
fn render_full(req: &Request, fonts: &mut Fonts, assets: &Assets, quality: Quality) -> Vec<u8> {
    let stride = (W * 3) as usize;
    let mut buf = vec![0u8; stride * H as usize];
    let area = Rect {
        x: 0,
        y: 0,
        w: W,
        h: H,
    };
    req.render_with_quality(fonts, assets, area, &mut buf, stride, quality)
        .expect("scene renders");
    buf
}

#[test]
fn persistent_tree_matches_reparse_byte_for_byte() {
    let mut checked = 0usize;
    for &detail in &[Detail::Full, Detail::Lite] {
        for &(quality, qname) in TIERS {
            // ONE persistent tree, reused across the ascending frame sweep — so
            // a per-frame match is also proof there is no residue between
            // frames (animate is pure in the frame index).
            let (mut req_b, handles) = scene_tree(W, H, detail);
            let mut fonts_b = rig_fonts();
            let assets_b = rig_assets();
            for &f in FRAMES {
                // A: the oracle — a fresh parse of this frame's IR string.
                let req_a =
                    Request::parse(&scene_ir(W, H, f, detail)).expect("scene_ir(frame) parses");
                let mut fonts_a = rig_fonts();
                let assets_a = rig_assets();
                let a = render_full(&req_a, &mut fonts_a, &assets_a, quality);

                // B: the persistent tree mutated in place to this frame.
                animate(&mut req_b, &handles, W, H, f);
                let b = render_full(&req_b, &mut fonts_b, &assets_b, quality);

                assert_eq!(a.len(), b.len(), "buffer length mismatch");
                assert!(
                    a == b,
                    "{} {qname} frame {f}: persistent tree diverged from the re-parse \
                     (a resolved field is stale or the resolve math diverged)",
                    detail.scene_id()
                );
                checked += 1;
            }
        }
    }
    println!(
        "persistent-anim byte-identity: PASS — {checked} renders \
         ({} frames x 2 details x 3 tiers) each byte-identical to a fresh re-parse",
        FRAMES.len()
    );
}

/// Focused no-residue proof: frame `f` on a tree that has been walked through
/// every prior sweep frame must equal frame `f` on a tree JUST parsed and
/// advanced straight to `f`. If any setter left a field from an earlier frame
/// behind, these two would differ.
#[test]
fn animate_carries_no_residue_between_frames() {
    let detail = Detail::Full;
    let quality = Quality::Exact;
    let mut fonts = rig_fonts();
    let assets = rig_assets();

    // A long-lived tree walked through the whole ascending sweep.
    let (mut req_used, handles) = scene_tree(W, H, detail);
    for &f in FRAMES {
        animate(&mut req_used, &handles, W, H, f);
        // A pristine tree taken straight to the same frame.
        let (mut req_fresh, h2) = scene_tree(W, H, detail);
        animate(&mut req_fresh, &h2, W, H, f);

        let used = render_full(&req_used, &mut fonts, &assets, quality);
        let fresh = render_full(&req_fresh, &mut fonts, &assets, quality);
        assert!(
            used == fresh,
            "frame {f}: a reused tree differs from a freshly-advanced one — animate left residue"
        );
    }
}

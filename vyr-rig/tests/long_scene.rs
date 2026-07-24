//! The long animated scene's golden: the committed hash chains, enforced by
//! `./dev.py test`.
//!
//! The chains (`vyr-rig/hashchain-long.json`, `-long-lite.json`) are blessed
//! at the FULL preset — 1200 frames, one complete animation period. Running
//! all 1200 in the unit-test gate would cost ~1.5 s per scene, which is
//! affordable, but the point of the tiered presets is that the cheap tier is
//! the one you can pay after every change: so the gate checks the SMOKE
//! prefix (60 frames) against the committed chain's first 60 entries, and
//! the deep run stays `python3 scripts/rig-long.py --steps rig`.
//!
//! A prefix check is sound because the scene is a pure function of the frame
//! index (`vyr_scene`'s own tests pin that): frame *i* of a 60-frame run and
//! frame *i* of a 1200-frame run are the same frame by construction, so a
//! prefix mismatch is a real pixel regression and never a run-shape artifact.

use std::path::Path;

use vyr_rig::{Detail, HashChain, Preset, Scene, run_anim_scene};

const W: u32 = 480;
const H: u32 = 270;

fn golden(scene: Scene) -> HashChain {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("workspace root")
        .join(scene.chain_file());
    let json = std::fs::read_to_string(&path).unwrap_or_else(|e| {
        panic!(
            "read {}: {e} — bless it with `vyr-rig anim --bless`",
            path.display()
        )
    });
    HashChain::parse(&json).expect("committed chain parses")
}

fn check_prefix(scene: Scene) {
    let g = golden(scene);
    assert_eq!(g.scene, scene.id(), "committed chain is for another scene");
    assert_eq!(
        (g.w, g.h, g.frames),
        (W, H, Preset::Full.frames()),
        "committed chain is not the acceptance shape"
    );
    let frames = Preset::Smoke.frames();
    let run = run_anim_scene(scene, W, H, frames, |_, _| {}).expect("long scene renders");
    for (i, h) in run.frame_hashes.iter().enumerate() {
        let got = format!("{h:#018x}");
        assert_eq!(
            got,
            g.frame_hashes[i],
            "{}: frame {i} diverged from the committed chain \
             (re-bless deliberately, as its own commit)",
            scene.id()
        );
    }
    // The per-step series is the scene's reason to exist; a run that stopped
    // producing one would pass the hash check and still be useless.
    assert_eq!(run.dirty_series.len() as u32, frames - 1);
    assert_eq!(run.glyph_bytes.len() as u32, frames);
}

#[test]
fn long_full_matches_the_committed_chain() {
    check_prefix(Scene::Long(Detail::Full));
}

#[test]
fn long_lite_matches_the_committed_chain() {
    check_prefix(Scene::Long(Detail::Lite));
}

/// The two detail levels must be genuinely different scenes — if `Lite` ever
/// collapsed onto `Full` the "variant that fits the MCU arena" would be a
/// second name for a scene that does not fit.
#[test]
fn the_detail_levels_are_different_scenes() {
    let a = golden(Scene::Long(Detail::Full));
    let b = golden(Scene::Long(Detail::Lite));
    assert_ne!(a.run_hash, b.run_hash);
    assert_ne!(a.scene, b.scene);
}

/// The dirty-fraction distribution is the scene's other product. Assert the
/// SHAPE of it rather than exact numbers: a near-static tail, a full-frame
/// repaint, and something in between. A scene that only ever produced one
/// dirty fraction could not test the dirty-rect cost model at all.
#[test]
fn the_scene_spans_the_dirty_fraction_range() {
    let frames = Preset::Smoke.frames();
    let run = run_anim_scene(Scene::Long(Detail::Full), W, H, frames, |_, _| {})
        .expect("long scene renders");
    let pcts = run.dirty_pct_series();
    let min = pcts.iter().copied().fold(f64::INFINITY, f64::min);
    let max = pcts.iter().copied().fold(0.0f64, f64::max);
    assert!(min < 10.0, "no near-static frames: min dirty {min:.2}%");
    assert!(max >= 99.9, "no full-frame repaint: max dirty {max:.2}%");
    let mid = pcts.iter().filter(|&&p| (10.0..90.0).contains(&p)).count();
    assert!(mid > frames as usize / 4, "too few mid-range frames: {mid}");
}

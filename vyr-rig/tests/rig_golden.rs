//! F18 short-chain golden — the in-`cargo test` slice of the rig acceptance:
//! 60 frames at the 120 rung (one logical second: two toggle edges, a
//! progress wrap, the slider/mover/counter/checker all moving). `run_anim`
//! asserts non-blank + INCREMENTAL==FULL byte-equality on EVERY frame
//! internally; this golden pins the resulting FNV hash chain so `cargo test`
//! catches any pixel drift in seconds. The full 600-frame @480 acceptance
//! run (PNG sequence, lossless video, the committed `vyr-rig/hashchain.json`)
//! is `./dev.py anim`.

use vyr_rig::{FNV_OFFSET, fnv1a_chain, run_anim};

/// Committed golden: the chained run hash of 60 frames @ 120×68.
/// Re-bless via `./dev.py test --bless` (prints the new value).
const SHORT_CHAIN_FNV1A: u64 = 0xCC56_1580_50BD_2EC9;

#[test]
fn short_chain_golden() {
    let run = run_anim(120, 68, 60, |_, _| {}).expect("anim runs (per-frame equality inside)");
    assert_eq!(run.frame_hashes.len(), 60);
    // Chain self-consistency: the run hash IS the fold of the frame hashes.
    let mut folded = FNV_OFFSET;
    for &fh in &run.frame_hashes {
        folded = fnv1a_chain(folded, fh);
    }
    assert_eq!(folded, run.run_hash, "chain fold mismatch");
    if std::env::var_os("VYR_BLESS").is_some() {
        eprintln!("BLESS: SHORT_CHAIN_FNV1A = {:#018X}", run.run_hash);
        return;
    }
    assert_eq!(
        run.run_hash, SHORT_CHAIN_FNV1A,
        "rig short chain drifted — investigate before re-blessing"
    );
}

/// The animation must actually animate: consecutive frames differ (the
/// counter + slider + mover change EVERY frame) and the incremental path
/// repaints a real-but-small fraction of the screen.
#[test]
fn anim_actually_animates() {
    let run = run_anim(120, 68, 12, |_, _| {}).expect("anim runs");
    for i in 1..run.frame_hashes.len() {
        assert_ne!(
            run.frame_hashes[i],
            run.frame_hashes[i - 1],
            "frames {} and {} hash identically — the scene stopped animating",
            i - 1,
            i
        );
    }
    assert!(run.dirty_px > 0, "no dirty pixels — nothing animated");
    assert!(
        run.dirty_pct() < 80.0,
        "dirty {:.1}%/step — the scene degenerated into full repaints",
        run.dirty_pct()
    );
}

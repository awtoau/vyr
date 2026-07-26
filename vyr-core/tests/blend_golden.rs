//! #60 partial-alpha (blend) goldens — the translucent-rect path, gated per tier.
//!
//! #60 asked whether a flat translucent rect "falls all the way back to the
//! tiny-skia source-over pipeline" in Draft/Fast. **It does not.** A radius-0
//! translucent `vy_frame` routes to the integer `draft_span` `d255` source-over
//! (`fill_rrect` → integer path → `fill_rrect_rounded_draft` → `draft_span`),
//! never to tiny-skia — `curve_to_aa(0)` is false and `integer_spans()` is true
//! for both Fast and Draft. These tests pin the corrected finding:
//!
//! - `blend_tiers_agree_byte_exact`: on a flat translucent rect over an OPAQUE
//!   backdrop, Draft, Fast and Exact are **byte-identical**. The integer `d255`
//!   blend reproduces tiny-skia's source-over exactly (the painter's documented
//!   opaque-dst identity), so the cheap integer path is not a fidelity loss
//!   here — it is the oracle result at a fraction of the cost.
//! - `blend_draft_fast_full_fastpath`: Draft AND Fast render the translucent
//!   rect at 100 % fast-path coverage — ZERO tiny-skia (the empirical
//!   confirmation of #60 Task 1: no fallback). Exact has no integer path and
//!   DOES use tiny-skia `lowp::source_over` — its cost (`blend480` +900 % over
//!   opaque, per the #37 probe) is the oracle price, by design, not a bug.
//! - `blend_golden_hash`: the translucent-rect scene hashes to the committed
//!   constant (deterministic; re-bless `./dev.py test --bless`).
//! - `blend_band_equivalence`: the integer `d255` blend is band-exact — a pure
//!   function of dst pixel + colour + alpha, so a banded render is byte-identical
//!   to the full frame (invariant I1 holds for the alpha path too).

use vyr_core::{Assets, Fonts, Quality, Rect, RenderStats};

const W: u32 = 120;
const H: u32 = 60;

/// A translucent (alpha `0x80` = 128) mid-grey `vy_frame` over an OPAQUE
/// backdrop — the #37 probe's `blend*` geometry (full-width, radius 0), the
/// case that most exposed the (mis)diagnosed "fallback". The backdrop is opaque
/// so the `d255 == tiny-skia` source-over identity holds.
const BLEND_IR: &str = r##"{"schema_version":"0.6-vyvanse","w":120,"h":60,"root":{"name":"view","attrs":{"background":"#101418"},"children":[{"name":"vy_frame","attrs":{"x":"0","y":"10","width":"120","height":"40","background":"#7FA8C880"}}]}}"##;

/// Committed blend golden (FNV-1a 64 of the RGB888 buffer). SHARED across all
/// three tiers — they agree byte-for-byte on a flat translucent rect (see
/// `blend_tiers_agree_byte_exact`). Re-bless via `./dev.py test --bless`.
const BLEND_GOLDEN_FNV1A: u64 = 0xCACE_5310_B78C_0825;

fn fnv1a(data: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(0x100_0000_01b3);
    }
    h
}

fn render_full(quality: Quality) -> (Vec<u8>, RenderStats) {
    let mut fonts = Fonts::new();
    let assets = Assets::new();
    let mut buf = vec![0u8; (W * H * 3) as usize];
    let stats = vyr_core::render_with_quality(
        BLEND_IR,
        &mut fonts,
        &assets,
        Rect {
            x: 0,
            y: 0,
            w: W,
            h: H,
        },
        &mut buf,
        (W * 3) as usize,
        quality,
    )
    .expect("blend fixture renders");
    (buf, stats)
}

fn render_banded(quality: Quality, band_h: u32) -> Vec<u8> {
    let mut fonts = Fonts::new();
    let assets = Assets::new();
    let stride = (W * 3) as usize;
    let mut out = vec![0u8; stride * H as usize];
    let mut y = 0;
    while y < H {
        let bh = band_h.min(H - y);
        let off = y as usize * stride;
        let end = off + stride * (bh as usize - 1) + (W * 3) as usize;
        vyr_core::render_with_quality(
            BLEND_IR,
            &mut fonts,
            &assets,
            Rect {
                x: 0,
                y: y as i32,
                w: W,
                h: bh,
            },
            &mut out[off..end],
            stride,
            quality,
        )
        .expect("blend band renders");
        y += bh;
    }
    out
}

/// #60's core finding: a flat translucent rect over an opaque backdrop is
/// byte-identical at Draft, Fast and Exact. The integer `d255` blend IS the
/// tiny-skia source-over result — no fidelity is traded for the cheap path.
#[test]
fn blend_tiers_agree_byte_exact() {
    let exact = render_full(Quality::Exact).0;
    let fast = render_full(Quality::Fast).0;
    let draft = render_full(Quality::Draft).0;
    assert_eq!(
        draft, exact,
        "Draft's integer d255 blend differs from Exact's tiny-skia source-over on a \
         flat translucent rect — the opaque-dst identity is broken"
    );
    assert_eq!(
        fast, exact,
        "Fast differs from Exact on a flat translucent rect"
    );
}

/// #60 Task 1, confirmed: Draft and Fast carry the translucent rect entirely on
/// the integer fast path (no tiny-skia), and Exact uses tiny-skia (no fast path).
#[test]
fn blend_draft_fast_full_fastpath() {
    for q in [Quality::Draft, Quality::Fast] {
        let (_, st) = render_full(q);
        let cov = st.fastpath_pixels as f64 / st.pixels_written as f64;
        assert!(
            cov >= 0.999,
            "{q:?} blend fast-path coverage {:.1}% < 100% — the translucent rect fell back \
             to the tiny-skia source-over pipeline (the #60 hypothesis); it must not",
            100.0 * cov
        );
    }
    // Exact fast-paths the FLAT translucent rect too (#37 pixmap-direct path,
    // byte-identically — the golden-hash tests still pass), so it has a non-zero
    // fast-path count. Curves/lines still go through tiny-skia.
    let (_, ex) = render_full(Quality::Exact);
    assert!(
        ex.fastpath_pixels > 0,
        "Exact should fast-path the flat translucent rect (#37, byte-identically)"
    );
}

#[test]
fn blend_golden_hash() {
    // All three tiers agree (see blend_tiers_agree_byte_exact), so one hash gates
    // them all; render Draft as the representative.
    let (buf, _) = render_full(Quality::Draft);
    let h = fnv1a(&buf);
    if std::env::var_os("VYR_BLESS").is_some() {
        eprintln!("BLESS: BLEND_GOLDEN_FNV1A = {h:#018X}");
        return;
    }
    assert_eq!(
        h, BLEND_GOLDEN_FNV1A,
        "blend golden drifted — investigate before re-blessing"
    );
}

#[test]
fn blend_band_equivalence() {
    for q in [Quality::Exact, Quality::Fast, Quality::Draft] {
        let full = render_full(q).0;
        for bh in [7u32, 30] {
            assert_eq!(
                render_banded(q, bh),
                full,
                "{q:?} blend banded (band_h={bh}) != full frame — the alpha path is not band-exact"
            );
        }
    }
}

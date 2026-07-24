//! `--features testcard` — the **labelled colour test card**: a display
//! bring-up fixture that turns "does that look right?" into "read the label".
//!
//! # The question it exists to answer
//!
//! [`crate::present`] renders RGB888 straight into the SDRAM framebuffer and
//! then applies an R/B swap pass, on the strength of RM0090's statement that
//! LTDC's RGB888 layer reads **B at the lowest address** while vyr-core writes
//! R, G, B. That swap costs 59.7 ms against the 12.59 ms blit it replaced —
//! 4.7×, which reverses the verdict for direct rendering. Nobody has ever
//! confirmed the premise on glass, because the only thing ever displayed was
//! [`crate::lcd::PANEL_IR`], a dashboard mock-up whose colours nobody can
//! adjudicate from memory.
//!
//! So: same renderer, same entry point, same IR pipeline — a scene where every
//! swatch **carries the name of the colour it is supposed to be**. If the block
//! labelled `RED` is blue, the channels are swapped. No interpretation, and a
//! photograph is evidence.
//!
//! # Two regions, and why they are two
//!
//! | rows | what | hashed? |
//! |---|---|---|
//! | `0 .. CARD_BODY_H` (280) | [`CARD_IR`], a committed asset — swatches, ramps, captions | **yes** — [`render_card`] folds it |
//! | `CARD_BODY_H .. PANEL_H` (280..320) | the build-identity strip, composed at run time by [`identity_ir`] | no |
//!
//! The identity strip has to say which presentation path drew the picture, what
//! pixel format it wrote and where the framebuffer is — so it necessarily
//! DIFFERS per path, and a hash over it could not be compared. Splitting the
//! two is what lets the card body's hash be byte-identical across the SPI,
//! banded-LTDC and direct paths and across x86-64 / emulated M4 / real M4,
//! while a photograph of the panel is still self-describing. A card that does
//! not say what produced it is evidence of nothing.
//!
//! # Nothing here is a new render path
//!
//! [`banded`] is the same loop every other leg runs: caller-owned buffer,
//! arbitrary sub-rect, explicit stride, `render_with_quality` (invariant I1).
//! The paths that own a framebuffer ([`crate::ltdc`], [`crate::present`]) call
//! their OWN existing rect renderers with these rects instead, so the card
//! travels each path's real code, not a copy of it.
//!
//! # Two cards, one fixture
//!
//! `--features orientcard` compiles `assets/orientcard.json` in place of
//! `assets/testcard.json` and changes [`CARD_NAME`] / [`READ_ME`]. Nothing else
//! differs — same rects, same hash discipline, same [`reclaim`]. It answers the
//! question the colour card cannot: which PHYSICAL corner of the glass is the
//! renderer's `(0,0)`, which way `+X` and `+Y` run, and whether the scan is
//! mirrored. All eight orientations (four rotations × mirrored/not) look
//! different, so a single glance is a complete report.
//!
//! One consequence is load-bearing. The direct path ([`crate::present`]) draws
//! the COLOUR card with **no** R/B correction by default, because whether the
//! correction is needed is what that card is asking. For the orientation card
//! the answer was already in — read off the glass: LTDC's RGB888 layer is
//! B-first — so by default the direct path applies
//! [`crate::present::swap_rb_in_place`] and the orientation card appears in
//! true colours. A card whose colours lie would only muddle an orientation
//! reading, and its wedge/badge colours are printed in its own legend.
//!
//! [`crate::present::RB_SWAP`] (`VYR_RB_SWAP`) overrides that default, and the
//! MADCTL sweep sets it to `0` on BOTH cards: MADCTL's `BGR` bit swaps exactly
//! the same two channels, so with both active they cancel and a
//! correct-looking panel would be a wrong configuration read as a right one.
//! Whichever way it is set, the card's identity strip prints it on the glass.

// Which helpers are live depends on which leg is compiled, and every
// combination leaves some of them unused: the direct path (`present`)
// renders through its OWN framebuffer-window renderer and takes only the IR
// and the rects from here, while the SPI / banded / host / emulated-M4 legs
// go through `render_card` and `banded`. Warning about that per feature set
// would be noise about a module that is entirely reachable across the set of
// builds it exists for.
#![allow(dead_code)]

use alloc::format;
use alloc::string::String;

use vyr_core::{Assets, Fonts, Quality, Rect, RenderError, ir::Request};

// --- panel geometry ---------------------------------------------------------
//
// Restated here rather than imported, because [`crate::lcd`] is compiled ONLY
// for `target_os = "none"` (it pokes F429 registers) and this module also runs
// on the host and under qemu, where the card is rendered for its hash and its
// reference PNG with no panel in sight. The static asserts below make the
// restatement checkable rather than a second source of truth: when `lcd` IS
// compiled, a divergence is a compile error.

/// Panel width — `crate::lcd::PANEL_W`.
pub const PANEL_W: u32 = 240;
/// Panel height — `crate::lcd::PANEL_H`.
pub const PANEL_H: u32 = 320;
/// Band height — `crate::lcd::PANEL_BAND_H`.
const PANEL_BAND_H: u32 = 16;
/// FNV-1a 64 offset basis — `crate::lcd::FNV_OFFSET`.
pub const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x100_0000_01b3;

#[cfg(all(target_os = "none", any(feature = "lcd", feature = "ltdc")))]
const _: () = {
    assert!(PANEL_W == crate::lcd::PANEL_W);
    assert!(PANEL_H == crate::lcd::PANEL_H);
    assert!(PANEL_BAND_H == crate::lcd::PANEL_BAND_H);
    assert!(FNV_OFFSET == crate::lcd::FNV_OFFSET);
};

fn fnv1a_fold(mut h: u64, data: &[u8]) -> u64 {
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(FNV_PRIME);
    }
    h
}

/// The card body, generated by `scripts/make-testcard.py` /
/// `scripts/make-orientcard.py` (geometry is arithmetic there, not a
/// hand-written claim about equal steps).
///
/// A committed asset rather than an inline `const`: the SAME bytes are rendered
/// by `vyr-cli` on the host to produce the reference PNG, so "the panel should
/// look like the PNG" is a statement about one file, not two transcriptions.
///
/// `--features orientcard` swaps the asset and nothing else. Two questions, two
/// pictures, ONE fixture: the band loop, the body/identity split, the hash and
/// [`reclaim`] are shared, so a second card cannot drift from the first in any
/// way that matters. Both cards are 240×320 with the same [`CARD_BODY_H`], so
/// every rect and every hashed region below is card-independent.
#[cfg(not(feature = "orientcard"))]
pub const CARD_IR: &str = include_str!("../assets/testcard.json");
#[cfg(feature = "orientcard")]
pub const CARD_IR: &str = include_str!("../assets/orientcard.json");

/// What this build's card is called, for the one ALERT line each display leg
/// prints. Keeps the per-card wording in ONE place instead of a `cfg` at every
/// emit site.
#[cfg(not(feature = "orientcard"))]
pub const CARD_NAME: &str = "COLOUR TEST CARD";
#[cfg(feature = "orientcard")]
pub const CARD_NAME: &str = "CORNER AND ORIENTATION CARD";

/// The instruction to the human, printed by whichever leg leaves the card on
/// the glass. A fixture whose reading depends on remembering what it was for is
/// not a fixture.
#[cfg(not(feature = "orientcard"))]
pub const READ_ME: &str = "READ THE LABELS: the block labelled RED must be red, \
     GREEN green, BLUE blue.";
#[cfg(feature = "orientcard")]
pub const READ_ME: &str = "REPORT TWO THINGS. (1) Which PHYSICAL corner of the \
     panel shows the solid white badge numbered 1 — top-left, top-right, \
     bottom-right or bottom-left as YOU are holding it. (2) Which physical \
     direction the yellow 'X+ ->' wedge thickens in (left-to-right, \
     right-to-left, top-to-bottom or bottom-to-top). Two cross-checks come \
     free: the badges 1>2>3>4 must run CLOCKWISE on the glass, and the text \
     must read forwards — either one reversed means the scan is MIRRORED, not \
     merely rotated. Compare against the host reference PNG, which is the same \
     card rendered on x86-64.";

/// Rows `0 .. 280`: the hashed card body. Must match `make-testcard.py`.
pub const CARD_BODY_H: u32 = 280;

/// The card body's rect — what [`render_card`] draws and hashes.
pub const CARD_RECT: Rect = Rect {
    x: 0,
    y: 0,
    w: PANEL_W,
    h: CARD_BODY_H,
};

/// The identity strip's rect — rows `280 .. 320`, never hashed.
pub const IDENT_RECT: Rect = Rect {
    x: 0,
    y: CARD_BODY_H as i32,
    w: PANEL_W,
    h: PANEL_H - CARD_BODY_H,
};

/// Build the identity strip's IR: three lines of ~40 characters at 11 px, on
/// black, occupying exactly [`IDENT_RECT`].
///
/// The lines are the caller's because only the caller knows them: which
/// presentation path is drawing, what the pixel format and framebuffer address
/// are, and whether any correcting pass was applied. Keep each ≤ ~40 chars —
/// at 11 px Roboto that is about 225 px of the 240 available.
pub fn identity_ir(l1: &str, l2: &str, l3: &str) -> String {
    let y0 = CARD_BODY_H;
    format!(
        r##"{{"schema_version":"0.6-vyvanse","w":{PANEL_W},"h":{PANEL_H},
  "root": {{"name":"view","attrs":{{"background":"#000000"}},"children":[
    {{"name":"vy_label","attrs":{{"x":"4","y":"{}","width":"232","height":"13",
      "text":{l1:?},"color":"#FFFFFF","font_family":"roboto","font_size":"11"}}}},
    {{"name":"vy_label","attrs":{{"x":"4","y":"{}","width":"232","height":"13",
      "text":{l2:?},"color":"#FFFFFF","font_family":"roboto","font_size":"11"}}}},
    {{"name":"vy_label","attrs":{{"x":"4","y":"{}","width":"232","height":"13",
      "text":{l3:?},"color":"#FFFFFF","font_family":"roboto","font_size":"11"}}}}
  ]}}}}"##,
        y0 + 1,
        y0 + 14,
        y0 + 27
    )
}

/// Render `rect` band by band into `band_buf` at the RECT's pitch, folding
/// every output byte into `hash_in` and handing each finished band to `flush`.
///
/// This is [`crate::lcd::show_panel_scene`]'s loop with the flush pulled out as
/// a closure, so the SPI leg, the host leg and the emulated-M4 leg all use one
/// implementation and cannot drift. `flush` receives `(y, h, &band_buf[..len])`
/// with `y` in SCREEN coordinates.
///
/// Nothing here is timed: this is a bring-up fixture, and its cost is not a
/// number anyone should quote.
// Eight, and each one is load-bearing: the request, the two registries, the
// caller's buffer, the tier, the rect, the fold to continue and the flush.
// Same shape and same reason as `ltdc::render_rect_from`.
#[allow(clippy::too_many_arguments)]
pub fn banded<F>(
    req: &Request,
    fonts: &mut Fonts,
    assets: &Assets,
    band_buf: &mut [u8],
    quality: Quality,
    rect: Rect,
    hash_in: u64,
    mut flush: F,
) -> Result<u64, RenderError>
where
    F: FnMut(u32, u32, &[u8]),
{
    let stride = (rect.w * 3) as usize;
    let mut hash = hash_in;
    let mut y = 0u32;
    while y < rect.h {
        let h = PANEL_BAND_H.min(rect.h - y);
        let band = Rect {
            x: rect.x,
            y: rect.y + y as i32,
            w: rect.w,
            h,
        };
        let len = stride * h as usize;
        req.render_with_quality(fonts, assets, band, &mut band_buf[..len], stride, quality)?;
        hash = fnv1a_fold(hash, &band_buf[..len]);
        flush(rect.y as u32 + y, h, &band_buf[..len]);
        y += h;
    }
    Ok(hash)
}

/// Parse + render the card body through [`banded`] and return its FNV-1a 64
/// hash — the number that must agree across every presentation path and both
/// ISAs. Fresh fold, so it is comparable with a framebuffer read-back that
/// starts its own.
pub fn render_card<F>(
    fonts: &mut Fonts,
    assets: &Assets,
    band_buf: &mut [u8],
    quality: Quality,
    flush: F,
) -> Result<u64, RenderError>
where
    F: FnMut(u32, u32, &[u8]),
{
    let req = Request::parse(crate::opaque(CARD_IR))?;
    banded(
        &req, fonts, assets, band_buf, quality, CARD_RECT, FNV_OFFSET, flush,
    )
}

/// Live/peak heap bytes, where there is an allocator that counts them.
fn heap() -> (usize, usize) {
    #[cfg(target_os = "none")]
    {
        crate::m4::heap_now()
    }
    #[cfg(not(target_os = "none"))]
    {
        (0, 0)
    }
}

/// Throw away the mock-up's registries and start the card's from scratch,
/// reporting the heap either side.
///
/// Load-bearing on the board, not tidiness. The card's parsed tree is 68 IR
/// nodes and every node costs a `serde_json::Map` allocation; measured on the
/// STM32F429I-DISC1's 120 KiB arena, parsing it on TOP of the panel mock-up's
/// tree, its glyph cache at two more sizes, the heap copy of the TTF and the
/// 32,768 B band pixmap ran the allocator out — `memory allocation of 5120
/// bytes failed`, which is the `children` Vec doubling as the last swatch is
/// pushed. The emulated-M4 leg renders the card and NOTHING else in the same
/// 120 KiB and fits comfortably, so restoring that footprint before the card is
/// exactly the fix: drop the previous scene's `Request`, replace both
/// registries, re-register the one font the card asks for.
///
/// The caller must have dropped its own `Request` first — this cannot do it,
/// and it is the single largest item.
pub fn reclaim(
    emit: &mut dyn FnMut(&str),
    fonts: &mut Fonts,
    assets: &mut Assets,
    font: &[u8],
) -> Result<(), RenderError> {
    let (before, peak) = heap();
    // Assignment, not shadowing: the old registries must actually be DROPPED.
    *fonts = Fonts::new();
    *assets = Assets::new();
    let (freed, _) = heap();
    fonts.register("roboto", crate::opaque(font).to_vec())?;
    let (after, _) = heap();
    emit(&alloc::format!(
        "INFO  [vyr-size] testcard: reclaimed for the card — heap live {before} B \
         -> {freed} B after dropping the scene's registries -> {after} B with the \
         font re-registered (peak so far {peak} B of the arena)"
    ));
    Ok(())
}

/// The one line every leg prints, so one grep answers "did every path draw the
/// same card?".
pub fn report(emit: &mut dyn FnMut(&str), path: &str, hash: u64, quality: Quality) {
    let (live, peak) = heap();
    emit(&format!(
        "INFO  [vyr-size] testcard: path={path} body={}x{} fnv1a={hash:#018x} \
         quality={quality:?} heap_live={live} B heap_peak={peak} B (hash covers \
         rows 0..{CARD_BODY_H} only — the identity strip below is per-path text \
         and is deliberately outside it)",
        PANEL_W, CARD_BODY_H
    ));
}

//! `--features present` (#45) — **measure both presentation modes** on the
//! real STM32F429I-DISC1 instead of assuming which one wins.
//!
//! `docs/plan.md` §"Presentation modes" lists three display topologies that
//! the single entry point `render(tree, area, buf, stride)` is supposed to
//! serve. Only ONE of them has ever been measured on silicon (partial + flush,
//! [`crate::ltdc`]). This module measures the other resident-framebuffer one
//! against it, same scene, same tier, same clock, same DWT window discipline:
//!
//! | mode | what the renderer writes into | display cost afterwards |
//! |---|---|---|
//! | **Banded + flush** ([`crate::ltdc::render_rect`]) | a 240×16 RGB888 band buffer in **CCM** | a convert-and-copy pass: RGB888 → RGB565 into SDRAM |
//! | **Direct** (here) | the **SDRAM framebuffer itself**, `stride` = the screen pitch | none — there is no second pass |
//!
//! Direct trades a whole blit pass for slower stores during rendering. Which
//! wins is an empirical question about how many times the renderer touches a
//! pixel and how much slower SDRAM is than CCM, and that is what this measures.
//!
//! # No vyr-core change was needed, and that is the point
//!
//! Direct mode is `render_with_quality(.., area = the band, buf = &mut
//! fb[row_offset..], stride = PANEL_W * 3)`. Invariant I1 says `buf` is the
//! caller's and the origin of `buf` is not the origin of the screen; both were
//! already true, so pointing `buf` at external memory is a CALLER decision that
//! needed no new code path, no full-frame special case and no `unsafe` inside
//! core. The only `unsafe` is here, forming a `&mut [u8]` over the FMC
//! aperture — measurement scaffolding's own territory.
//!
//! # The pixel format is the whole difficulty
//!
//! vyr-core emits **RGB888**. The scan-out framebuffer the `ltdc` leg uses is
//! **RGB565**, and the conversion is exactly what the blit pays for. Three ways
//! out, and this module takes the second:
//!
//! 1. **Convert during the blit** — what banded mode does. Measured here as the
//!    control.
//! 2. **Keep an RGB888 framebuffer** and tell LTDC so (`L1PFCR = 1`). Costs 1.5×
//!    the SDRAM (230,400 B vs 153,600 B) and 1.5× the scan-out read bandwidth
//!    (15.0 MB/s vs 10.0 MB/s at the measured 65.33 Hz), and makes direct
//!    rendering possible AT ALL, because `finish_into_rgb888` writes RGB888 and
//!    nothing else.
//! 3. **Render RGB565 natively** — issue #22 `OutFmt`, unimplemented and
//!    deliberately NOT implemented here. It is the only option that makes
//!    direct rendering genuinely single-touch: today even "direct" is
//!    pixmap → convert → framebuffer, and #22 would make it
//!    pixmap → framebuffer at 2 B/px instead of 3.
//!
//! ## The channel order, said out loud
//!
//! LTDC's RGB888 layer format reads **B at the lowest address**, then G, then R
//! (RM0090 pixel-data mapping; the same little-endian order as its ARGB8888).
//! vyr-core writes R, G, B. So a framebuffer written directly by the renderer
//! and scanned as `L1PFCR = 1` displays **red and blue swapped**. That is a
//! property of the silicon's format, not a rendering bug: the BYTES are
//! identical to what banded mode produced, which is what
//! [`fb888_rect_hash`] proves.
//!
//! [`swap_rb_in_place`] fixes it as a separate, separately-PRICED pass, so the
//! verdict can be read either way — "direct, bytes correct, colours swapped on
//! glass" or "direct + a correcting pass" — and so a human can look at the
//! panel and see the direct-rendered frame in its true colours.
//!
//! # What is timed, and what is not
//!
//! Every DWT window here wraps exactly one thing. Frame hashes, framebuffer
//! read-backs, mode switching and reporting are all OUTSIDE every window. The
//! 480×270 measurement window in [`crate::workload::run`] is never entered from
//! here, exactly as for [`crate::ltdc`].

use alloc::vec::Vec;

use vyr_core::{Assets, Fonts, Quality, Rect, RenderError, dirty_rects, ir::Request};

use crate::lcd::{self, PANEL_BAND_H, PANEL_H, PANEL_W, fnv1a_fold, rd, wr};
use crate::ltdc::{
    self, Cost, FB_BASE, L1_CFBAR, L1_CFBLR, L1_CR, L1_PFCR, LTDC_ICR, LTDC_ISR, LTDC_LX_CR_LEN,
    LTDC_SRCR, LTDC_SRCR_IMR, SDRAM_BASE, cyc, wait_ms,
};

// --- the second framebuffer -------------------------------------------------

/// The **RGB888** scan-out framebuffer, placed clear of the RGB565 one so both
/// can be resident and LTDC can be pointed at either without re-rendering.
///
/// `0xD0000000 + 0x40000` = 262,144 B in, comfortably past the RGB565 frame's
/// 153,600 B, and 4-aligned (LTDC fetches 32-bit words; the renderer's row
/// starts are 4-aligned too because `PANEL_W * 3 = 720` is a multiple of 4).
pub const FB888_BASE: u32 = SDRAM_BASE + 0x0004_0000;
/// 240 × 320 × 3 = 230,400 B — **1.5×** the RGB565 framebuffer.
pub const FB888_BYTES: u32 = PANEL_W * PANEL_H * 3;
/// Row pitch of the RGB888 framebuffer, in bytes: the `stride` direct mode
/// hands to `render()`.
const FB888_STRIDE: usize = (PANEL_W * 3) as usize;

/// Where the bandwidth probes write: 1 MiB into SDRAM, past **both**
/// framebuffers, so a probe can never corrupt a frame under test.
const PROBE_BASE: u32 = SDRAM_BASE + 0x0010_0000;
/// One probe burst. 8 KiB is long enough to average out loop-entry effects and
/// short enough (~126 µs at the measured ~65 MB/s) to fit entirely inside the
/// panel's 8-line vertical blanking interval (~373 µs), which is what makes the
/// blanking-vs-active comparison below meaningful.
const BURST_BYTES: u32 = 8192;

// --- LTDC layer format switching --------------------------------------------

/// Which framebuffer LTDC is scanning, and in which format.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Fmt {
    /// `L1PFCR = 2`, 153,600 B at [`FB_BASE`] — the banded leg's target.
    Rgb565,
    /// `L1PFCR = 1`, 230,400 B at [`FB888_BASE`] — direct mode's target.
    Rgb888,
}

impl Fmt {
    fn bytes_per_px(self) -> u32 {
        match self {
            Fmt::Rgb565 => 2,
            Fmt::Rgb888 => 3,
        }
    }
    fn base(self) -> u32 {
        match self {
            Fmt::Rgb565 => FB_BASE,
            Fmt::Rgb888 => FB888_BASE,
        }
    }
    fn pfcr(self) -> u32 {
        match self {
            Fmt::Rgb565 => 2,
            Fmt::Rgb888 => 1,
        }
    }
}

/// Point the live layer at `fmt`'s framebuffer and reload the shadow registers.
///
/// Address, pixel format and line length must move together — a half-applied
/// switch scans the right memory with the wrong pitch and produces a sheared
/// picture that looks like a rendering bug. `LTDC_SRCR = IMR` applies all three
/// atomically at the next reload; the `+3` in the line-length field is the
/// silicon's definition, not a typo (see [`crate::ltdc::ltdc_init`]).
pub fn fb_select(fmt: Fmt) {
    let line = PANEL_W * fmt.bytes_per_px();
    wr(L1_PFCR, fmt.pfcr());
    wr(L1_CFBAR, fmt.base());
    wr(L1_CFBLR, (line << 16) | (line + 3));
    wr(LTDC_SRCR, LTDC_SRCR_IMR);
}

/// Turn the layer off (LTDC keeps scanning, emitting only the background
/// colour) so a probe runs with **zero framebuffer read traffic** on the bus.
/// This is the contention-free control, and it is the only way to attribute a
/// slowdown to scan-out rather than to the SDRAM itself.
fn layer_enable(on: bool) {
    let v = rd(L1_CR);
    wr(
        L1_CR,
        if on {
            v | LTDC_LX_CR_LEN
        } else {
            v & !LTDC_LX_CR_LEN
        },
    );
    wr(LTDC_SRCR, LTDC_SRCR_IMR);
}

/// LTDC_ISR bit 1 — FIFO underrun: the controller asked for pixel data and the
/// bus did not deliver in time. **Hardware's own account of bus starvation**,
/// which is exactly the failure mode a CPU hammering the same SDRAM would
/// cause. Sticky until cleared through LTDC_ICR.
const ISR_FUIF: u32 = 1 << 1;
/// LTDC_ISR bit 2 — transfer error (bus error on a framebuffer fetch).
const ISR_TERRIF: u32 = 1 << 2;

fn isr_clear() {
    wr(LTDC_ICR, 0xF);
}

fn isr_read() -> u32 {
    rd(LTDC_ISR)
}

// --- raw memory probes ------------------------------------------------------

/// Write `bytes` of 32-bit stores to `base`, return DWT cycles.
///
/// Deliberately the SAME loop shape as [`crate::ltdc::sdram_test`]'s bulk pass
/// (one `write_volatile` per iteration, pointer bumped by 4) so the numbers
/// here are directly comparable with the 8 MiB figure that leg already prints.
/// The loop overhead is therefore also the same and is NOT netted out — both
/// numbers describe "what a store loop achieves", which is what a renderer
/// writing pixels actually is.
fn write_burst(base: u32, bytes: u32) -> u32 {
    let t0 = cyc();
    let mut p = base as *mut u32;
    let end = (base + bytes) as *mut u32;
    while p < end {
        // SAFETY: `base .. base+bytes` is inside a memory aperture the caller
        // has proven (SDRAM by `sdram_test`, CCM by being a live static), and
        // is 4-aligned by construction.
        unsafe {
            p.write_volatile(0xA5A5_5A5A);
            p = p.add(1);
        }
    }
    cyc().wrapping_sub(t0)
}

/// Read `bytes` of 32-bit loads from `base`, return (cycles, fold) — the fold
/// keeps the loads live.
fn read_burst(base: u32, bytes: u32) -> (u32, u32) {
    let t0 = cyc();
    let mut acc = 0u32;
    let mut p = base as *const u32;
    let end = (base + bytes) as *const u32;
    while p < end {
        // SAFETY: as `write_burst`.
        unsafe {
            acc = acc.wrapping_add(p.read_volatile());
            p = p.add(1);
        }
    }
    (cyc().wrapping_sub(t0), acc)
}

/// Bytes per second achieved, as MB/s (10^6), from a byte count and a cycle
/// count at the achieved core clock.
fn mbps(bytes: u64, cycles: u32, hz: u32) -> u32 {
    if cycles == 0 {
        return 0;
    }
    ((bytes * hz as u64) / (cycles as u64 * 1_000_000)) as u32
}

/// Kilobytes per second — for probes whose MB/s would round to a useless
/// integer.
fn kbps(bytes: u64, cycles: u32, hz: u32) -> u32 {
    if cycles == 0 {
        return 0;
    }
    ((bytes * hz as u64) / (cycles as u64 * 1_000)) as u32
}

// --- scan-out phase ---------------------------------------------------------

/// Wait until LTDC's current line counter is inside `[lo, hi]`.
///
/// `LTDC_CPSR` CYPOS counts TOTAL lines, so lines `0 ..= AVBP` (0..=3) and
/// `> AAH` (>323) are blanking and `AVBP+1 ..= AAH` are active. Budget 50 ms =
/// ~3 frame periods at the measured 65.33 Hz: long enough that a healthy
/// controller always hits the window, short enough to bail rather than hang if
/// scan-out has stopped.
fn wait_line(lo: u32, hi: u32) -> bool {
    wait_ms(50, || {
        let y = rd(ltdc::LTDC_CPSR) & 0xFFFF;
        y >= lo && y <= hi
    })
}

// --- the RGB888 framebuffer -------------------------------------------------

/// A `&mut [u8]` over the RGB888 framebuffer starting at pixel `(x, y)`, long
/// enough for `h` rows of `w` pixels at the screen pitch — precisely the
/// `buf` contract `finish_into_rgb888` asserts (`stride*(h-1) + w*3`, because
/// the last row only needs its own pixels).
///
/// # Safety
///
/// The caller must not hold two overlapping slices at once. Every caller here
/// creates one, hands it to `render`, and drops it before the next band.
unsafe fn fb888_band(x: u32, y: u32, w: u32, h: u32) -> &'static mut [u8] {
    let off = ((y * PANEL_W + x) * 3) as usize;
    let len = FB888_STRIDE * (h as usize - 1) + (w * 3) as usize;
    // SAFETY: `off + len` stays inside FB888_BYTES for any rect clipped to the
    // panel, and the region is in the FMC aperture `sdram_test` proved. The
    // renderer writes bytes; no alignment requirement beyond 1.
    unsafe { core::slice::from_raw_parts_mut((FB888_BASE as usize + off) as *mut u8, len) }
}

/// FNV-1a over `rect` of the RGB888 framebuffer, row by row at the screen
/// pitch — the SAME byte sequence the banded path folds out of its CCM band
/// buffer, which is what makes the two hashes comparable at all.
///
/// `h` is the fold so far, so a multi-rect update chains into ONE hash exactly
/// as the banded path's does.
pub fn fb888_rect_hash_from(mut h: u64, rect: Rect) -> u64 {
    for row in 0..rect.h {
        let off = (((rect.y as u32 + row) * PANEL_W) + rect.x as u32) as usize * 3;
        // SAFETY: inside the framebuffer; read-only.
        let s = unsafe {
            core::slice::from_raw_parts(
                (FB888_BASE as usize + off) as *const u8,
                (rect.w * 3) as usize,
            )
        };
        h = fnv1a_fold(h, s);
    }
    h
}

/// [`fb888_rect_hash_from`] starting a fresh fold.
pub fn fb888_rect_hash(rect: Rect) -> u64 {
    fb888_rect_hash_from(lcd::FNV_OFFSET, rect)
}

/// Whether the direct path applies [`swap_rb_in_place`], the software R/B
/// correction, before the frame reaches the glass.
///
/// **Default is OFF, on every path.** The shipped correction is `MADCTL=0x00`,
/// which was read off the glass to fix both the 180° rotation and the R/B swap
/// on the `present`/direct path at once (badge 1 top-left, X+ wedge yellow, text
/// forwards); with that the software swap is redundant and a second swap would
/// only put red and blue back the wrong way round. The mechanism is KEPT, not
/// deleted: `VYR_RB_SWAP=1` turns the software pass back on — the fallback if the
/// still-pending lcd-path reading at `0x00` refutes the direct-path finding and
/// the panel turns out to be physically BGR-wired.
///
/// `VYR_RB_SWAP=0` turns it off and `VYR_RB_SWAP=1` turns it on. The MADCTL
/// sweep needs it off on EVERY card anyway: MADCTL's `BGR` bit and this pass
/// both swap red and blue, so with both active they cancel and a
/// correct-looking panel would be mistaken for a correct configuration. Every
/// leg logs which corrections were live and the on-glass identity strip prints
/// them, so a photograph can never be read the wrong way round.
pub(crate) const RB_SWAP: bool = rb_swap_from_env();

const fn rb_swap_from_env() -> bool {
    match option_env!("VYR_RB_SWAP") {
        // Default OFF everywhere: MADCTL=0x00 is the shipped R/B correction, so
        // the software pass is redundant unless a build explicitly asks for it.
        None => false,
        Some(s) => {
            let b = s.as_bytes();
            !(b.len() == 1 && b[0] == b'0')
        }
    }
}

/// The same fold with every RGB triple REVERSED — i.e. what the framebuffer
/// should hash to after [`swap_rb_in_place`]. Computing the expectation from
/// the pre-swap contents is what turns "the swap ran" into "the swap is
/// correct". Live in every `present` build: when [`RB_SWAP`] is off it is what
/// the log reports the frame WOULD have become, which is the evidence that the
/// pass was skipped rather than silently broken.
fn fb888_rect_hash_swapped(rect: Rect) -> u64 {
    let mut h = lcd::FNV_OFFSET;
    let mut row_buf = [0u8; (PANEL_W * 3) as usize];
    for row in 0..rect.h {
        let off = (((rect.y as u32 + row) * PANEL_W) + rect.x as u32) as usize * 3;
        let n = (rect.w * 3) as usize;
        // SAFETY: inside the framebuffer; read-only.
        let s = unsafe { core::slice::from_raw_parts((FB888_BASE as usize + off) as *const u8, n) };
        row_buf[..n].copy_from_slice(s);
        for px in row_buf[..n].chunks_exact_mut(3) {
            px.swap(0, 2);
        }
        h = fnv1a_fold(h, &row_buf[..n]);
    }
    h
}

/// Swap R and B in place across the whole RGB888 framebuffer, so LTDC's
/// B-G-R-in-memory layer format displays vyr's R-G-B bytes in their true
/// colours. Returns DWT cycles.
///
/// Four pixels at a time (12 bytes = three 32-bit words), because the FMC is a
/// 16-bit SDRAM and a byte-granular swap would triple the number of bus
/// transactions for the same work. This is a full read-modify-write pass over
/// the framebuffer and is priced as its own line — it is NOT free, and whether
/// it is worth paying is exactly the kind of thing the verdict has to weigh.
///
/// Whether it runs is [`RB_SWAP`]'s decision, not this function's — the call
/// is a runtime `if` on a `const`, so it still folds away when it is off.
pub fn swap_rb_in_place() -> u32 {
    let t0 = cyc();
    let mut p = FB888_BASE as *mut u32;
    let end = (FB888_BASE + FB888_BYTES) as *mut u32;
    while p < end {
        // SAFETY: inside the framebuffer; FB888_BYTES is a multiple of 12, so
        // the three-word group never runs past `end`; 4-aligned throughout.
        unsafe {
            let mut t = [0u8; 12];
            t[0..4].copy_from_slice(&p.read_volatile().to_le_bytes());
            t[4..8].copy_from_slice(&p.add(1).read_volatile().to_le_bytes());
            t[8..12].copy_from_slice(&p.add(2).read_volatile().to_le_bytes());
            for px in t.chunks_exact_mut(3) {
                px.swap(0, 2);
            }
            p.write_volatile(u32::from_le_bytes([t[0], t[1], t[2], t[3]]));
            p.add(1)
                .write_volatile(u32::from_le_bytes([t[4], t[5], t[6], t[7]]));
            p.add(2)
                .write_volatile(u32::from_le_bytes([t[8], t[9], t[10], t[11]]));
            p = p.add(3);
        }
    }
    cyc().wrapping_sub(t0)
}

// --- direct rendering -------------------------------------------------------

/// Render `rect` **straight into the SDRAM framebuffer**, band by band.
///
/// The band loop is byte-for-byte the shape of [`crate::ltdc::render_rect`] —
/// same `PANEL_BAND_H`, same sub-rects, same entry point — and differs in
/// exactly one thing: `buf` is a window onto the framebuffer at the screen
/// pitch instead of a CCM staging buffer at the rect pitch, so there is no
/// second pass. Banding is still required (invariant I1 is not the reason —
/// the tiny-skia pixmap for a full 240×320 area would be 307,200 B and the
/// arena is 122,880 B), which is itself the answer to "does direct mode need a
/// full-frame code path?": no, and it could not have one.
///
/// No hash is folded inside the timed window; the caller reads the framebuffer
/// back afterwards with [`fb888_rect_hash`].
#[allow(clippy::too_many_arguments)]
fn render_direct(
    req: &Request,
    fonts: &mut Fonts,
    assets: &Assets,
    quality: Quality,
    rect: Rect,
    cost: &mut Cost,
    pixels: &mut u64,
) -> Result<(), RenderError> {
    let mut y = 0u32;
    while y < rect.h {
        let h = PANEL_BAND_H.min(rect.h - y);
        let band = Rect {
            x: rect.x,
            y: rect.y + y as i32,
            w: rect.w,
            h,
        };
        // SAFETY: one live slice at a time; dropped at the end of the iteration.
        let buf = unsafe { fb888_band(rect.x as u32, rect.y as u32 + y, rect.w, h) };
        let t0 = cyc();
        let stats = req.render_with_quality(fonts, assets, band, buf, FB888_STRIDE, quality)?;
        cost.render_cycles = cost.render_cycles.wrapping_add(cyc().wrapping_sub(t0));
        *pixels += stats.pixels_written;
        y += h;
    }
    Ok(())
}

// --- the comparison ---------------------------------------------------------

/// How many warmed repetitions each cost figure averages over. Same reasoning
/// as [`crate::ltdc`]'s: the renderer is deterministic, so this averages away
/// DWT granularity and refresh-phase bus contention, not run-to-run variance.
/// Full-frame `Exact` is ~0.4 s a repetition, hence the smaller count there.
const REPS_FULL: u32 = 4;
const REPS_DIRTY: u32 = 8;

/// The three tiers, measured in one flash — `Quality` is a RUNTIME argument to
/// `render_with_quality`, so all three code paths are linked in every build and
/// no per-tier firmware rebuild is needed (unlike the `draft`/`fast` cargo
/// features, which only select the 480×270 workload's constant).
const TIERS: [(Quality, &str); 3] = [
    (Quality::Exact, "Exact"),
    (Quality::Fast, "Fast"),
    (Quality::Draft, "Draft"),
];

/// `PANEL_IR` with the readout and the two sliders moved — the "next frame" of
/// a dashboard. Feeding this and `PANEL_IR` to [`dirty_rects`] yields the rects
/// a real incremental update would repaint, which is the geometry the
/// dirty-rect half of the comparison must use if it is to mean anything.
const PANEL_IR_NEXT: &str = r##"{
  "schema_version": "0.6-vyvanse",
  "w": 240, "h": 320,
  "root": {"name": "view", "attrs": {"background": "#22262B"}, "children": [
    {"name": "vy_frame", "attrs": {"x": "8", "y": "8", "width": "224", "height": "40",
      "background": "#2E3440", "radius": "8", "border_width": "1", "border_color": "#4C566A"}},
    {"name": "vy_label", "attrs": {"x": "20", "y": "20", "width": "170", "height": "20",
      "text": "vyr on F429I-DISC1", "color": "#ECEFF4"}},
    {"name": "vy_image", "attrs": {"x": "200", "y": "16", "width": "24", "height": "24",
      "src": "checker-24.png"}},
    {"name": "vy_gauge", "attrs": {"x": "16", "y": "60", "width": "110", "height": "110",
      "value": "65", "color": "#88C0D0"}},
    {"name": "vy_lcd", "attrs": {"x": "138", "y": "100", "width": "90", "height": "24",
      "text": "1481", "color": "#A3BE8C", "style_text_font": "roboto_20"}},
    {"name": "vy_label", "attrs": {"x": "140", "y": "132", "width": "90", "height": "16",
      "text": "rpm", "color": "#7A869A"}},
    {"name": "vy_slider", "attrs": {"x": "16", "y": "186", "width": "208", "height": "18",
      "value": "70"}},
    {"name": "vy_slider", "attrs": {"x": "16", "y": "214", "width": "208", "height": "18",
      "value": "35"}},
    {"name": "vy_progress", "attrs": {"x": "16", "y": "244", "width": "208", "height": "12",
      "value": "80"}},
    {"name": "vy_toggle", "attrs": {"x": "16", "y": "266", "width": "56", "height": "28",
      "value": "1"}},
    {"name": "vy_label", "attrs": {"x": "84", "y": "272", "width": "120", "height": "18",
      "text": "bypass", "color": "#D8DEE9"}},
    {"name": "vy_line", "attrs": {"x": "8", "y": "300", "width": "224", "height": "2",
      "background": "#4C566A"}},
    {"name": "vy_label", "attrs": {"x": "10", "y": "304", "width": "220", "height": "14",
      "text": "awto / vyr 240x320 ILI9341", "color": "#7A869A"}}
  ]}
}"##;

/// One (mode, tier, rect-set) measurement.
struct Row {
    mode: &'static str,
    tier: &'static str,
    rect_name: &'static str,
    cost: Cost,
    hash: u64,
    px: u64,
    /// Screen pixels covered by the rect set — the write volume denominator.
    area_px: u64,
}

/// Bytes the mode moves ACROSS THE FMC BUS for `area_px` pixels: banded writes
/// 2 B/px (RGB565), direct writes 3 B/px (RGB888).
fn sdram_bytes(mode: &str, area_px: u64) -> u64 {
    if mode == "direct" {
        area_px * 3
    } else {
        area_px * 2
    }
}

fn emit_row(emit: &mut dyn FnMut(&str), r: &Row, hz: u32) {
    let total = r.cost.render_cycles.wrapping_add(r.cost.blit_cycles);
    let sd = sdram_bytes(r.mode, r.area_px);
    // For banded, the SDRAM traffic all happens in the blit window, so its
    // achieved bandwidth is exact. For direct there is no separate window --
    // the stores are interleaved with rasterization -- so the figure is the
    // bandwidth achieved ACROSS THE WHOLE RENDER, which is the number that
    // matters to a frame budget even though it is not a peak.
    let win = if r.mode == "direct" {
        r.cost.render_cycles
    } else {
        r.cost.blit_cycles
    };
    emit(&alloc::format!(
        "INFO  [vyr-size] present: mode={} tier={} rect={} area_px={} render={} c \
         blit={} c total={} c px_written={} hash={:#018x} sdram_bytes={} \
         sdram_kbps={} ns_per_px={}",
        r.mode,
        r.tier,
        r.rect_name,
        r.area_px,
        r.cost.render_cycles,
        r.cost.blit_cycles,
        total,
        r.px,
        r.hash,
        sd,
        kbps(sd, win, hz),
        if r.area_px == 0 {
            0
        } else {
            (total as u64 * 1_000_000_000) / (r.area_px * hz as u64)
        }
    ));
}

/// Measure one tier over one rect set in BOTH modes and report.
#[allow(clippy::too_many_arguments)]
fn measure_pair(
    emit: &mut dyn FnMut(&str),
    req: &Request,
    fonts: &mut Fonts,
    assets: &Assets,
    band_buf: &mut [u8],
    rects: &[Rect],
    rect_name: &'static str,
    quality: Quality,
    tier: &'static str,
    reps: u32,
    hz: u32,
) -> Result<bool, RenderError> {
    let area_px: u64 = rects.iter().map(|r| r.w as u64 * r.h as u64).sum();

    // --- banded + flush (the control) --------------------------------------
    let mut bc = Cost {
        render_cycles: 0,
        blit_cycles: 0,
    };
    let mut bpx = 0u64;
    let mut bhash = lcd::FNV_OFFSET;
    for _ in 0..reps {
        bhash = lcd::FNV_OFFSET;
        for r in rects {
            // Chain the fold across the rect set so a multi-rect update has ONE
            // hash, exactly as a single-rect one does.
            bhash = ltdc::render_rect_from(
                req, fonts, assets, quality, band_buf, *r, &mut bc, &mut bpx, bhash,
            )?;
        }
    }
    bc.render_cycles /= reps;
    bc.blit_cycles /= reps;
    let banded = Row {
        mode: "banded",
        tier,
        rect_name,
        cost: bc,
        hash: bhash,
        px: bpx / reps as u64,
        area_px,
    };

    // --- direct into SDRAM --------------------------------------------------
    let mut dc = Cost {
        render_cycles: 0,
        blit_cycles: 0,
    };
    let mut dpx = 0u64;
    for _ in 0..reps {
        for r in rects {
            render_direct(req, fonts, assets, quality, *r, &mut dc, &mut dpx)?;
        }
    }
    dc.render_cycles /= reps;
    // Hash the framebuffer back OUT of SDRAM -- outside every window, and in
    // the same rect order the banded fold used.
    let mut dhash = lcd::FNV_OFFSET;
    for r in rects {
        dhash = fb888_rect_hash_from(dhash, *r);
    }
    let direct = Row {
        mode: "direct",
        tier,
        rect_name,
        cost: dc,
        hash: dhash,
        px: dpx / reps as u64,
        area_px,
    };

    emit_row(emit, &banded, hz);
    emit_row(emit, &direct, hz);

    let equal = banded.hash == direct.hash;
    let bt = banded
        .cost
        .render_cycles
        .wrapping_add(banded.cost.blit_cycles);
    let dt = direct.cost.render_cycles;
    emit(&alloc::format!(
        "INFO  [vyr-size] present: verdict tier={tier} rect={rect_name} \
         banded_total={bt} c direct_total={dt} c winner={} margin_pct={} \
         pixels_equal={equal} banded_hash={:#018x} direct_hash={:#018x}",
        if dt < bt { "direct" } else { "banded" },
        if bt == 0 {
            0
        } else {
            ((bt as i64 - dt as i64) * 100) / bt as i64
        },
        banded.hash,
        direct.hash
    ));
    Ok(equal)
}

/// Everything above, for every tier and both rect shapes, plus the memory-system
/// probes the verdict needs to be explained rather than merely stated.
pub fn compare(
    emit: &mut dyn FnMut(&str),
    band_buf: &mut [u8],
    req: &Request,
    fonts: &mut Fonts,
    assets: &Assets,
    hz: u32,
) -> Result<(), RenderError> {
    emit(&alloc::format!(
        "INFO  [vyr-size] present: RGB888 framebuffer at {FB888_BASE:#010x} \
         ({FB888_BYTES} B, 1.5x the RGB565 frame's {}) — L1PFCR=1, line {} B; \
         RGB565 frame stays resident at {FB_BASE:#010x}",
        PANEL_W * PANEL_H * 2,
        PANEL_W * 3
    ));

    let full = Rect {
        x: 0,
        y: 0,
        w: PANEL_W,
        h: PANEL_H,
    };

    // --- memory system, before any of the render comparison -----------------
    probes(emit, band_buf, hz);

    // --- the dirty rects a real update produces -----------------------------
    let next = Request::parse(crate::opaque(PANEL_IR_NEXT))?;
    let t0 = cyc();
    let drects: Vec<Rect> = dirty_rects(req, &next);
    let diff_cycles = cyc().wrapping_sub(t0);
    let drect_px: u64 = drects.iter().map(|r| r.w as u64 * r.h as u64).sum();
    emit(&alloc::format!(
        "INFO  [vyr-size] present: dirty_rects(prev,next) n={} px={} ({}% of screen) \
         diff={} c rects={}",
        drects.len(),
        drect_px,
        drect_px * 100 / (PANEL_W as u64 * PANEL_H as u64),
        diff_cycles,
        {
            let mut s = alloc::string::String::new();
            for r in &drects {
                s.push_str(&alloc::format!("({},{} {}x{}) ", r.x, r.y, r.w, r.h));
            }
            s
        }
    ));

    // --- the comparison ------------------------------------------------------
    // The scene rendered into the dirty rects is `next`, not `prev`: that is
    // what an incremental update actually paints. Both modes get the same one.
    let mut all_equal = true;
    for (q, name) in TIERS {
        isr_clear();
        all_equal &= measure_pair(
            emit,
            req,
            fonts,
            assets,
            band_buf,
            &[full],
            "full",
            q,
            name,
            REPS_FULL,
            hz,
        )?;
        let isr = isr_read();
        emit(&alloc::format!(
            "INFO  [vyr-size] present: ltdc_isr tier={name} rect=full raw={isr:#010x} \
             fifo_underrun={} transfer_error={}",
            (isr & ISR_FUIF) != 0,
            (isr & ISR_TERRIF) != 0
        ));

        isr_clear();
        all_equal &= measure_pair(
            emit,
            req,
            fonts,
            assets,
            band_buf,
            &[ltdc::DIRTY],
            "dirty1",
            q,
            name,
            REPS_DIRTY,
            hz,
        )?;
        if !drects.is_empty() {
            all_equal &= measure_pair(
                emit, &next, fonts, assets, band_buf, &drects, "dirtyN", q, name, REPS_DIRTY, hz,
            )?;
        }
        let isr = isr_read();
        emit(&alloc::format!(
            "INFO  [vyr-size] present: ltdc_isr tier={name} rect=dirty raw={isr:#010x} \
             fifo_underrun={} transfer_error={}",
            (isr & ISR_FUIF) != 0,
            (isr & ISR_TERRIF) != 0
        ));
    }
    emit(&alloc::format!(
        "INFO  [vyr-size] present: pixels_identical_across_modes={all_equal}"
    ));

    // --- leave the panel showing the DIRECT frame ----------------------------
    // `--features testcard` swaps the final picture for the LABELLED COLOUR
    // TEST CARD and, deliberately, applies NO R/B correction — see
    // [`testcard_direct`]. It is the whole experiment, so it replaces this tail
    // rather than running after it: two full-frame direct renders would leave
    // the panel showing whichever happened to be last. The call itself is made
    // by [`crate::ltdc::show_scene`], which OWNS the scene's `Request` and both
    // registries and can therefore free them first — this function only borrows
    // them, and on a 120 KiB arena that difference is the difference between a
    // rendered card and an allocation panic.
    #[cfg(feature = "testcard")]
    return Ok(());

    // Re-render the base scene at the workload tier so what is on the glass is
    // the scene every other leg reports, not the dirty-rect variant left over
    // from the loop above.
    #[cfg(not(feature = "testcard"))]
    {
        let mut c = Cost {
            render_cycles: 0,
            blit_cycles: 0,
        };
        let mut px = 0u64;
        render_direct(
            req,
            fonts,
            assets,
            crate::workload::WORKLOAD_QUALITY,
            full,
            &mut c,
            &mut px,
        )?;
        let pre = fb888_rect_hash(full);
        let want = fb888_rect_hash_swapped(full);
        if RB_SWAP {
            let swap_cycles = swap_rb_in_place();
            let post = fb888_rect_hash(full);
            emit(&alloc::format!(
                "INFO  [vyr-size] present: rb_swap cycles={swap_cycles} bytes={FB888_BYTES} \
             kbps={} pre={pre:#018x} post={post:#018x} want={want:#018x} correct={}",
                kbps(FB888_BYTES as u64, swap_cycles, hz),
                post == want
            ));
        } else {
            // `VYR_RB_SWAP=0`: the priced pass is NOT run, so what LTDC scans
            // is exactly the bytes vyr-core wrote, R first. Reported as its own
            // line rather than by omission — a missing line reads as a broken
            // build, and this is a deliberate configuration.
            emit(&alloc::format!(
                "INFO  [vyr-size] present: rb_swap SKIPPED (VYR_RB_SWAP=0) cycles=0 \
             bytes={FB888_BYTES} pre={pre:#018x} post={pre:#018x} \
             would_have_become={want:#018x} correct=n/a — no software channel \
             correction is active on this frame"
            ));
        }
        isr_clear();
        fb_select(Fmt::Rgb888);
        // Let the controller scan a few whole frames in the new format before its
        // error flags are believed. 100 ms is ~6.5 frames at the measured 65.33 Hz
        // and is a busy-wait, never a shell sleep.
        lcd::delay_ms(100);
        let isr = isr_read();
        let (cdsr, cpsr) = ltdc::ltdc_status();
        emit(&alloc::format!(
            "INFO  [vyr-size] present: now scanning RGB888 fb={FB888_BASE:#010x} \
         L1PFCR={:#x} L1CFBAR={:#010x} L1CFBLR={:#010x} L1CR={:#010x} \
         CDSR={cdsr:#010x} CPSR={cpsr:#010x} isr={isr:#010x} fifo_underrun={} \
         scanout_read_bytes_per_s={}",
            rd(L1_PFCR),
            rd(L1_CFBAR),
            rd(L1_CFBLR),
            rd(L1_CR),
            (isr & ISR_FUIF) != 0,
            FB888_BYTES as u64 * 6533 / 100
        ));
        emit(&alloc::format!(
            "ALERT [vyr-size] present: the panel is now showing the DIRECT-rendered \
         frame — vyr-core wrote RGB888 straight into SDRAM with no blit. \
         CORRECTIONS ACTIVE: MADCTL={:#04x} ({}), software R/B swap={}. It should \
         look IDENTICAL to the banded RGB565 frame the ltdc leg leaves. Swapped \
         red/blue means the channel order is still wrong; a sheared or noisy \
         image means the RGB888 layer geometry is wrong; a correct image is the \
         only confirmation a machine cannot give.",
            lcd::MADCTL,
            lcd::madctl_flags(),
            if RB_SWAP { "APPLIED" } else { "NOT applied" }
        ));
        Ok(())
    }
}

/// `--features testcard`: leave the panel showing the card, rendered DIRECT
/// into the RGB888 framebuffer, with the software R/B swap applied only if
/// [`RB_SWAP`] says so (colour card: off by default; orientation card: on by
/// default; `VYR_RB_SWAP` overrides both, and the MADCTL sweep turns it off).
///
/// This is the experiment the swap pass was never subjected to. Everything
/// upstream of the glass is machine-checked already — both modes produce
/// byte-identical RGB888, the framebuffer reads back what was written, LTDC
/// reports no underrun — and NONE of it can say which byte the RGB888 layer
/// treats as red. Only light coming out of the panel can, and only if the
/// picture says what it is supposed to be.
///
/// So the swap is deliberately NOT applied: what LTDC scans is exactly the
/// bytes `vyr-core` wrote, R first. Then
///
/// * the block labelled `RED` appearing **blue** ⇒ the layer reads B at the
///   lowest address, RM0090 is right, and the correcting pass is REQUIRED;
/// * the block labelled `RED` appearing **red** ⇒ the layer reads R first, the
///   swap is unnecessary, and direct rendering wins outright.
///
/// The `lcd` and `ltdc` builds of the same card are the controls: both convert
/// to RGB565 in vyr's own software and exercise no RGB888 layer format, so if
/// the labels are wrong THERE the fault is upstream of this question entirely.
///
/// The raw framebuffer bytes under three swatch centres are logged as well —
/// machine proof of what the renderer wrote, against which the photograph of
/// what the panel displayed can be compared.
#[cfg(feature = "testcard")]
pub(crate) fn testcard_direct(
    emit: &mut dyn FnMut(&str),
    fonts: &mut Fonts,
    assets: &Assets,
) -> Result<(), RenderError> {
    use crate::testcard;

    let quality = crate::workload::WORKLOAD_QUALITY;
    let card = Request::parse(crate::opaque(testcard::CARD_IR))?;
    let mut c = Cost {
        render_cycles: 0,
        blit_cycles: 0,
    };
    let mut px = 0u64;
    render_direct(
        &card,
        fonts,
        assets,
        quality,
        testcard::CARD_RECT,
        &mut c,
        &mut px,
    )?;
    // Hash READ BACK OUT of SDRAM, not folded on the way in: this is the
    // number compared against the SPI leg, the banded leg, the host and the
    // emulated M4, and reading it back also proves the memory holds it.
    let hash = fb888_rect_hash(testcard::CARD_RECT);
    // Taken BEFORE any correcting pass, which is what makes it comparable with
    // the SPI leg, the banded leg, the host and the emulated M4: every one of
    // those folds the bytes `vyr-core` emitted, R first. A hash taken after the
    // swap would be a hash of different bytes and would prove nothing about
    // whether the same IR produced the same pixels.
    testcard::report(
        emit,
        if RB_SWAP {
            "present-direct-rgb888-RBSWAP"
        } else {
            "present-direct-rgb888-NOSWAP"
        },
        hash,
        quality,
    );

    // The card's own parsed tree is ~45 KiB of the 120 KiB arena and is finished
    // with the moment its pixels are in SDRAM. Dropping it before the identity
    // strip is parsed is not tidiness: MEASURED on this board, holding it cost
    // the strip's 32,768 B band pixmap its allocation
    // (`memory allocation of 32768 bytes failed`) even though live+wanted was
    // under the arena, because the free space was no longer contiguous. The legs
    // that go through `testcard::render_card` get this for free — the request is
    // that function's local — and only the direct path, which owns its request
    // across two renders, has to say it out loud.
    drop(card);

    // Line 3 is the whole configuration, printed on the glass, so the
    // photograph is self-describing: the MADCTL actually written and whether
    // the software R/B pass ran. Both swap red and blue; both active cancel,
    // and a card that did not say so could be read as "correct" when it is
    // merely doubly wrong.
    let ident = Request::parse(&testcard::identity_ir(
        "PATH present: DIRECT render, no blit",
        "FMT RGB888 fb 0xD0040000 L1PFCR=1",
        &alloc::format!(
            "MADCTL {:#04x} {} / RB sw {}",
            lcd::MADCTL,
            lcd::madctl_flags(),
            if RB_SWAP { "SWAPPED" } else { "none" }
        ),
    ))?;
    render_direct(
        &ident,
        fonts,
        assets,
        quality,
        testcard::IDENT_RECT,
        &mut c,
        &mut px,
    )?;

    // What is actually in memory under three known-colour points, in ADDRESS
    // order — machine proof of what the renderer wrote, against which a
    // photograph of what the panel displayed can be compared.
    let probe = |x: u32, y: u32| -> (u8, u8, u8) {
        let off = ((y * testcard::PANEL_W + x) * 3) as usize;
        // SAFETY: inside the RGB888 framebuffer; read-only.
        let s = unsafe { core::slice::from_raw_parts((FB888_BASE as usize + off) as *const u8, 3) };
        (s[0], s[1], s[2])
    };
    // Colour card: the RED/GREEN/BLUE swatch centres. Geometry from
    // scripts/make-testcard.py — swatches start at y = 24 with a 23 px pitch,
    // 21 px tall, spanning x = 70..236.
    #[cfg(not(feature = "orientcard"))]
    let (names, pts) = (
        ["RED(153,34)", "GREEN(153,57)", "BLUE(153,80)"],
        [(153u32, 34u32), (153, 57), (153, 80)],
    );
    // Orientation card: the origin block, the +X wedge and the +Y wedge.
    // Geometry from scripts/make-orientcard.py — cyan block at (0,0)..(24,12),
    // yellow wedge along the top edge, magenta wedge down the left.
    #[cfg(feature = "orientcard")]
    let (names, pts) = (
        ["ORIGIN(2,8)", "XWEDGE(200,4)", "YWEDGE(2,240)"],
        [(2u32, 8u32), (200, 4), (2, 240)],
    );
    let mut probes = alloc::string::String::new();
    for (n, (x, y)) in names.iter().zip(pts.iter()) {
        let (r, g, b) = probe(*x, *y);
        probes.push_str(&alloc::format!("{n}=[{r:02x} {g:02x} {b:02x}] "));
    }
    emit(&alloc::format!(
        "INFO  [vyr-size] testcard: framebuffer bytes BEFORE any correction, in \
         ADDRESS order — {probes}. vyr-core writes R,G,B, so these ARE the \
         renderer's bytes."
    ));

    // --- the software R/B correction, if this build is paying for it ---------
    // The colour card exists to ASK whether LTDC's RGB888 layer is B-first, so
    // by default it is displayed raw; the orientation card, which asks
    // something else entirely, by default pays the correcting pass so its
    // colours cannot muddle the reading. `VYR_RB_SWAP` overrides either, and
    // the MADCTL sweep turns it OFF on both — MADCTL's BGR bit swaps the same
    // two channels, and two swaps that cancel look exactly like none.
    //
    // Reusing [`swap_rb_in_place`] rather than inventing a second mechanism is
    // the point: one priced, tested pass, and the post-swap hash is CHECKED
    // against the expectation computed from the pre-swap contents, so "the swap
    // ran" becomes "the swap is correct".
    {
        let full = Rect {
            x: 0,
            y: 0,
            w: PANEL_W,
            h: PANEL_H,
        };
        let pre = fb888_rect_hash(full);
        let want = fb888_rect_hash_swapped(full);
        if RB_SWAP {
            let cycles = swap_rb_in_place();
            let post = fb888_rect_hash(full);
            emit(&alloc::format!(
                "INFO  [vyr-size] testcard: rb_swap APPLIED cycles={cycles} \
                 bytes={FB888_BYTES} pre={pre:#018x} post={post:#018x} \
                 want={want:#018x} correct={} (the card body hash reported above is \
                 the PRE-swap fold, which is what the other paths and both ISAs \
                 fold too)",
                post == want
            ));
        } else {
            emit(&alloc::format!(
                "INFO  [vyr-size] testcard: rb_swap NOT applied cycles=0 \
                 bytes={FB888_BYTES} pre={pre:#018x} post={pre:#018x} \
                 would_have_become={want:#018x} — LTDC is scanning exactly the \
                 bytes vyr-core wrote, R first, so any channel order visible on \
                 the glass is MADCTL's and the layer format's alone"
            ));
        }
    }

    isr_clear();
    fb_select(Fmt::Rgb888);
    // Let the controller scan a few whole frames in the new format before its
    // error flags are believed. 100 ms is ~6.5 frames at the measured 65.33 Hz
    // and is a busy-wait, never a shell sleep.
    lcd::delay_ms(100);
    let isr = isr_read();
    let (cdsr, cpsr) = ltdc::ltdc_status();
    emit(&alloc::format!(
        "INFO  [vyr-size] present: now scanning RGB888 fb={FB888_BASE:#010x} \
         L1PFCR={:#x} L1CFBAR={:#010x} L1CFBLR={:#010x} L1CR={:#010x} \
         CDSR={cdsr:#010x} CPSR={cpsr:#010x} isr={isr:#010x} fifo_underrun={} \
         scanout_read_bytes_per_s={}",
        rd(L1_PFCR),
        rd(L1_CFBAR),
        rd(L1_CFBLR),
        rd(L1_CR),
        (isr & ISR_FUIF) != 0,
        FB888_BYTES as u64 * 6533 / 100
    ));
    // ONE alert, and it names every correction that was live. Two swaps that
    // cancel produce a correct-looking panel from a wrong configuration, so the
    // configuration is stated rather than implied — on the glass (the identity
    // strip) and in the log (here).
    emit(&alloc::format!(
        "ALERT [vyr-size] testcard: the panel is showing the {}, rendered DIRECT \
         into the RGB888 framebuffer. CORRECTIONS ACTIVE: MADCTL={:#04x} ({}), \
         software R/B swap={}. {}",
        testcard::CARD_NAME,
        lcd::MADCTL,
        lcd::madctl_flags(),
        if RB_SWAP { "APPLIED" } else { "NOT applied" },
        testcard::READ_ME
    ));
    Ok(())
}

/// The memory-system probes, run in one place so their conditions are stated
/// once: same 8 KiB burst, same loop, same SDRAM region, differing only in what
/// LTDC is doing to the bus at the time.
fn probes(emit: &mut dyn FnMut(&str), band_buf: &mut [u8], hz: u32) {
    let ccm = band_buf.as_mut_ptr() as u32;
    let ccm_c = write_burst(ccm, BURST_BYTES);
    let (ccm_r, _) = read_burst(ccm, BURST_BYTES);

    // SDRAM with LTDC scanning RGB565 -- today's steady state.
    fb_select(Fmt::Rgb565);
    let s565_w = write_burst(PROBE_BASE, BURST_BYTES);
    let (s565_r, _) = read_burst(PROBE_BASE, BURST_BYTES);

    // SDRAM with LTDC scanning RGB888 -- 1.5x the scan-out read traffic.
    fb_select(Fmt::Rgb888);
    let s888_w = write_burst(PROBE_BASE, BURST_BYTES);
    let (s888_r, _) = read_burst(PROBE_BASE, BURST_BYTES);

    // SDRAM with the layer OFF -- no framebuffer fetches at all. The control.
    layer_enable(false);
    let soff_w = write_burst(PROBE_BASE, BURST_BYTES);
    let (soff_r, _) = read_burst(PROBE_BASE, BURST_BYTES);
    layer_enable(true);
    fb_select(Fmt::Rgb565);

    emit(&alloc::format!(
        "INFO  [vyr-size] present: bw burst={BURST_BYTES} B \
         ccm_w={ccm_c} c ({} MB/s) ccm_r={ccm_r} c ({} MB/s) \
         sdram_w_rgb565scan={s565_w} c ({} MB/s) sdram_r_rgb565scan={s565_r} c ({} MB/s) \
         sdram_w_rgb888scan={s888_w} c ({} MB/s) sdram_r_rgb888scan={s888_r} c ({} MB/s) \
         sdram_w_layeroff={soff_w} c ({} MB/s) sdram_r_layeroff={soff_r} c ({} MB/s)",
        mbps(BURST_BYTES as u64, ccm_c, hz),
        mbps(BURST_BYTES as u64, ccm_r, hz),
        mbps(BURST_BYTES as u64, s565_w, hz),
        mbps(BURST_BYTES as u64, s565_r, hz),
        mbps(BURST_BYTES as u64, s888_w, hz),
        mbps(BURST_BYTES as u64, s888_r, hz),
        mbps(BURST_BYTES as u64, soff_w, hz),
        mbps(BURST_BYTES as u64, soff_r, hz),
    ));
    emit(&alloc::format!(
        "INFO  [vyr-size] present: contention ccm_vs_sdram={}x \
         scanout_cost_rgb565_pct={} scanout_cost_rgb888_pct={}",
        s565_w.checked_div(ccm_c).unwrap_or(0),
        if soff_w == 0 {
            0
        } else {
            ((s565_w as i64 - soff_w as i64) * 100) / soff_w as i64
        },
        if soff_w == 0 {
            0
        } else {
            ((s888_w as i64 - soff_w as i64) * 100) / soff_w as i64
        }
    ));

    // --- blanking versus active scan-out ------------------------------------
    // Same burst, started at two known scan positions. Lines 0..=3 are
    // VSYNC+VBP (blanking, no pixel fetches); line 160 is mid-active.
    const PHASE_REPS: u32 = 8;
    let mut blank = u32::MAX;
    let mut blank_sum = 0u32;
    let mut active = u32::MAX;
    let mut active_sum = 0u32;
    let mut phased = 0u32;
    for _ in 0..PHASE_REPS {
        if !wait_line(0, 1) {
            break;
        }
        let c = write_burst(PROBE_BASE, BURST_BYTES);
        blank = blank.min(c);
        blank_sum += c;
        if !wait_line(160, 168) {
            break;
        }
        let c = write_burst(PROBE_BASE, BURST_BYTES);
        active = active.min(c);
        active_sum += c;
        phased += 1;
    }
    if let (Some(bm), Some(am)) = (
        blank_sum.checked_div(phased),
        active_sum.checked_div(phased),
    ) {
        emit(&alloc::format!(
            "INFO  [vyr-size] present: phase reps={phased} burst={BURST_BYTES} B \
             blank_min={blank} c blank_mean={bm} c ({} MB/s) \
             active_min={active} c active_mean={am} c ({} MB/s) \
             active_penalty_pct={}",
            mbps(BURST_BYTES as u64, bm, hz),
            mbps(BURST_BYTES as u64, am, hz),
            if blank == 0 {
                0
            } else {
                ((active as i64 - blank as i64) * 100) / blank as i64
            }
        ));
    } else {
        emit(
            "ERROR [vyr-size] present: phase probe could not synchronise to \
             LTDC_CPSR — scan-out is not running, so blanking-vs-active is \
             unmeasurable",
        );
    }
}

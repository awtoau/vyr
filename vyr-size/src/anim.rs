//! `--features anim` — make the ILI9341 panel MOVE by pushing only the pixels
//! that changed (#28 follow-on; the counter-proposal to LTDC+SDRAM, #30).
//!
//! # The hypothesis under test
//!
//! A full-screen flush is 240·320·2 = 153,600 B at 5.625 MHz ≈ **218 ms**, so
//! [`crate::lcd`] is a static-frame path. But vyr does not have to repaint the
//! screen to change the screen:
//!
//! * [`vyr_core::dirty_rects`] diffs two IR `Request`s and returns exactly the
//!   screen regions whose pixels may differ (a superset, conservatively
//!   subtree-granular — see its module docs);
//! * `render(tree, area, buf, stride)` renders an ARBITRARY sub-rect (invariant
//!   I1 — dirty rects are its arbitrary-rectangle consumer, x-offsets and
//!   vertical seams included);
//! * the ILI9341 takes `CASET`/`PASET` for an arbitrary GRAM window and RETAINS
//!   every pixel outside it, refreshing the glass from its own frame memory.
//!
//! So both the render and the wire cost SHOULD scale with dirty area rather
//! than screen area, and no framebuffer is needed anywhere on the host. That is
//! the claim; this module measures it instead of asserting it.
//!
//! # What is measured, and how the three costs are separated
//!
//! Per animated frame, four DWT_CYCCNT windows, never overlapping:
//!
//! | phase | what it covers |
//! |-------|----------------|
//! | `c_ir` | building the next frame's IR JSON + `Request::parse` — the vyvanse-feeds-IR half of the pipeline, priced separately because it is NOT rendering |
//! | `c_diff` | [`vyr_core::dirty_rects`] alone |
//! | `c_render` | Σ `render_with_quality` over the dirty sub-bands |
//! | `c_flush` | Σ `CASET`/`PASET` + the RGB565 pixel stream out of SPI5 |
//!
//! `clock_cycles()` costs a handful of cycles per read (two volatile debug-block
//! loads); at four reads per sub-band against millions of cycles of work it is
//! below the resolution of anything reported here, and it is charged to whatever
//! window it sits inside rather than netted out.
//!
//! # Correctness: the determinism proof must survive animation
//!
//! An incremental repaint that is *nearly* right is worse than a slow one. The
//! host proves byte-exactness in `vyr-core/tests/dirty_rects.rs`; this module
//! re-proves it ON THE BOARD, in band memory, with no framebuffer:
//!
//! * `full_hash(next)` — `next` rendered as full-width 240×16 bands, FNV-1a
//!   folded.
//! * `composite_hash(prev, next)` — for each band: render `prev`, then re-render
//!   `next` into each (dirty rect ∩ band) sub-region of the SAME band buffer,
//!   then fold. That is exactly the composite the panel's GRAM ends up holding.
//!
//! Equal hashes ⇒ the incremental update is byte-identical to a full render.
//! What this CANNOT prove is that the controller's GRAM really retained the
//! untouched pixels — that is a hardware property, visible to a human and to
//! nothing else on this vehicle.
//!
//! # A deliberate omission
//!
//! `vy_gauge`'s `value` is not painted today (the widget draws its ring; sweep
//! arcs are F4, unbuilt). Animating it would dirty 100×100 px for zero visible
//! change — a flattering benchmark and a dishonest one. The gauge is therefore
//! STATIC here, and motion comes from widgets whose value the painter actually
//! reads: a `vy_chart` scope trace, `vy_slider`/`vy_progress`/`vy_toggle`
//! values, and a `vy_lcd` numeric readout.

use alloc::format;
use alloc::string::String;
use core::fmt::Write;

use vyr_core::ir::Request;
use vyr_core::{Assets, Fonts, Quality, Rect, RenderError, dirty_rects};

use crate::lcd::{FNV_OFFSET, PANEL_BAND_H, PANEL_H, PANEL_W, flush_rect, fnv1a_fold};

/// Timed frames per (quality, mode) cell. Small on purpose: one `full` frame at
/// `Quality::Exact` is ~0.4 s of render plus 0.22 s of wire, so a big N buys
/// precision nobody needs and spends board time everyone does. Six frames put
/// the per-frame numbers well clear of the ~µs DWT read overhead.
const FRAMES: u32 = 6;

/// Screen pixels — the denominator of every dirty-area percentage.
const SCREEN_PX: u64 = (PANEL_W as u64) * (PANEL_H as u64);

#[inline]
fn cyc() -> u32 {
    crate::m4::clock_cycles() as u32
}

/// What one (quality, mode) cell cost, summed over its timed frames.
#[derive(Default, Clone, Copy)]
struct Costs {
    ir: u64,
    diff: u64,
    render: u64,
    flush: u64,
    dirty_px: u64,
    rects: u64,
    subbands: u64,
}

impl Costs {
    fn add(&mut self, o: Costs) {
        self.ir += o.ir;
        self.diff += o.diff;
        self.render += o.render;
        self.flush += o.flush;
        self.dirty_px += o.dirty_px;
        self.rects += o.rects;
        self.subbands += o.subbands;
    }
}

// --- the animated scene ------------------------------------------------------

/// Which part of the scene moves. Four points on the dirty-fraction curve,
/// chosen so the reported cost/fraction relationship is sampled rather than
/// asserted from one measurement.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Mode {
    /// One numeric readout re-renders. The sparsest useful update.
    Readout,
    /// A scope trace sweeps. One mid-size widget.
    Trace,
    /// Readout + trace + toggle + both sliders + the progress bar.
    Panel,
    /// The root backdrop changes — everything is dirty by construction. The
    /// control case: an incremental update that degenerates to a full frame.
    Full,
}

impl Mode {
    fn name(self) -> &'static str {
        match self {
            Mode::Readout => "readout",
            Mode::Trace => "trace",
            Mode::Panel => "panel",
            Mode::Full => "full",
        }
    }
}

const MODES: [Mode; 4] = [Mode::Readout, Mode::Trace, Mode::Panel, Mode::Full];

/// 50 + 45·sin(2πi/32), rounded — an integer sine so scene generation costs no
/// float math and is bit-identical on every run (determinism, invariant I8).
const SINE32: [u32; 32] = [
    50, 59, 67, 75, 82, 87, 92, 94, 95, 94, 92, 87, 82, 75, 67, 59, 50, 41, 33, 25, 18, 13, 8, 6,
    5, 6, 8, 13, 18, 25, 33, 41,
];

/// Backdrops for [`Mode::Full`] — visibly different from each other, so a human
/// watching the board can tell the dense case ran at all.
const BACKDROPS: [&str; 4] = ["#22262B", "#3B2226", "#223B2B", "#2B3B22"];

/// 48 chart samples, phase-advanced by `f`: two summed harmonics so the trace
/// has structure rather than a single travelling sine.
fn wave(f: u32) -> String {
    let mut s = String::with_capacity(200);
    for j in 0..48u32 {
        let a = SINE32[((j + f * 2) % 32) as usize];
        let b = SINE32[((j * 3 + f) % 32) as usize];
        let v = (2 * a + b) / 3;
        if j > 0 {
            s.push(',');
        }
        // Infallible: writing into a String.
        let _ = write!(s, "{v}");
    }
    s
}

/// The animated values of one frame. Frame 0 of every mode is the SAME scene,
/// so the modes differ only in what moves — not in what is on screen.
struct Vals {
    bg: &'static str,
    rpm: u32,
    on: u32,
    s1: u32,
    s2: u32,
    prog: u32,
    trace: String,
}

fn vals(mode: Mode, f: u32) -> Vals {
    let base = Vals {
        bg: BACKDROPS[0],
        rpm: 1480,
        on: 1,
        s1: 62,
        s2: 35,
        prog: 80,
        trace: wave(0),
    };
    match mode {
        Mode::Readout => Vals {
            rpm: 1000 + (f * 137) % 900,
            ..base
        },
        Mode::Trace => Vals {
            trace: wave(f),
            ..base
        },
        Mode::Panel => Vals {
            rpm: 1000 + (f * 137) % 900,
            on: f % 2,
            s1: 5 + (f * 17) % 90,
            s2: 5 + (f * 29) % 90,
            prog: 5 + (f * 11) % 90,
            trace: wave(f),
            ..base
        },
        Mode::Full => Vals {
            bg: BACKDROPS[(f % 4) as usize],
            ..base
        },
    }
}

/// The 240×320 animated fixture, as IR JSON — the SAME vocabulary as
/// [`crate::lcd::PANEL_IR`] (frame, label, image, gauge, lcd, toggle, chart,
/// two sliders, progress, line), re-laid-out so the moving widgets do not
/// overlap and their dirty boxes stay separable.
///
/// Built as a string and re-parsed every frame ON PURPOSE: that is the shape of
/// the real contract (vyvanse hands vyr IR JSON, `schema_version` and all), and
/// pricing it separately from rendering is the only way to say honestly how much
/// of a frame budget the ingest half eats.
fn ir(v: &Vals) -> String {
    format!(
        "{{\"schema_version\":\"0.6-vyvanse\",\"w\":240,\"h\":320,\
         \"root\":{{\"name\":\"view\",\"attrs\":{{\"background\":\"{bg}\"}},\"children\":[\
         {{\"name\":\"vy_frame\",\"attrs\":{{\"x\":\"8\",\"y\":\"8\",\"width\":\"224\",\"height\":\"40\",\
         \"background\":\"#2E3440\",\"radius\":\"8\",\"border_width\":\"1\",\"border_color\":\"#4C566A\"}}}},\
         {{\"name\":\"vy_label\",\"attrs\":{{\"x\":\"20\",\"y\":\"20\",\"width\":\"170\",\"height\":\"20\",\
         \"text\":\"vyr dirty-rect anim\",\"color\":\"#ECEFF4\"}}}},\
         {{\"name\":\"vy_image\",\"attrs\":{{\"x\":\"200\",\"y\":\"16\",\"width\":\"24\",\"height\":\"24\",\
         \"src\":\"checker-24.png\"}}}},\
         {{\"name\":\"vy_gauge\",\"attrs\":{{\"x\":\"16\",\"y\":\"56\",\"width\":\"100\",\"height\":\"100\",\
         \"color\":\"#88C0D0\"}}}},\
         {{\"name\":\"vy_lcd\",\"attrs\":{{\"x\":\"126\",\"y\":\"66\",\"width\":\"104\",\"height\":\"28\",\
         \"text\":\"{rpm}\",\"color\":\"#A3BE8C\",\"style_text_font\":\"roboto_20\"}}}},\
         {{\"name\":\"vy_label\",\"attrs\":{{\"x\":\"128\",\"y\":\"98\",\"width\":\"100\",\"height\":\"16\",\
         \"text\":\"rpm\",\"color\":\"#7A869A\"}}}},\
         {{\"name\":\"vy_toggle\",\"attrs\":{{\"x\":\"126\",\"y\":\"120\",\"width\":\"56\",\"height\":\"28\",\
         \"value\":\"{on}\"}}}},\
         {{\"name\":\"vy_label\",\"attrs\":{{\"x\":\"188\",\"y\":\"126\",\"width\":\"46\",\"height\":\"18\",\
         \"text\":\"run\",\"color\":\"#D8DEE9\"}}}},\
         {{\"name\":\"vy_chart\",\"attrs\":{{\"x\":\"16\",\"y\":\"164\",\"width\":\"208\",\"height\":\"64\",\
         \"mode\":\"scope\",\"color\":\"#88C0D0\",\"points\":\"{trace}\"}}}},\
         {{\"name\":\"vy_slider\",\"attrs\":{{\"x\":\"16\",\"y\":\"238\",\"width\":\"208\",\"height\":\"18\",\
         \"value\":\"{s1}\"}}}},\
         {{\"name\":\"vy_slider\",\"attrs\":{{\"x\":\"16\",\"y\":\"262\",\"width\":\"208\",\"height\":\"18\",\
         \"value\":\"{s2}\"}}}},\
         {{\"name\":\"vy_progress\",\"attrs\":{{\"x\":\"16\",\"y\":\"286\",\"width\":\"208\",\"height\":\"12\",\
         \"value\":\"{prog}\"}}}},\
         {{\"name\":\"vy_line\",\"attrs\":{{\"x\":\"8\",\"y\":\"302\",\"width\":\"224\",\"height\":\"2\",\
         \"background\":\"#4C566A\"}}}},\
         {{\"name\":\"vy_label\",\"attrs\":{{\"x\":\"10\",\"y\":\"304\",\"width\":\"220\",\"height\":\"14\",\
         \"text\":\"awto / vyr SPI dirty-rect\",\"color\":\"#7A869A\"}}}}\
         ]}}}}",
        bg = v.bg,
        rpm = v.rpm,
        on = v.on,
        s1 = v.s1,
        s2 = v.s2,
        prog = v.prog,
        trace = v.trace,
    )
}

// --- the incremental update path --------------------------------------------

/// Repaint ONLY what changed between `prev` and `next`, straight onto the glass.
///
/// Each merged dirty rect is walked in `PANEL_BAND_H`-row sub-bands (the band
/// buffer is the only pixel memory that exists — 240×16 RGB888 = 11,520 B of the
/// CCM buffer, and the Exact gutter pixmap for a 240×16 area is 32,768 B of
/// heap, the same working set the static panel path already proves fits). For
/// each sub-band: `render(area = sub-band)`, then `CASET`/`PASET` to exactly
/// that GRAM window and stream it. No framebuffer, host-side or otherwise — the
/// controller's own GRAM holds the frame.
fn update(
    prev: &Request,
    next: &Request,
    fonts: &mut Fonts,
    assets: &Assets,
    band_buf: &mut [u8],
    quality: Quality,
) -> Result<Costs, RenderError> {
    let mut c = Costs::default();

    let t0 = cyc();
    let rects = dirty_rects(prev, next);
    let t1 = cyc();
    c.diff = t1.wrapping_sub(t0) as u64;
    c.rects = rects.len() as u64;

    for r in &rects {
        c.dirty_px += r.w as u64 * r.h as u64;
        let stride = r.w as usize * 3;
        // Rows that fit the band buffer, capped at the panel band height so the
        // per-render pixmap never exceeds the one the static path validated.
        let rows = (band_buf.len() / stride).min(PANEL_BAND_H as usize) as u32;
        if rows == 0 {
            // Cannot happen at 240 px max width (720 B ≪ 11,520 B) — but a
            // silent zero-row loop would look like a working no-op, so say so.
            return Err(RenderError::BadIr(format!(
                "dirty rect {} px wide does not fit the {} B band buffer",
                r.w,
                band_buf.len()
            )));
        }
        let mut y = r.y;
        let bottom = r.y + r.h as i32;
        while y < bottom {
            let h = rows.min((bottom - y) as u32);
            let area = Rect {
                x: r.x,
                y,
                w: r.w,
                h,
            };
            let len = stride * h as usize;

            let ta = cyc();
            next.render_with_quality(fonts, assets, area, &mut band_buf[..len], stride, quality)?;
            let tb = cyc();
            flush_rect(r.x as u32, y as u32, r.w, h, &band_buf[..len], stride);
            let tc = cyc();

            c.render += tb.wrapping_sub(ta) as u64;
            c.flush += tc.wrapping_sub(tb) as u64;
            c.subbands += 1;
            y += h as i32;
        }
    }
    Ok(c)
}

/// Full banded render of `req` straight to the glass — the static path, reused
/// to establish each mode's frame 0 before the incremental loop starts. Not
/// timed: it exists so the incremental updates have a known-correct base.
fn flush_full(
    req: &Request,
    fonts: &mut Fonts,
    assets: &Assets,
    band_buf: &mut [u8],
    quality: Quality,
) -> Result<(), RenderError> {
    let stride = (PANEL_W * 3) as usize;
    let mut y = 0u32;
    while y < PANEL_H {
        let h = PANEL_BAND_H.min(PANEL_H - y);
        let area = Rect {
            x: 0,
            y: y as i32,
            w: PANEL_W,
            h,
        };
        let len = stride * h as usize;
        req.render_with_quality(fonts, assets, area, &mut band_buf[..len], stride, quality)?;
        flush_rect(0, y, PANEL_W, h, &band_buf[..len], stride);
        y += h;
    }
    Ok(())
}

// --- the on-device byte-exactness proof --------------------------------------

/// FNV-1a over a full banded render of `req`. The oracle the composite must
/// match.
fn full_hash(
    req: &Request,
    fonts: &mut Fonts,
    assets: &Assets,
    band_buf: &mut [u8],
    quality: Quality,
) -> Result<u64, RenderError> {
    let stride = (PANEL_W * 3) as usize;
    let mut hash = FNV_OFFSET;
    let mut y = 0u32;
    while y < PANEL_H {
        let h = PANEL_BAND_H.min(PANEL_H - y);
        let area = Rect {
            x: 0,
            y: y as i32,
            w: PANEL_W,
            h,
        };
        let len = stride * h as usize;
        req.render_with_quality(fonts, assets, area, &mut band_buf[..len], stride, quality)?;
        hash = fnv1a_fold(hash, core::hint::black_box(&band_buf[..len]));
        y += h;
    }
    Ok(hash)
}

/// FNV-1a over the frame an incremental update ACTUALLY leaves behind, built
/// band by band so no framebuffer is needed: render `prev` into the band, then
/// re-render `next` into each (dirty rect ∩ band) sub-region of that same
/// buffer — the arbitrary-sub-rect render at a non-zero x offset, which is the
/// part of `render(area, buf, stride)` a full-width band never exercises.
///
/// Must equal [`full_hash`] of `next`. Renders only; nothing is flushed, so the
/// glass keeps whatever the animation loop last put there.
fn composite_hash(
    prev: &Request,
    next: &Request,
    fonts: &mut Fonts,
    assets: &Assets,
    band_buf: &mut [u8],
    quality: Quality,
) -> Result<(u64, u64, u64), RenderError> {
    let rects = dirty_rects(prev, next);
    let dirty_px: u64 = rects.iter().map(|r| r.w as u64 * r.h as u64).sum();
    let stride = (PANEL_W * 3) as usize;
    let mut hash = FNV_OFFSET;
    let mut y = 0u32;
    while y < PANEL_H {
        let h = PANEL_BAND_H.min(PANEL_H - y);
        let band = Rect {
            x: 0,
            y: y as i32,
            w: PANEL_W,
            h,
        };
        let len = stride * h as usize;
        prev.render_with_quality(fonts, assets, band, &mut band_buf[..len], stride, quality)?;
        for r in &rects {
            let sub = r.intersect(band);
            if sub.is_empty() {
                continue;
            }
            let off = (sub.y - band.y) as usize * stride + sub.x as usize * 3;
            let end = off + stride * (sub.h as usize - 1) + sub.w as usize * 3;
            next.render_with_quality(fonts, assets, sub, &mut band_buf[off..end], stride, quality)?;
        }
        hash = fnv1a_fold(hash, core::hint::black_box(&band_buf[..len]));
        y += h;
    }
    Ok((hash, rects.len() as u64, dirty_px))
}

// --- the run -----------------------------------------------------------------

fn qname(q: Quality) -> &'static str {
    match q {
        Quality::Exact => "Exact",
        Quality::Draft => "Draft",
    }
}

fn measure_mode(
    emit: &mut dyn FnMut(&str),
    fonts: &mut Fonts,
    assets: &Assets,
    band_buf: &mut [u8],
    quality: Quality,
    mode: Mode,
) -> Result<(), RenderError> {
    // Frame 0 on the glass, full render: the incremental loop below is only
    // meaningful against a base the panel really holds.
    let mut prev = Request::parse(&ir(&vals(mode, 0)))?;
    flush_full(&prev, fonts, assets, band_buf, quality)?;

    // One untimed warm-up update: the glyph cache fills exactly once (F5), and
    // timing the rasterization of digits that every later frame reuses would
    // price a one-off as a per-frame cost.
    let warm = Request::parse(&ir(&vals(mode, 1)))?;
    update(&prev, &warm, fonts, assets, band_buf, quality)?;
    prev = warm;

    let mut total = Costs::default();
    for f in 2..(2 + FRAMES) {
        let t0 = cyc();
        let json = ir(&vals(mode, f));
        let next = Request::parse(&json)?;
        let t1 = cyc();
        let mut c = update(&prev, &next, fonts, assets, band_buf, quality)?;
        c.ir = t1.wrapping_sub(t0) as u64;
        total.add(c);
        prev = next;
    }

    emit(&format!(
        "INFO  [vyr-size] anim: q={} mode={} frames={FRAMES} rects={} subbands={} \
         dirty_px={} screen_px={SCREEN_PX} c_ir={} c_diff={} c_render={} c_flush={}",
        qname(quality),
        mode.name(),
        total.rects,
        total.subbands,
        total.dirty_px,
        total.ir,
        total.diff,
        total.render,
        total.flush,
    ));

    // The determinism proof, on this mode's own frame pair.
    let a = Request::parse(&ir(&vals(mode, 0)))?;
    let b = Request::parse(&ir(&vals(mode, 1)))?;
    let (comp, nrects, dpx) = composite_hash(&a, &b, fonts, assets, band_buf, quality)?;
    let full = full_hash(&b, fonts, assets, band_buf, quality)?;
    emit(&format!(
        "INFO  [vyr-size] anim verify: q={} mode={} rects={nrects} dirty_px={dpx} \
         full={full:#018x} composite={comp:#018x} match={}",
        qname(quality),
        mode.name(),
        full == comp,
    ));
    Ok(())
}

/// Frames in the continuous run. At the measured Draft/panel rate (~145 ms per
/// frame) 60 frames is ~9 s of unbroken motion — long enough for a human to
/// walk over and watch, short enough not to dominate the board session.
const LIVE_FRAMES: u32 = 60;

/// One CONTINUOUS animation, at the tier the sweep above says is fastest for a
/// busy scene.
///
/// Two reasons it exists. First, the only witness to "does the panel animate?"
/// is a person looking at it, and eight short sweep cells interleaved with
/// untimed full-frame re-bases are hard to watch; this is nine seconds of
/// nothing but incremental updates. Second, it produces ONE end-to-end number
/// that is not a sum of separately-timed windows — every cycle between the
/// first and last frame is inside it, IR build, parse, diff, render, flush and
/// all the loop overhead in between.
///
/// Timed as a Σ of per-frame deltas rather than one big window: DWT_CYCCNT is
/// 32-bit and wraps every ~23.9 s at 180 MHz, which nine seconds of frames
/// could straddle ambiguously, whereas a single ~145 ms frame never can.
fn live(
    emit: &mut dyn FnMut(&str),
    fonts: &mut Fonts,
    assets: &Assets,
    band_buf: &mut [u8],
) -> Result<(), RenderError> {
    let quality = Quality::Draft;
    let mode = Mode::Panel;
    let mut prev = Request::parse(&ir(&vals(mode, 0)))?;
    flush_full(&prev, fonts, assets, band_buf, quality)?;
    let warm = Request::parse(&ir(&vals(mode, 1)))?;
    update(&prev, &warm, fonts, assets, band_buf, quality)?;
    prev = warm;

    emit(&format!(
        "ALERT [vyr-size] anim live: {LIVE_FRAMES} continuous incremental frames \
         (q=Draft mode=panel) START — the readout, toggle, scope trace, both \
         sliders and the progress bar should all be moving; the header, gauge \
         ring and footer must stay perfectly still (they are never re-sent)"
    ));

    let mut cycles = 0u64;
    let mut dirty_px = 0u64;
    for f in 2..(2 + LIVE_FRAMES) {
        let t0 = cyc();
        let next = Request::parse(&ir(&vals(mode, f)))?;
        let c = update(&prev, &next, fonts, assets, band_buf, quality)?;
        let t1 = cyc();
        cycles += t1.wrapping_sub(t0) as u64;
        dirty_px += c.dirty_px;
        prev = next;
    }
    emit(&format!(
        "INFO  [vyr-size] anim live: q=Draft mode=panel frames={LIVE_FRAMES} \
         c_wall={cycles} dirty_px={dirty_px} screen_px={SCREEN_PX}"
    ));
    Ok(())
}

/// Animate the panel and report what it cost. Runs AFTER
/// [`crate::lcd::show_panel_scene`] (SPI5 and the ILI9341 are already up) and
/// entirely OUTSIDE the 480×270 measurement's DWT window — the `--features
/// board` hash and cycles/frame are untouched by anything in this file.
pub fn run(emit: &mut dyn FnMut(&str), band_buf: &mut [u8]) -> Result<(), RenderError> {
    emit(&format!(
        "INFO  [vyr-size] anim: dirty-rect panel animation {PANEL_W}x{PANEL_H} \
         (screen_px={SCREEN_PX}), band={PANEL_BAND_H} rows, band_buf={} B, \
         sysclk_hz={}, spi_hz=5625000, bytes_per_dirty_px=2",
        band_buf.len(),
        crate::m4::sysclk_hz(),
    ));

    let mut fonts = Fonts::new();
    fonts.register(
        "roboto",
        crate::opaque(crate::workload::SUBSET_FONT).to_vec(),
    )?;
    let mut assets = Assets::new();
    assets.register(
        vyr_core::demo::IMAGE_ASSET,
        vyr_core::RgbaImage::new(24, 24, crate::opaque(crate::workload::CHECKER).to_vec())?,
    )?;

    for quality in [Quality::Exact, Quality::Draft] {
        for mode in MODES {
            measure_mode(emit, &mut fonts, &assets, band_buf, quality, mode)?;
        }
    }
    emit(
        "INFO  [vyr-size] anim: dirty-rect animation sweep done — every cell above repainted the panel incrementally",
    );
    live(emit, &mut fonts, &assets, band_buf)?;
    emit(
        "ALERT [vyr-size] anim: done — the last incremental frame stays on the glass (the ILI9341 refreshes from its own GRAM after the target halts)",
    );
    Ok(())
}

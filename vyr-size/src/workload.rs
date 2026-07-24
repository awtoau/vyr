//! The run-qemu workload — the honest MCU frame (#9 dynamic half).
//!
//! Registers the ASCII SUBSET font (assets/roboto-ascii.ttf, 8,084 B — the
//! LVGL-style cut-down face; yes this is still the #19 heap copy, now ~8 KB
//! instead of 160 KB, which is exactly the point of the subset) + the
//! checker RGBA, parses a 480×270 panel fixture ONCE, then renders it as
//! HORIZONTAL 480×16 BANDS into one reused RGB888 band buffer — a full
//! 480×270 gutter pixmap is 567,424 B and cannot exist in 192 KiB SRAM;
//! banded is the only honest mode on this class of part.
//!
//! Every band's output bytes fold into ONE streaming FNV-1a hash. Bands are
//! full-width and emitted in row order, so the stream is byte-identical to
//! the assembled full frame — the hash a host full-frame render produces is
//! the hash the M4 must produce (band equivalence is the repo's day-1
//! invariant, here re-proven CROSS-ISA on an emulated M4 core).
//!
//! The same module runs on the host (std leg) and on the M4 (semihosting
//! leg): the caller supplies the line sink, the heap-counter read, and an
//! optional centisecond clock (SYS_CLOCK under qemu icount — deterministic
//! virtual time).

use alloc::format;
// vec! is only needed by the host-only full-frame cross-check below.
#[cfg(not(target_os = "none"))]
use alloc::vec;

use vyr_core::{Assets, Fonts, Quality, Rect, RenderError, RenderStats, Shapes};

use crate::opaque;

/// F16 (#16): the quality tier this build renders at. `Quality::Exact` by
/// default (the oracle frame); `--features draft` selects `Quality::Draft`
/// (integer no-AA fast path), `--features fast` selects `Quality::Fast` (#27:
/// integer spans, anti-aliased curves) — a build-time choice because the M4
/// binary has no env. `./dev.py qemu-m4 --draft` / `--fast` flips it.
#[cfg(not(any(feature = "draft", feature = "fast")))]
pub const WORKLOAD_QUALITY: Quality = Quality::Exact;
/// See [`WORKLOAD_QUALITY`]. `draft` wins if both tier features are enabled.
#[cfg(feature = "draft")]
pub const WORKLOAD_QUALITY: Quality = Quality::Draft;
/// #27 middle tier — see [`WORKLOAD_QUALITY`].
#[cfg(all(feature = "fast", not(feature = "draft")))]
pub const WORKLOAD_QUALITY: Quality = Quality::Fast;

/// Fixture frame size — the 480×270 the vyvanse memory profiles use.
pub const FIXTURE_W: u32 = 480;
/// Fixture frame size.
pub const FIXTURE_H: u32 = 270;
/// Horizontal band height: 480×16 ⇒ gutter pixmap (480+16)·(16+16)·4 =
/// 63,488 B + band buffer 480·16·3 = 23,040 B — the F9 working-RAM model's
/// reference band (docs/measurements/f9-static.md).
pub const BAND_H: u32 = 16;

/// Size of the caller-provided RGB888 band buffer. The CALLER places it —
/// the M4 leg keeps it as a CCM static (SRAM is the heap's), the host leg
/// heap-allocates it (vyr-cli's shape, so the heap columns stay readable:
/// host peak INCLUDES these bytes, the M4 peak does not — the table prices
/// the buffer as its own row either way).
pub const BAND_BYTES: usize = (FIXTURE_W as usize) * 3 * (BAND_H as usize);

/// The ASCII subset of Roboto (provenance: assets/roboto-ascii.md;
/// regenerate: scripts/make-subset-font.py). Lives in flash via
/// include_bytes!; `Fonts::register` then copies it to heap — the #19
/// finding, priced in the heap numbers this workload reports.
pub const SUBSET_FONT: &[u8] = include_bytes!("../assets/roboto-ascii.ttf");

/// The 24×24 straight-RGBA checker (decode-at-build — the F15 model).
/// `pub` so the `lcd` leg's panel scene blits the SAME flash-resident asset.
pub const CHECKER: &[u8] = include_bytes!("../assets/checker-24.rgba");

/// 480×270 panel fixture — DEMO_IR/TEXT_IR-derived: text (both font sizes),
/// an image blit, and one of each headline widget, all crossing several
/// 16-row band seams. ASCII-only text (the subset font's whole range).
/// (`--features rig` animates `vyr-scene` instead and never reads this; the
/// constant stays so flipping the feature off is a one-word change.)
#[cfg_attr(feature = "rig", allow(dead_code))]
pub const FIXTURE_IR: &str = r##"{
  "schema_version": "0.6-vyvanse",
  "w": 480, "h": 270,
  "root": {"name": "view", "attrs": {"background": "#22262B"}, "children": [
    {"name": "vy_frame", "attrs": {"x": "12", "y": "10", "width": "456", "height": "44",
      "background": "#2E3440", "radius": "8", "border_width": "1", "border_color": "#4C566A"}},
    {"name": "vy_label", "attrs": {"x": "28", "y": "22", "width": "240", "height": "20",
      "text": "Compressor 2 - line B", "color": "#ECEFF4"}},
    {"name": "vy_image", "attrs": {"x": "428", "y": "20", "width": "24", "height": "24",
      "src": "checker-24.png"}},
    {"name": "vy_gauge", "attrs": {"x": "24", "y": "76", "width": "110", "height": "110",
      "value": "65", "color": "#88C0D0"}},
    {"name": "vy_lcd", "attrs": {"x": "44", "y": "196", "width": "90", "height": "24",
      "text": "1480", "color": "#A3BE8C", "style_text_font": "roboto_20"}},
    {"name": "vy_slider", "attrs": {"x": "180", "y": "92", "width": "260", "height": "18",
      "value": "62"}},
    {"name": "vy_slider", "attrs": {"x": "180", "y": "128", "width": "260", "height": "18",
      "value": "35"}},
    {"name": "vy_progress", "attrs": {"x": "180", "y": "164", "width": "260", "height": "12",
      "value": "80"}},
    {"name": "vy_toggle", "attrs": {"x": "180", "y": "196", "width": "56", "height": "28",
      "value": "1"}},
    {"name": "vy_label", "attrs": {"x": "248", "y": "202", "width": "120", "height": "18",
      "text": "bypass", "color": "#D8DEE9"}},
    {"name": "vy_line", "attrs": {"x": "12", "y": "236", "width": "456", "height": "2",
      "background": "#4C566A"}},
    {"name": "vy_label", "attrs": {"x": "16", "y": "246", "width": "220", "height": "16",
      "text": "awto / vyr on emulated M4", "color": "#7A869A"}}
  ]}
}"##;

// FNV-1a 64 — the repo's golden-hash function (same constants as
// vyr-rig::fnv1a; reimplemented here because vyr-rig is a std crate).
const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x100_0000_01b3;

fn fnv1a_fold(mut h: u64, data: &[u8]) -> u64 {
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(FNV_PRIME);
    }
    h
}

fn phase(emit: &mut dyn FnMut(&str), heap: &dyn Fn() -> (usize, usize), name: &str) {
    let (live, peak) = heap();
    emit(&format!(
        "INFO  [vyr-size] phase {name}: heap live={live} B peak={peak} B"
    ));
}

/// Render one full frame as horizontal bands into the reused `band_buf`,
/// streaming every output byte into the FNV fold. When `report` carries the
/// (emit, heap) pair, the heap mark after band 1 is emitted (frame 1 only —
/// the per-phase story). Returns (frame hash, Σ pixels_written, last band's
/// stats — whose glyph-cache counters are the Fonts registry's cumulative
/// truth).
#[allow(clippy::type_complexity, clippy::too_many_arguments)]
fn render_frame_banded(
    req: &vyr_core::ir::Request,
    fonts: &mut Fonts,
    assets: &Assets,
    shapes: &mut Shapes,
    band_buf: &mut [u8],
    quality: Quality,
    report: Option<(&mut dyn FnMut(&str), &dyn Fn() -> (usize, usize))>,
    fold: bool,
) -> Result<(u64, u64, RenderStats), RenderError> {
    let stride = (FIXTURE_W * 3) as usize;
    let mut report = report;
    let mut hash = FNV_OFFSET;
    let mut pixels = 0u64;
    let mut fastpath = 0u64;
    let mut stats = RenderStats::default();
    let mut y = 0u32;
    while y < FIXTURE_H {
        let h = BAND_H.min(FIXTURE_H - y);
        let band = Rect {
            x: 0,
            y: y as i32,
            w: FIXTURE_W,
            h,
        };
        let len = stride * h as usize;
        // #32: `shapes` is held OUTSIDE this loop (and outside the frame
        // loop), so each distinct contour is flattened once for the life of
        // the run instead of once per band per frame. On this part that is
        // the difference between paying `libm`'s f64-in-software trig 17
        // times over and paying it once.
        stats = req.render_with_shapes(
            fonts,
            assets,
            shapes,
            band,
            &mut band_buf[..len],
            stride,
            quality,
        )?;
        pixels += stats.pixels_written;
        fastpath += stats.fastpath_pixels;
        // black_box is UNCONDITIONAL and load-bearing: it forces the band
        // bytes to be materialized, so the renderer cannot be optimized away
        // when `fold` is false. Only the FNV fold itself is skipped — see #44.
        let band = core::hint::black_box(&band_buf[..len]);
        if fold {
            hash = fnv1a_fold(hash, band);
        }
        if y == 0
            && let Some((emit, heap)) = report.take()
        {
            phase(emit, heap, "first-band");
        }
        y += h;
    }
    // Carry the FRAME-summed fast-path coverage (the per-band stats hold only
    // the last band's) so the F16 honesty number is the whole frame's.
    stats.fastpath_pixels = fastpath;
    Ok((hash, pixels, stats))
}

/// Run the whole workload, emitting the structured report lines through
/// `emit` (semihosting on M4, stdout on hosts). `heap` reads the counting
/// allocator's (live, peak); `clock_cs` — when present — is semihosting
/// SYS_CLOCK (centiseconds of qemu virtual time; deterministic under
/// icount) and enables the timed warmed-frames loop. `band_buf` is the
/// caller-placed [`BAND_BYTES`] output buffer. Returns the frame hash.
pub fn run(
    emit: &mut dyn FnMut(&str),
    heap: &dyn Fn() -> (usize, usize),
    clock_cs: Option<&dyn Fn() -> i32>,
    band_buf: &mut [u8],
    quality: Quality,
) -> Result<u64, RenderError> {
    #[cfg(feature = "rig")]
    return run_animated(emit, heap, clock_cs, band_buf, quality);
    #[cfg(not(feature = "rig"))]
    run_static(emit, heap, clock_cs, band_buf, quality)
}

/// The original single-fixture workload: one frame, rendered over and over.
/// See [`run`].
#[cfg(not(feature = "rig"))]
fn run_static(
    emit: &mut dyn FnMut(&str),
    heap: &dyn Fn() -> (usize, usize),
    clock_cs: Option<&dyn Fn() -> i32>,
    band_buf: &mut [u8],
    quality: Quality,
) -> Result<u64, RenderError> {
    if band_buf.len() != BAND_BYTES {
        // Honest hard failure — a short buffer would render a thinner band
        // than the model prices.
        return Err(RenderError::BadIr(alloc::format!(
            "band_buf is {} B, expected {BAND_BYTES}",
            band_buf.len()
        )));
    }
    let qname = match quality {
        Quality::Exact => "Exact",
        Quality::Fast => "Fast",
        Quality::Draft => "Draft",
    };
    emit(&format!(
        "INFO  [vyr-size] workload: {FIXTURE_W}x{FIXTURE_H} RGB888 in {FIXTURE_W}x{BAND_H} \
         horizontal bands; subset font {} B (full roboto: 162,876 B); quality={qname}",
        SUBSET_FONT.len()
    ));

    let mut fonts = Fonts::new();
    // The #19 heap copy, subset-sized — priced by the font-reg phase line.
    fonts.register("roboto", opaque(SUBSET_FONT).to_vec())?;
    phase(emit, heap, "font-reg");

    let mut assets = Assets::new();
    assets.register(
        vyr_core::demo::IMAGE_ASSET,
        vyr_core::RgbaImage::new(24, 24, opaque(CHECKER).to_vec())?,
    )?;
    phase(emit, heap, "asset-reg");

    // Parse ONCE, render bands from the kept tree — the MCU loop shape
    // (vyr-cli does the same: Request::parse, then render_with per area).
    let req = vyr_core::ir::Request::parse(opaque(FIXTURE_IR))?;
    phase(emit, heap, "parse");

    // #32: ONE contour memo for the whole run — the curve flattening a banded
    // renderer would otherwise repeat for every band it touches, every frame.
    // Its heap cost is inside the phase/peak lines below, deliberately: a cache
    // that did not fit the 122,880 B arena would be a regression, not a win.
    let mut shapes = Shapes::new();

    let (hash, pixels, stats) = render_frame_banded(
        &req,
        &mut fonts,
        &assets,
        &mut shapes,
        band_buf,
        quality,
        Some((&mut *emit, heap)),
        true,
    )?;
    phase(emit, heap, "frame");
    let bands = FIXTURE_H.div_ceil(BAND_H);
    emit(&format!(
        "INFO  [vyr-size] frame fnv1a={hash:#018x} bands={bands} pixels_written={pixels}"
    ));
    // F16 (#16) honesty: how much of THIS frame the integer fast path carried
    // (0 under Exact). The remaining pixels took the tiny-skia/Exact path —
    // the residue the headline insns/frame still pays for.
    let cov = if pixels > 0 {
        100.0 * stats.fastpath_pixels as f64 / pixels as f64
    } else {
        0.0
    };
    emit(&format!(
        "INFO  [vyr-size] F16 fast-path: {} / {pixels} delivered px ({cov:.1}%) via the \
         integer no-AA span fill (quality={qname})",
        stats.fastpath_pixels
    ));
    emit(&format!(
        "INFO  [vyr-size] glyph cache: rasterized={} entries={} bytes={}",
        stats.glyphs_rasterized, stats.glyph_cache_entries, stats.glyph_cache_bytes
    ));
    // #32 honesty: what the contour memo actually held and how often it hit.
    // `overflow` MUST be 0 — a non-zero value means the scene outgrew the
    // budget and part of the frame is still re-flattening per band.
    emit(&format!(
        "INFO  [vyr-size] contour memo: entries={} bytes={} hits={} misses={} overflow={}",
        shapes.cache_entries(),
        shapes.cache_bytes(),
        shapes.hits(),
        shapes.misses(),
        shapes.overflow()
    ));

    if let Some(clock) = clock_cs {
        // Warmed steady state: glyph cache is full, so these frames are the
        // per-frame render cost. SYS_CLOCK ticks are centiseconds (1 cs = 10⁷
        // insns); 20 frames so the ±1 cs quantization is ÷20 = ±0.5 M/frame,
        // fine enough to resolve Draft's ~12 M/frame against the LVGL 10 M
        // anchor (4 frames quantized to ±2.5 M/frame — too coarse near LVGL).
        const TIMED_FRAMES: u32 = 20;

        // #44: the hash fold is NOT inside the timed window. It was 36.2% of
        // Draft's frame and 54.7% of LVGL's, so leaving it in meant every
        // published per-frame figure was part renderer, part benchmark — and
        // because it is a roughly fixed cost it flattered the SMALLER frame,
        // which is exactly the comparison that matters. Subtracting it
        // afterwards proved unreliable: the fold's own cost varies with tier,
        // opt-level and compiler, and a stale fold figure once inverted the
        // sign of a published result.
        //
        // Determinism is still proven, just not on the stopwatch: this
        // verification pass renders and folds OUTSIDE the window and must
        // reproduce the reference hash before the timed pass runs.
        let (h_verify, _, _) = render_frame_banded(
            &req,
            &mut fonts,
            &assets,
            &mut shapes,
            band_buf,
            quality,
            None,
            true,
        )?;
        if h_verify != hash {
            emit(&format!(
                "ERROR [vyr-size] warmed frame hash {h_verify:#018x} != first {hash:#018x}"
            ));
            return Err(RenderError::Unimplemented("non-deterministic re-render"));
        }

        let t0 = clock();
        for _ in 0..TIMED_FRAMES {
            render_frame_banded(
                &req,
                &mut fonts,
                &assets,
                &mut shapes,
                band_buf,
                quality,
                None,
                false,
            )?;
        }
        let t1 = clock();
        // Wrapping u32: on the board leg the clock is DWT_CYCCNT, a
        // free-running 32-bit counter, and one Exact frame is ~1.1e8 cycles —
        // 20 of them (~2.2e9) overflow an i32 and print NEGATIVE if the
        // subtraction is done signed (measured: "-2044044886 cs"). Doing it in
        // u32 also makes a counter wrap inside the window yield the right
        // delta.
        let delta = (t1 as u32).wrapping_sub(t0 as u32);

        // #44: a SECOND timed pass, identical but WITH the fold, so `total`,
        // `fold` and `render_only` are all MEASURED on this cell rather than
        // derived from a fold figure taken on some other tier/opt-level/
        // compiler — the arithmetic that once inverted the sign of a published
        // result. `render_only` is the headline; `total` keeps old rows
        // comparable; `fold` is their difference and is reported, not assumed.
        let t2 = clock();
        for _ in 0..TIMED_FRAMES {
            render_frame_banded(
                &req,
                &mut fonts,
                &assets,
                &mut shapes,
                band_buf,
                quality,
                None,
                true,
            )?;
        }
        let t3 = clock();
        let total = (t3 as u32).wrapping_sub(t2 as u32);
        // Saturating: the two passes are separately quantized (1 cs on the
        // qemu leg), so a short run can read total slightly BELOW render_only.
        // That is quantization noise, not a negative fold — clamp at 0 rather
        // than wrapping to ~4e9 and reporting a spectacular fake.
        let fold = total.saturating_sub(delta);

        // Dead-code sanity gate. Removing the fold risks the compiler
        // eliminating the render itself, which would look like an enormous
        // speedup. The fold's largest measured share of a frame is 54.7%
        // (LVGL), i.e. render_only was 45% of total at its worst; a 10% floor
        // sits ~4.5x below that and can only trip on near-total elimination,
        // not on a genuinely fold-dominated frame.
        if delta < total / 10 {
            emit(&format!(
                "ERROR [vyr-size] timed render_only {delta} < 10% of total {total} \
                 — the timed render was probably eliminated; refusing to report"
            ));
            return Err(RenderError::Unimplemented("timed loop eliminated"));
        }

        #[cfg(feature = "board")]
        emit(&format!(
            "INFO  [vyr-size] timed: {TIMED_FRAMES} warmed frames in {delta} cycles \
             (DWT_CYCCNT, REAL SILICON — {} cycles/frame) \
             render_only={delta} total={total} fold={fold} \
             render_only_per_frame={} total_per_frame={} fold_per_frame={}",
            delta / TIMED_FRAMES,
            delta / TIMED_FRAMES,
            total / TIMED_FRAMES,
            fold / TIMED_FRAMES
        ));
        #[cfg(not(feature = "board"))]
        emit(&format!(
            "INFO  [vyr-size] timed: {TIMED_FRAMES} warmed frames in {delta} cs virtual \
             (SYS_CLOCK; icount shift=0 makes 1 virtual ns = 1 guest insn) \
             render_only={delta} total={total} fold={fold}"
        ));
    }

    let (live, peak) = heap();
    emit(&format!(
        "ALERT [vyr-size] workload ok: heap peak={peak} B live-end={live} B"
    ));
    Ok(hash)
}

// --- the long ANIMATED workload (`--features rig`) ----------------------------

/// Detail level of the animated scene — [`vyr_scene::Detail::Lite`] under
/// `--features rig-lite` (the MCU-fitting variant), Full otherwise.
#[cfg(all(feature = "rig", not(feature = "rig-lite")))]
pub const RIG_DETAIL: vyr_scene::Detail = vyr_scene::Detail::Full;
/// See [`RIG_DETAIL`].
#[cfg(all(feature = "rig", feature = "rig-lite"))]
pub const RIG_DETAIL: vyr_scene::Detail = vyr_scene::Detail::Lite;

/// Frames the animated workload renders — `vyr_scene::Preset`, selected by
/// feature because the M4 binary has no env and the count must be part of
/// the build the ELF hash pins.
#[cfg(all(feature = "rig", feature = "rig-smoke"))]
pub const RIG_FRAMES: u32 = 60;
/// See [`RIG_FRAMES`].
#[cfg(all(feature = "rig", feature = "rig-full", not(feature = "rig-smoke")))]
pub const RIG_FRAMES: u32 = 1200;
/// See [`RIG_FRAMES`].
#[cfg(all(feature = "rig", not(feature = "rig-smoke"), not(feature = "rig-full")))]
pub const RIG_FRAMES: u32 = 300;

/// The long animated workload: `RIG_FRAMES` DIFFERENT frames of
/// `vyr_scene`'s long scene, each generated from its frame index, parsed,
/// and rendered in 480×16 bands — the shape a real animating device runs.
///
/// Two passes, deliberately:
///
/// 1. **Survey (untimed).** Every frame's banded byte stream folds into a
///    per-frame FNV-1a, and those chain into ONE run digest — the same chain
///    `vyr-rig` computes on the host, so a single hash compares whole runs
///    across ISAs and the per-frame line says which frame first diverged.
///    Heap live/peak, glyph-cache size and contour-memo occupancy are
///    emitted periodically: a heap PEAK is a scalar and cannot show drift,
///    and drift over a long run is the entire point of a long run.
/// 2. **Timed.** The same frames again with no semihosting inside the loop,
///    bracketed by two clock reads, so `scripts/qemu-insn.py`'s
///    largest-delta rule still isolates exactly the render window (#44: the
///    hash fold stays OUT of it). `--features rig-perframe` adds a clock
///    read per frame instead, turning that window into a per-frame series.
///
/// The pass-2 window INCLUDES scene generation and IR parse. That is not
/// slack: an animating UI re-emits its IR every frame, so those are frame
/// costs, and hiding them would make the number describe a still image.
#[cfg(feature = "rig")]
fn run_animated(
    emit: &mut dyn FnMut(&str),
    heap: &dyn Fn() -> (usize, usize),
    clock_cs: Option<&dyn Fn() -> i32>,
    band_buf: &mut [u8],
    quality: Quality,
) -> Result<u64, RenderError> {
    if band_buf.len() != BAND_BYTES {
        return Err(RenderError::BadIr(alloc::format!(
            "band_buf is {} B, expected {BAND_BYTES}",
            band_buf.len()
        )));
    }
    let qname = match quality {
        Quality::Exact => "Exact",
        Quality::Fast => "Fast",
        Quality::Draft => "Draft",
    };
    emit(&format!(
        "INFO  [vyr-size] rig workload: scene {} suite {} — {RIG_FRAMES} frames \
         ({:.2} s @ {} fps) at {FIXTURE_W}x{FIXTURE_H} in {FIXTURE_W}x{BAND_H} bands; \
         quality={qname}",
        RIG_DETAIL.scene_id(),
        vyr_scene::SUITE_VERSION,
        RIG_FRAMES as f32 / vyr_scene::FPS as f32,
        vyr_scene::FPS,
    ));

    let mut fonts = Fonts::new();
    fonts.register("roboto", opaque(SUBSET_FONT).to_vec())?;
    phase(emit, heap, "font-reg");
    let mut assets = Assets::new();
    assets.register(
        vyr_core::demo::IMAGE_ASSET,
        vyr_core::RgbaImage::new(24, 24, opaque(CHECKER).to_vec())?,
    )?;
    phase(emit, heap, "asset-reg");

    // ONE contour memo for the whole run (#32) — and now under churn: the
    // scene's gauge breathes through 13 distinct diameters, so this is the
    // first time the 8 KiB budget is asked to hold more than one shape.
    let mut shapes = Shapes::new();

    // ~20 samples whatever the preset, so the survey's own semihosting
    // deltas stay far smaller than the timed window (qemu-insn.py takes the
    // LARGEST delta; a coarse survey must never out-run the measurement).
    let report_every = (RIG_FRAMES / 20).max(1);
    let mut chain = vyr_scene::FNV_OFFSET;
    for f in 0..RIG_FRAMES {
        // The IR STRING is dropped before the render starts: on a part with
        // 122,880 B of arena, holding a 3–4 KB scene string alive across a
        // banded frame is 3–4 KB of peak nobody needs, and a real animating
        // device would not do it either.
        let req = {
            let ir = vyr_scene::scene_ir(FIXTURE_W, FIXTURE_H, f, RIG_DETAIL);
            vyr_core::ir::Request::parse(opaque(ir.as_str()))?
        };
        let (hash, pixels, stats) = render_frame_banded(
            &req,
            &mut fonts,
            &assets,
            &mut shapes,
            band_buf,
            quality,
            None,
            true,
        )?;
        chain = vyr_scene::fnv1a_chain(chain, hash);
        if f.is_multiple_of(report_every) || f + 1 == RIG_FRAMES {
            let (live, peak) = heap();
            emit(&format!(
                "INFO  [vyr-size] rig frame={f} hash={hash:#018x} px={pixels} \
                 heap_live={live} heap_peak={peak} glyph_bytes={} glyph_entries={} \
                 memo_bytes={} memo_hits={} memo_misses={} memo_overflow={} fastpath={}",
                stats.glyph_cache_bytes,
                stats.glyph_cache_entries,
                shapes.cache_bytes(),
                shapes.hits(),
                shapes.misses(),
                shapes.overflow(),
                stats.fastpath_pixels,
            ));
        }
    }
    let (live, peak) = heap();
    emit(&format!(
        "INFO  [vyr-size] rig chain fnv1a={chain:#018x} frames={RIG_FRAMES} \
         scene={} detail_frames_ok heap_peak={peak} heap_live={live}",
        RIG_DETAIL.scene_id(),
    ));

    if let Some(clock) = clock_cs {
        let t0 = clock();
        for f in 0..RIG_FRAMES {
            // Per-frame bracketing (#rig-perframe): a semihosting trap here
            // makes every plugin delta exactly one frame, which is how the
            // WORST frame gets a number instead of an estimate.
            #[cfg(feature = "rig-perframe")]
            let _ = clock();
            let req = {
                let ir = vyr_scene::scene_ir(FIXTURE_W, FIXTURE_H, f, RIG_DETAIL);
                vyr_core::ir::Request::parse(opaque(ir.as_str()))?
            };
            render_frame_banded(
                &req,
                &mut fonts,
                &assets,
                &mut shapes,
                band_buf,
                quality,
                None,
                false,
            )?;
        }
        let t1 = clock();
        let delta = (t1 as u32).wrapping_sub(t0 as u32);
        // The literal "timed: N warmed frames" spelling is the contract
        // scripts/qemu-insn.py parses to divide the window by frames.
        #[cfg(feature = "board")]
        emit(&format!(
            "INFO  [vyr-size] timed: {RIG_FRAMES} warmed frames in {delta} cycles \
             (DWT_CYCCNT, REAL SILICON — {} cycles/frame, animated)",
            delta / RIG_FRAMES
        ));
        #[cfg(not(feature = "board"))]
        emit(&format!(
            "INFO  [vyr-size] timed: {RIG_FRAMES} warmed frames in {delta} cs virtual \
             (SYS_CLOCK; the window includes scene emit + IR parse — animation frame cost)"
        ));
    }

    let (live, peak) = heap();
    emit(&format!(
        "ALERT [vyr-size] rig workload ok: heap peak={peak} B live-end={live} B"
    ));
    Ok(chain)
}

/// Host-only cross-check for the animated workload: frame 0 of the long
/// scene, hashed BOTH ways — `(banded, full-frame)`. Frame 0 rather than the
/// chain, because a chain over many frames has no full-frame counterpart to
/// compare against; band equivalence is a per-frame property and this is the
/// per-frame form of it.
#[cfg(all(not(target_os = "none"), feature = "rig"))]
pub fn rig_frame0_hashes(quality: Quality) -> Result<(u64, u64), RenderError> {
    let mut fonts = Fonts::new();
    fonts.register("roboto", SUBSET_FONT.to_vec())?;
    let mut assets = Assets::new();
    assets.register(
        vyr_core::demo::IMAGE_ASSET,
        vyr_core::RgbaImage::new(24, 24, CHECKER.to_vec())?,
    )?;
    let ir = vyr_scene::scene_ir(FIXTURE_W, FIXTURE_H, 0, RIG_DETAIL);
    let req = vyr_core::ir::Request::parse(&ir)?;
    let mut shapes = Shapes::new();
    let mut band = vec![0u8; BAND_BYTES];
    let (banded, _, _) = render_frame_banded(
        &req,
        &mut fonts,
        &assets,
        &mut shapes,
        &mut band,
        quality,
        None,
        true,
    )?;
    let stride = (FIXTURE_W * 3) as usize;
    let mut buf = vec![0u8; stride * FIXTURE_H as usize];
    let area = Rect {
        x: 0,
        y: 0,
        w: FIXTURE_W,
        h: FIXTURE_H,
    };
    vyr_core::render_with_quality(&ir, &mut fonts, &assets, area, &mut buf, stride, quality)?;
    Ok((banded, fnv1a_fold(FNV_OFFSET, &buf)))
}

/// Host-only cross-check: the SAME fixture rendered full-frame in one pass
/// (the 567,424 B gutter pixmap a 192 KiB part can never hold), hashed with
/// the same FNV — must equal the banded stream's hash (band equivalence).
#[cfg(all(not(target_os = "none"), not(feature = "rig")))]
pub fn full_frame_hash(quality: Quality) -> Result<u64, RenderError> {
    let mut fonts = Fonts::new();
    fonts.register("roboto", SUBSET_FONT.to_vec())?;
    let mut assets = Assets::new();
    assets.register(
        vyr_core::demo::IMAGE_ASSET,
        vyr_core::RgbaImage::new(24, 24, CHECKER.to_vec())?,
    )?;
    let stride = (FIXTURE_W * 3) as usize;
    let mut buf = vec![0u8; stride * FIXTURE_H as usize];
    let area = Rect {
        x: 0,
        y: 0,
        w: FIXTURE_W,
        h: FIXTURE_H,
    };
    vyr_core::render_with_quality(
        FIXTURE_IR, &mut fonts, &assets, area, &mut buf, stride, quality,
    )?;
    Ok(fnv1a_fold(FNV_OFFSET, &buf))
}

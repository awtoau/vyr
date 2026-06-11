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

use vyr_core::{Assets, Fonts, Quality, Rect, RenderError, RenderStats};

use crate::opaque;

/// F16 (#16): the quality tier this build renders at. `Quality::Exact` by
/// default (the 75 M-insn oracle frame); `--features draft` selects
/// `Quality::Draft` (integer no-AA fast path) — a build-time choice because
/// the M4 binary has no env. `./dev.py qemu-m4 --draft` flips it.
#[cfg(not(feature = "draft"))]
pub const WORKLOAD_QUALITY: Quality = Quality::Exact;
/// See [`WORKLOAD_QUALITY`].
#[cfg(feature = "draft")]
pub const WORKLOAD_QUALITY: Quality = Quality::Draft;

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
const CHECKER: &[u8] = include_bytes!("../assets/checker-24.rgba");

/// 480×270 panel fixture — DEMO_IR/TEXT_IR-derived: text (both font sizes),
/// an image blit, and one of each headline widget, all crossing several
/// 16-row band seams. ASCII-only text (the subset font's whole range).
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
#[allow(clippy::type_complexity)]
fn render_frame_banded(
    req: &vyr_core::ir::Request,
    fonts: &mut Fonts,
    assets: &Assets,
    band_buf: &mut [u8],
    quality: Quality,
    report: Option<(&mut dyn FnMut(&str), &dyn Fn() -> (usize, usize))>,
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
        stats =
            req.render_with_quality(fonts, assets, band, &mut band_buf[..len], stride, quality)?;
        pixels += stats.pixels_written;
        fastpath += stats.fastpath_pixels;
        // black_box: the band bytes must be materialized before hashing.
        hash = fnv1a_fold(hash, core::hint::black_box(&band_buf[..len]));
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

    let (hash, pixels, stats) = render_frame_banded(
        &req,
        &mut fonts,
        &assets,
        band_buf,
        quality,
        Some((&mut *emit, heap)),
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

    if let Some(clock) = clock_cs {
        // Warmed steady state: glyph cache is full, so these frames are the
        // per-frame render cost. 4 frames because SYS_CLOCK ticks are
        // centiseconds — one frame would quantize away the reading.
        const TIMED_FRAMES: u32 = 4;
        let t0 = clock();
        for _ in 0..TIMED_FRAMES {
            let (h2, _, _) =
                render_frame_banded(&req, &mut fonts, &assets, band_buf, quality, None)?;
            if h2 != hash {
                emit(&format!(
                    "ERROR [vyr-size] warmed frame hash {h2:#018x} != first {hash:#018x}"
                ));
                return Err(RenderError::Unimplemented("non-deterministic re-render"));
            }
        }
        let t1 = clock();
        emit(&format!(
            "INFO  [vyr-size] timed: {TIMED_FRAMES} warmed frames in {} cs virtual \
             (SYS_CLOCK; icount shift=0 makes 1 virtual ns = 1 guest insn)",
            t1 - t0
        ));
    }

    let (live, peak) = heap();
    emit(&format!(
        "ALERT [vyr-size] workload ok: heap peak={peak} B live-end={live} B"
    ));
    Ok(hash)
}

/// Host-only cross-check: the SAME fixture rendered full-frame in one pass
/// (the 567,424 B gutter pixmap a 192 KiB part can never hold), hashed with
/// the same FNV — must equal the banded stream's hash (band equivalence).
#[cfg(not(target_os = "none"))]
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

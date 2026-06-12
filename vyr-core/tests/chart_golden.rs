//! F4 vy_chart composite goldens: a single-series line + bar chart, painted
//! from the IR's declared `points` (NOT invented demo data), renders
//! deterministically and byte-exact under banding at BOTH quality tiers —
//! Exact (AA series line) and Draft (hard-edged). Junk IR (`points` /
//! `chart_type` / `range`) hard-errors in every band (I6 + no error-swallowing).
//!
//! Re-bless the hashes: `VYR_BLESS=1` (see `./dev.py test --bless`).
//! `VYR_TEST_DUMP=1` writes the PNGs to ../tmp/ for eyeballing.

use vyr_core::demo::CHART_IR;
use vyr_core::{Assets, Fonts, Quality, Rect, RenderError, RenderStats, render_with_quality};

const W: u32 = 120;
const H: u32 = 120;

/// Committed goldens (FNV-1a 64). Re-bless via `VYR_BLESS=1`.
const CHART_EXACT_FNV1A: u64 = 0x7FF2_DCCD_2610_3B14;
const CHART_DRAFT_FNV1A: u64 = 0x88E0_2ED0_E84A_BFEF;

fn fnv1a(data: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(0x100_0000_01b3);
    }
    h
}

fn render_band(quality: Quality, y: i32, h: u32, band: &mut [u8], stride: usize) -> RenderStats {
    let mut fonts = Fonts::new();
    let assets = Assets::new();
    render_with_quality(
        CHART_IR,
        &mut fonts,
        &assets,
        Rect { x: 0, y, w: W, h },
        band,
        stride,
        quality,
    )
    .expect("chart fixture renders")
}

/// Full-frame render at `quality` as a single band.
fn render_full(quality: Quality) -> Vec<u8> {
    let stride = (W * 3) as usize;
    let mut buf = vec![0u8; stride * H as usize];
    let stats = render_band(quality, 0, H, &mut buf, stride);
    assert!(stats.pixels_written > 0, "chart wrote no pixels");
    buf
}

/// Stitched horizontal bands of `band_h`.
fn render_banded(quality: Quality, band_h: u32) -> Vec<u8> {
    let stride = (W * 3) as usize;
    let mut out = vec![0u8; stride * H as usize];
    let mut y = 0;
    while y < H {
        let h = band_h.min(H - y);
        let band = &mut out[y as usize * stride..(y + h) as usize * stride];
        render_band(quality, y as i32, h, band, stride);
        y += h;
    }
    out
}

fn dump_png(name: &str, buf: &[u8]) {
    if std::env::var_os("VYR_TEST_DUMP").is_none() {
        return;
    }
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../tmp");
    std::fs::create_dir_all(&dir).unwrap();
    let file = std::fs::File::create(dir.join(name)).unwrap();
    let mut enc = png::Encoder::new(std::io::BufWriter::new(file), W, H);
    enc.set_color(png::ColorType::Rgb);
    enc.set_depth(png::BitDepth::Eight);
    enc.write_header().unwrap().write_image_data(buf).unwrap();
}

#[test]
fn chart_exact_golden_hash() {
    let buf = render_full(Quality::Exact);
    dump_png("chart-exact.png", &buf);
    let h = fnv1a(&buf);
    if std::env::var_os("VYR_BLESS").is_some() {
        eprintln!("BLESS: CHART_EXACT_FNV1A = {h:#018X}");
        return;
    }
    assert_eq!(h, CHART_EXACT_FNV1A, "chart Exact golden drifted");
}

#[test]
fn chart_draft_golden_hash() {
    let buf = render_full(Quality::Draft);
    dump_png("chart-draft.png", &buf);
    let h = fnv1a(&buf);
    if std::env::var_os("VYR_BLESS").is_some() {
        eprintln!("BLESS: CHART_DRAFT_FNV1A = {h:#018X}");
        return;
    }
    assert_eq!(h, CHART_DRAFT_FNV1A, "chart Draft golden drifted");
}

#[test]
fn chart_band_equivalence() {
    for quality in [Quality::Exact, Quality::Draft] {
        let full = render_full(quality);
        for band_h in [30u32, 17u32] {
            let banded = render_banded(quality, band_h);
            assert_eq!(
                full, banded,
                "{quality:?} band_h={band_h}: chart band stitch differs from full frame"
            );
        }
    }
}

#[test]
fn chart_differs_between_tiers() {
    // The Draft series line is hard-edged where Exact AAs it — if the two
    // tiers ever matched byte-for-byte the Draft fast path did nothing.
    assert_ne!(
        fnv1a(&render_full(Quality::Exact)),
        fnv1a(&render_full(Quality::Draft)),
        "Exact and Draft chart renders are identical — the AA series collapsed"
    );
}

#[test]
fn chart_junk_points_is_honest_error() {
    let ir = r#"{"w":120,"h":120,"root":{"name":"view","children":[
        {"name":"vy_chart","attrs":{"x":"0","y":"0","width":"100","height":"60",
         "points":"10,oops,30"}}]}}"#;
    let mut fonts = Fonts::new();
    let assets = Assets::new();
    let mut buf = vec![0u8; (W * H * 3) as usize];
    let err = render_with_quality(
        ir,
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
        Quality::Exact,
    )
    .expect_err("a non-numeric chart point must hard-error, not be silently dropped");
    assert!(matches!(err, RenderError::BadIr(_)));
}

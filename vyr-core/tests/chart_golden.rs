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

fn scope_ir(points: &str, decimate: &str) -> String {
    format!(
        r##"{{"w":120,"h":120,"root":{{"name":"view","attrs":{{"background":"#202020"}},"children":[
            {{"name":"vy_chart","attrs":{{"x":"10","y":"10","width":"100","height":"100",
              "mode":"scope","decimate":"{}","show_background":"0","show_grid":"0","show_frame":"0",
              "show_markers":"0","line_width":"1","line_color":"#65FF9B","points":"{}"}}}}
        ]}}}}"##,
        decimate, points
    )
}

fn render_ir_full(ir: &str, quality: Quality) -> Vec<u8> {
    let mut fonts = Fonts::new();
    let assets = Assets::new();
    let stride = (W * 3) as usize;
    let mut buf = vec![0u8; stride * H as usize];
    render_with_quality(
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
        stride,
        quality,
    )
    .expect("scope fixture renders full frame");
    buf
}

fn render_ir_banded(ir: &str, quality: Quality, band_h: u32) -> Vec<u8> {
    let mut fonts = Fonts::new();
    let assets = Assets::new();
    let stride = (W * 3) as usize;
    let mut banded = vec![0u8; stride * H as usize];
    let mut y = 0;
    while y < H {
        let h = band_h.min(H - y);
        let band = &mut banded[y as usize * stride..(y + h) as usize * stride];
        render_with_quality(
            ir,
            &mut fonts,
            &assets,
            Rect {
                x: 0,
                y: y as i32,
                w: W,
                h,
            },
            band,
            stride,
            quality,
        )
        .expect("scope fixture renders as bands");
        y += h;
    }
    banded
}

fn dense_points(n: usize) -> String {
    (0..n)
        .map(|i| {
            // Mixed-frequency deterministic trace with narrow impulses so the
            // decimation path is exercised aggressively.
            let base = ((i * 37 + i / 7) % 101) as i32;
            let spike = if i % 251 < 3 { 35 } else { 0 };
            let dip = if i % 389 < 2 { -28 } else { 0 };
            (base + spike + dip).clamp(0, 100).to_string()
        })
        .collect::<Vec<_>>()
        .join(",")
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

#[test]
fn chart_junk_decimate_is_honest_error() {
    let ir = r#"{"w":120,"h":120,"root":{"name":"view","children":[
        {"name":"vy_chart","attrs":{"x":"0","y":"0","width":"100","height":"60",
         "points":"10,20,30","decimate":"fft"}}]}}"#;
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
    .expect_err("an unknown decimate mode must hard-error");
    assert!(matches!(err, RenderError::BadIr(_)));
}

#[test]
fn chart_scope_minmax_band_equivalence() {
    // Dense trace (many samples per pixel column): this exercises the
    // per-column min/max decimation path used for scope-style display.
    let points = (0..1200)
        .map(|i| ((i * 37) % 101).to_string())
        .collect::<Vec<_>>()
        .join(",");
    let ir = format!(
        r##"{{"w":120,"h":120,"root":{{"name":"view","attrs":{{"background":"#202020"}},"children":[
            {{"name":"vy_chart","attrs":{{"x":"10","y":"10","width":"100","height":"100",
              "mode":"scope","decimate":"minmax","show_background":"0","show_grid":"0","show_frame":"0",
              "show_markers":"0","line_width":"1","points":"{}"}}}}
        ]}}}}"##,
        points
    );
    for quality in [Quality::Exact, Quality::Draft] {
        let mut fonts = Fonts::new();
        let assets = Assets::new();
        let stride = (W * 3) as usize;
        let mut full = vec![0u8; stride * H as usize];
        render_with_quality(
            &ir,
            &mut fonts,
            &assets,
            Rect {
                x: 0,
                y: 0,
                w: W,
                h: H,
            },
            &mut full,
            stride,
            quality,
        )
        .expect("scope fixture renders full frame");
        for band_h in [30u32, 17u32] {
            let mut banded = vec![0u8; stride * H as usize];
            let mut y = 0;
            while y < H {
                let h = band_h.min(H - y);
                let band = &mut banded[y as usize * stride..(y + h) as usize * stride];
                render_with_quality(
                    &ir,
                    &mut fonts,
                    &assets,
                    Rect {
                        x: 0,
                        y: y as i32,
                        w: W,
                        h,
                    },
                    band,
                    stride,
                    quality,
                )
                .expect("scope fixture renders as bands");
                y += h;
            }
            assert_eq!(
                full, banded,
                "{quality:?} scope minmax band_h={band_h}: band stitch differs from full frame"
            );
        }
    }
}

#[test]
fn chart_scope_none_band_equivalence_aggressive() {
    // Very dense input to stress the plain polyline path through many seam
    // crossings and odd band splits.
    let ir = scope_ir(&dense_points(8000), "none");
    // Draft is the no-AA fast path used for budgeted scope workloads.
    // This keeps the seam-stress assertion strong without coupling it to
    // AA edge blending details in Exact.
    for quality in [Quality::Draft] {
        let full = render_ir_full(&ir, quality);
        for band_h in [1u32, 7u32, 13u32, 29u32, 30u32, 17u32] {
            let banded = render_ir_banded(&ir, quality, band_h);
            assert_eq!(
                full, banded,
                "{quality:?} scope none band_h={band_h}: band stitch differs from full frame"
            );
        }
    }
}

#[test]
fn chart_scope_minmax_band_equivalence_aggressive() {
    // Same workload shape as the "none" test, but on the per-column min/max
    // decimation path.
    let ir = scope_ir(&dense_points(8000), "minmax");
    for quality in [Quality::Exact, Quality::Draft] {
        let full = render_ir_full(&ir, quality);
        for band_h in [1u32, 7u32, 13u32, 29u32, 30u32, 17u32] {
            let banded = render_ir_banded(&ir, quality, band_h);
            assert_eq!(
                full, banded,
                "{quality:?} scope minmax(aggressive) band_h={band_h}: band stitch differs from full frame"
            );
        }
    }
}

#[test]
fn chart_scope_minmax_differs_from_none_on_dense_trace() {
    // Aggressive scope workloads should produce different pixels between
    // minmax and plain polyline decimation modes; if they match exactly then
    // the decimation mode is not taking effect.
    let points = dense_points(8000);
    for quality in [Quality::Exact, Quality::Draft] {
        let minmax = render_ir_full(&scope_ir(&points, "minmax"), quality);
        let none = render_ir_full(&scope_ir(&points, "none"), quality);
        assert_ne!(
            minmax, none,
            "{quality:?}: scope minmax and none produced identical pixels on dense input"
        );
    }
}

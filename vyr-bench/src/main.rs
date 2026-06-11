//! vyr-bench — the F2 perf harness (invariants I3 + I4).
//!
//! Deterministic micro/scene benches with **ns/px as the canonical metric**,
//! a committed baseline (`vyr-bench/baseline.json`), and the **scaling-law
//! assertion** (per-pixel cost must stay ~flat across band sizes — a
//! superlinear blowup means a per-band fixed cost crept in).
//!
//! Hand-rolled rather than criterion ON PURPOSE: the gate needs exactly
//! (a) a stable ns/px number per bench, (b) a committed baseline to diff,
//! (c) a hard pass/fail — not statistical HTML reports. Criterion can layer
//! on later for rigor; this binary IS the nightly gate.
//!
//! Modes:
//!   vyr-bench run      — print the table (+ scaling assertion)
//!   vyr-bench record   — run + WRITE baseline.json (a reviewed act: commit
//!                        it separately with the reason)
//!   vyr-bench check    — run + COMPARE vs baseline.json: any bench slower
//!                        than REGRESSION_X × baseline fails (rc 1)
//!
//! Methodology: per bench, WARMUP iters then ROUNDS timed runs of ITERS
//! iterations each; the MEDIAN round is reported (median absorbs scheduler
//! noise without discarding it silently). Normalizers are per-bench and
//! documented in `PIXELS` — primitives by op bbox, scenes by delivered px,
//! bands by gutter-inclusive raster px (the honest model: the painter
//! rasterizes (w+2G)×(h+2G) for a w×h band).
//!
//! Run in RELEASE (dev.py bench does) — debug numbers are not baselines.

use std::time::Instant;

use vyr_core::demo::{DEMO_H, DEMO_IR, DEMO_W, IMAGE_ASSET, IMAGE_IR, TEXT_IR, demo_scene};
use vyr_core::{Assets, Canvas, Fonts, Rect, Rgb, RgbaImage, TinySkiaCanvas};

/// A check fails when a bench exceeds baseline × this. 1.5 = real regressions
/// fire, day-to-day desktop noise (a few %) does not.
const REGRESSION_X: f64 = 1.5;
/// Scaling assertion: ns/raster-px at ANY band height may not exceed the
/// full-frame ns/raster-px by more than this. Catches per-band blowups
/// (alloc, re-walk explosions) while tolerating honest small fixed costs.
const SCALING_X: f64 = 3.0;

const WARMUP: u32 = 3;
const ROUNDS: usize = 7;
const ITERS: u32 = 50;

const GUTTER: u32 = 8; // mirrors painter::GUTTER (raster-area normalizer)

fn timestamp() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let s = now.as_secs() % 86_400;
    format!(
        "{:02}:{:02}:{:02}.{:06} UTC",
        s / 3600,
        (s % 3600) / 60,
        s % 60,
        now.subsec_micros()
    )
}

fn log(msg: &str) {
    use std::io::Write as _;
    let line = format!("{}  INFO  [vyr-bench] {}", timestamp(), msg);
    eprintln!("{line}");
    if std::fs::create_dir_all("tmp").is_ok()
        && let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open("tmp/vyr-bench.log")
    {
        let _ = writeln!(f, "{line}");
    }
}

/// Median of `ROUNDS` timed runs of `ITERS` iterations of `f`; returns ns per
/// single iteration.
fn measure(mut f: impl FnMut()) -> f64 {
    for _ in 0..WARMUP {
        f();
    }
    let mut rounds: Vec<f64> = (0..ROUNDS)
        .map(|_| {
            let t0 = Instant::now();
            for _ in 0..ITERS {
                f();
            }
            t0.elapsed().as_nanos() as f64 / ITERS as f64
        })
        .collect();
    rounds.sort_by(|a, b| a.partial_cmp(b).unwrap());
    rounds[ROUNDS / 2]
}

struct Bench {
    name: &'static str,
    /// ns/px normalizer (what one iteration touches) — see module docs.
    pixels: f64,
    run: fn() -> f64,
}

fn band_canvas() -> TinySkiaCanvas {
    TinySkiaCanvas::new(Rect {
        x: 0,
        y: 0,
        w: 120,
        h: 120,
    })
    .expect("pixmap")
}

const INK: Rgb = Rgb {
    r: 0x1E,
    g: 0x5A,
    b: 0xA8,
};

fn bench_fill_rrect() -> f64 {
    let mut c = band_canvas();
    measure(|| {
        c.fill_rrect(
            Rect {
                x: 20,
                y: 20,
                w: 64,
                h: 64,
            },
            8,
            INK,
            0xFF,
        )
    })
}

fn bench_stroke_rrect() -> f64 {
    let mut c = band_canvas();
    measure(|| {
        c.stroke_rrect(
            Rect {
                x: 20,
                y: 20,
                w: 64,
                h: 64,
            },
            8,
            2,
            INK,
            0xFF,
        )
    })
}

fn bench_disc() -> f64 {
    let mut c = band_canvas();
    measure(|| c.disc(60, 60, 24, INK, 0xFF))
}

fn bench_ring() -> f64 {
    let mut c = band_canvas();
    measure(|| c.ring(60, 60, 24, 4, INK, 0xFF))
}

fn bench_line() -> f64 {
    let mut c = band_canvas();
    measure(|| c.line(4, 4, 116, 116, 3, INK, 0xFF))
}

fn bench_gradient() -> f64 {
    let mut c = band_canvas();
    measure(|| {
        c.fill_linear_gradient(
            Rect {
                x: 20,
                y: 20,
                w: 64,
                h: 64,
            },
            4,
            INK,
            Rgb {
                r: 0xE0,
                g: 0x20,
                b: 0x20,
            },
            false,
            0xFF,
        )
    })
}

// --- F5 text benches -------------------------------------------------------
// Steady-state semantics ON PURPOSE: the cache is warmed before measuring,
// so these report the recurring per-frame cost (pure cached blits) — the
// number a frame budget needs. The one-time rasterization cost is a boot
// cost, measured on target in F9 (boot-time glyph-cache fill).

const BENCH_TEXT: &str = "Vyr glyph run 0123456789";
const BENCH_TEXT_PX: u32 = 14;

fn bench_fonts() -> Fonts {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../fonts/roboto.ttf");
    let bytes = std::fs::read(&path).expect("vendored fonts/roboto.ttf");
    let mut fonts = Fonts::new();
    fonts.register("roboto", bytes).expect("roboto parses");
    fonts
}

/// ns/px normalizer for text/glyph_run: the mask pixels one run blits
/// (coverage-carrying bbox pixels — what the loop actually visits).
fn glyph_run_pixels() -> f64 {
    let mut fonts = bench_fonts();
    fonts
        .prepare_run("roboto", BENCH_TEXT_PX, BENCH_TEXT)
        .expect("prepare");
    let placed = fonts
        .placed_run("roboto", BENCH_TEXT_PX, BENCH_TEXT, 4, 60)
        .expect("placed");
    placed.iter().map(|g| (g.mask.w * g.mask.h) as f64).sum()
}

fn bench_glyph_run() -> f64 {
    let mut fonts = bench_fonts();
    fonts
        .prepare_run("roboto", BENCH_TEXT_PX, BENCH_TEXT)
        .expect("prepare");
    let mut c = band_canvas();
    measure(|| {
        let placed = fonts
            .placed_run("roboto", BENCH_TEXT_PX, BENCH_TEXT, 4, 60)
            .expect("placed");
        c.glyph_run(&placed, INK, 0xFF);
    })
}

fn bench_text_scene() -> f64 {
    let mut fonts = bench_fonts();
    let mut buf = vec![0u8; (DEMO_W * DEMO_H * 3) as usize];
    measure(|| {
        vyr_core::render_with_fonts(
            TEXT_IR,
            &mut fonts,
            Rect {
                x: 0,
                y: 0,
                w: DEMO_W,
                h: DEMO_H,
            },
            &mut buf,
            (DEMO_W * 3) as usize,
        )
        .expect("text fixture renders");
    })
}

// --- F6 image benches --------------------------------------------------------
// Like text: register-once semantics (decode is a load/boot cost, F9 measures
// it on target); these report the recurring per-frame BLIT cost.

/// A deterministic 64×64 straight-alpha image: left half opaque, right half
/// a=128 — so the bench prices the blend mix a real asset has (the opaque
/// fast path AND the d255 source-over), not just the cheapest case.
fn synth_image_64() -> RgbaImage {
    let mut rgba = Vec::with_capacity(64 * 64 * 4);
    for y in 0..64u32 {
        for x in 0..64u32 {
            let a = if x < 32 { 0xFF } else { 0x80 };
            rgba.extend_from_slice(&[(x * 4) as u8, (y * 4) as u8, 0x80, a]);
        }
    }
    RgbaImage::new(64, 64, rgba).expect("valid synth image")
}

fn bench_blit_image() -> f64 {
    let img = synth_image_64();
    let clip = Rect {
        x: 20,
        y: 20,
        w: 64,
        h: 64,
    };
    let mut c = band_canvas();
    measure(|| c.blit_image(20, 20, &img, clip))
}

/// The committed F6 checker asset, registered under the fixture's src name
/// (what `tests/image_golden.rs` renders — golden and baseline measure the
/// SAME pixels).
fn bench_assets() -> Assets {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../vyr-core/tests/assets/checker-24.png");
    let file = std::fs::File::open(&path).expect("committed checker-24.png");
    let mut reader = png::Decoder::new(std::io::BufReader::new(file))
        .read_info()
        .expect("png header");
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf).expect("png decode");
    buf.truncate(info.buffer_size());
    let img = RgbaImage::new(info.width, info.height, buf).expect("valid dims");
    let mut assets = Assets::new();
    assets.register(IMAGE_ASSET, img).expect("register");
    assets
}

fn bench_image_scene() -> f64 {
    let assets = bench_assets();
    let mut fonts = Fonts::new();
    let mut buf = vec![0u8; (DEMO_W * DEMO_H * 3) as usize];
    measure(|| {
        vyr_core::render_with(
            IMAGE_IR,
            &mut fonts,
            &assets,
            Rect {
                x: 0,
                y: 0,
                w: DEMO_W,
                h: DEMO_H,
            },
            &mut buf,
            (DEMO_W * 3) as usize,
        )
        .expect("image fixture renders");
    })
}

fn bench_demo_scene() -> f64 {
    let mut buf = vec![0u8; (DEMO_W * DEMO_H * 3) as usize];
    measure(|| {
        let mut c = TinySkiaCanvas::new(Rect {
            x: 0,
            y: 0,
            w: DEMO_W,
            h: DEMO_H,
        })
        .expect("pixmap");
        demo_scene(&mut c);
        c.finish_into_rgb888(&mut buf, (DEMO_W * 3) as usize);
    })
}

fn bench_ir_scene() -> f64 {
    let mut buf = vec![0u8; (DEMO_W * DEMO_H * 3) as usize];
    measure(|| {
        vyr_core::render(
            DEMO_IR,
            Rect {
                x: 0,
                y: 0,
                w: DEMO_W,
                h: DEMO_H,
            },
            &mut buf,
            (DEMO_W * 3) as usize,
        )
        .expect("fixture renders");
    })
}

fn benches() -> Vec<Bench> {
    vec![
        Bench {
            name: "prim/fill_rrect_64",
            pixels: 64.0 * 64.0,
            run: bench_fill_rrect,
        },
        Bench {
            name: "prim/stroke_rrect_64",
            pixels: 64.0 * 64.0,
            run: bench_stroke_rrect,
        },
        Bench {
            name: "prim/disc_r24",
            pixels: 48.0 * 48.0,
            run: bench_disc,
        },
        Bench {
            name: "prim/ring_r24w4",
            pixels: 56.0 * 56.0,
            run: bench_ring,
        },
        Bench {
            name: "prim/line_diag_w3",
            pixels: 120.0 * 3.0,
            run: bench_line,
        },
        Bench {
            name: "prim/gradient_64",
            pixels: 64.0 * 64.0,
            run: bench_gradient,
        },
        Bench {
            name: "text/glyph_run",
            pixels: glyph_run_pixels(),
            run: bench_glyph_run,
        },
        Bench {
            name: "prim/blit_image_64",
            pixels: 64.0 * 64.0,
            run: bench_blit_image,
        },
        Bench {
            name: "scene/demo_full",
            pixels: (DEMO_W * DEMO_H) as f64,
            run: bench_demo_scene,
        },
        Bench {
            name: "scene/ir_full",
            pixels: (DEMO_W * DEMO_H) as f64,
            run: bench_ir_scene,
        },
        Bench {
            name: "scene/text_full",
            pixels: (DEMO_W * DEMO_H) as f64,
            run: bench_text_scene,
        },
        Bench {
            name: "scene/image_full",
            pixels: (DEMO_W * DEMO_H) as f64,
            run: bench_image_scene,
        },
    ]
}

/// Render the IR scene as ONE band of height `h` (full width); returns ns per
/// gutter-inclusive raster pixel — the I4 flatness quantity.
fn band_ns_per_raster_px(h: u32) -> f64 {
    let stride = (DEMO_W * 3) as usize;
    let mut buf = vec![0u8; stride * h as usize];
    let ns = measure(|| {
        vyr_core::render(
            DEMO_IR,
            Rect {
                x: 0,
                y: 60,
                w: DEMO_W,
                h,
            },
            &mut buf,
            stride,
        )
        .expect("band renders");
    });
    let raster_px = ((DEMO_W + 2 * GUTTER) * (h + 2 * GUTTER)) as f64;
    ns / raster_px
}

/// The I4 assertion: per-raster-pixel cost stays ~flat as bands shrink.
fn scaling_assertion() -> Result<Vec<String>, String> {
    let heights = [120u32, 60, 30, 15, 8];
    let full = band_ns_per_raster_px(120);
    let mut lines = Vec::new();
    let mut worst = 0.0f64;
    for &h in &heights {
        let nspx = band_ns_per_raster_px(h);
        let ratio = nspx / full;
        worst = worst.max(ratio);
        lines.push(format!(
            "scaling band_h={h:3}  {nspx:7.2} ns/raster-px  ({ratio:4.2}x of full)"
        ));
    }
    if worst > SCALING_X {
        return Err(format!(
            "scaling-law VIOLATION: worst band ns/raster-px is {worst:.2}x full-frame \
             (limit {SCALING_X}x) — a per-band fixed cost crept in"
        ));
    }
    Ok(lines)
}

fn baseline_path() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("baseline.json")
}

fn read_baseline() -> Option<std::collections::BTreeMap<String, f64>> {
    let text = std::fs::read_to_string(baseline_path()).ok()?;
    serde_json::from_str(&text).ok()
}

fn main() -> std::process::ExitCode {
    let mode = std::env::args().nth(1).unwrap_or_else(|| "run".into());
    log(&format!(
        "mode={mode} (warmup={WARMUP} rounds={ROUNDS} iters={ITERS})"
    ));

    let mut results = std::collections::BTreeMap::new();
    for b in benches() {
        let ns = (b.run)();
        let nspx = ns / b.pixels;
        results.insert(b.name.to_string(), nspx);
        log(&format!(
            "{:24} {:9.1} ns/iter  {:6.2} ns/px",
            b.name, ns, nspx
        ));
    }

    match scaling_assertion() {
        Ok(lines) => {
            for l in &lines {
                log(l);
            }
            log("scaling-law assertion OK");
        }
        Err(e) => {
            log(&e);
            return std::process::ExitCode::FAILURE;
        }
    }

    match mode.as_str() {
        "run" => std::process::ExitCode::SUCCESS,
        "record" => {
            let json = serde_json::to_string_pretty(&results).expect("serialize");
            std::fs::write(baseline_path(), json + "\n").expect("write baseline");
            log(&format!(
                "baseline RECORDED → {} (commit it as its own reviewed change)",
                baseline_path().display()
            ));
            std::process::ExitCode::SUCCESS
        }
        "check" => {
            let Some(base) = read_baseline() else {
                log("no baseline.json — record one first (vyr-bench record)");
                return std::process::ExitCode::FAILURE;
            };
            let mut failed = false;
            for (name, nspx) in &results {
                match base.get(name) {
                    Some(b) if *nspx > b * REGRESSION_X => {
                        failed = true;
                        log(&format!(
                            "REGRESSION {name}: {nspx:.2} ns/px vs baseline {b:.2} \
                             (> {REGRESSION_X}x)"
                        ));
                    }
                    Some(b) if *nspx * REGRESSION_X < *b => {
                        log(&format!(
                            "improvement {name}: {nspx:.2} ns/px vs baseline {b:.2} \
                             (consider re-recording)"
                        ));
                    }
                    Some(_) => {}
                    None => {
                        failed = true;
                        log(&format!(
                            "REGRESSION-GATE GAP: {name} has no baseline entry"
                        ));
                    }
                }
            }
            if failed {
                log("perf check FAILED");
                std::process::ExitCode::FAILURE
            } else {
                log("perf check OK (all within budget)");
                std::process::ExitCode::SUCCESS
            }
        }
        other => {
            eprintln!("usage: vyr-bench [run|record|check] (got {other:?})");
            std::process::ExitCode::from(2)
        }
    }
}

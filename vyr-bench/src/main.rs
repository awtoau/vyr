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

use std::sync::OnceLock;
use std::time::Instant;

use vyr_core::demo::{
    CLIP_FLAT_IR, CLIP_IR, DEMO_H, DEMO_IR, DEMO_W, IMAGE_ASSET, IMAGE_IR, PANEL_NEXT_IR,
    PANEL_PREV_IR, TEXT_IR, demo_scene,
};
use vyr_core::{
    Assets, Canvas, Fonts, Quality, Rect, Rgb, RgbaImage, Shapes, TinySkiaCanvas, dirty_rects,
};

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
const SCOPE_W: u32 = 320;
const SCOPE_H: u32 = 240;

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
    let mut fonts = Fonts::new();
    fonts
        .register("roboto", include_bytes!("../../fonts/roboto.ttf").to_vec())
        .expect("roboto parses");
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
    // dst == clip: a 64×64 asset fit into a 64×64 box is 1:1 (the bench keeps
    // measuring the per-pixel blit; fit-to-cell adds only an integer src map).
    measure(|| c.blit_image(clip, &img, clip))
}

/// The committed F6 checker asset, registered under the fixture's src name
/// (what `tests/image_golden.rs` renders — golden and baseline measure the
/// SAME pixels).
fn bench_assets() -> Assets {
    let mut reader = png::Decoder::new(std::io::Cursor::new(include_bytes!(
        "../../vyr-core/tests/assets/checker-24.png"
    )))
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

// --- F3 clip + dirty benches -------------------------------------------------

/// Render one of the clip-pair scenes full-frame (fonts + checker assets —
/// the same inputs the clip golden uses).
fn clip_scene_ns(ir: &'static str) -> f64 {
    let mut fonts = bench_fonts();
    let assets = bench_assets();
    let mut buf = vec![0u8; (DEMO_W * DEMO_H * 3) as usize];
    measure(|| {
        vyr_core::render_with(
            ir,
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
        .expect("clip scene renders");
    })
}

/// Clipped containers with overflowing children: mask builds, masked draws
/// and fate checks included. `scene/clip_flat` is the same op mix with zero
/// clip pushes — the delta IS the clip stack's price.
fn bench_clip_scene() -> f64 {
    clip_scene_ns(CLIP_IR)
}

fn bench_clip_flat() -> f64 {
    clip_scene_ns(CLIP_FLAT_IR)
}

const PANEL_W: u32 = 480;
const PANEL_H: u32 = 320;

/// Full render of the 480×320 panel (NEXT state) — the dirty bench's
/// full-frame reference.
fn bench_panel_full() -> f64 {
    let mut fonts = bench_fonts();
    let mut buf = vec![0u8; (PANEL_W * PANEL_H * 3) as usize];
    measure(|| {
        vyr_core::render_with_fonts(
            PANEL_NEXT_IR,
            &mut fonts,
            Rect {
                x: 0,
                y: 0,
                w: PANEL_W,
                h: PANEL_H,
            },
            &mut buf,
            (PANEL_W * 3) as usize,
        )
        .expect("panel renders");
    })
}

/// The headline incremental win: one toggle flips on the panel; repaint only
/// the dirty regions onto the previous frame. Iterations after the first
/// repaint identical regions over identical pixels (B over B) — same ops,
/// same cost, deterministic timing. `tests/dirty_rects.rs` proves this very
/// pair byte-identical to the full render.
fn bench_panel_dirty_incr() -> f64 {
    let mut fonts = bench_fonts();
    let prev = vyr_core::ir::Request::parse(PANEL_PREV_IR).expect("prev parses");
    let next = vyr_core::ir::Request::parse(PANEL_NEXT_IR).expect("next parses");
    let assets = Assets::new();
    let stride = (PANEL_W * 3) as usize;
    let mut frame = vec![0u8; stride * PANEL_H as usize];
    prev.render_with(
        &mut fonts,
        &assets,
        Rect {
            x: 0,
            y: 0,
            w: PANEL_W,
            h: PANEL_H,
        },
        &mut frame,
        stride,
    )
    .expect("prev frame renders");
    measure(|| {
        next.render_incremental(&prev, &mut fonts, &assets, &mut frame, stride)
            .expect("incremental renders");
    })
}

/// ns/px normalizer for the incremental bench: the merged dirty area (what
/// one iteration actually repaints — diff cost rides on those pixels).
fn panel_dirty_pixels() -> f64 {
    let prev = vyr_core::ir::Request::parse(PANEL_PREV_IR).expect("prev parses");
    let next = vyr_core::ir::Request::parse(PANEL_NEXT_IR).expect("next parses");
    dirty_rects(&prev, &next)
        .iter()
        .map(|r| r.w as f64 * r.h as f64)
        .sum()
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

// --- F16 Draft-tier benches (#16) ------------------------------------------
// The Exact vs Draft per-pixel comparison: same fixture, same band, two
// quality tiers. The DEMO_IR scene is text/image-free (empty registries are
// honest); as of the v3 set EVERY op takes the integer fast path — opaque +
// translucent rects, the rounded fills + rounded strokes (hard corners), the
// curve primitives (disc/ring/line), and the gradient (integer ramp) — so the
// scene hits 100% fast-path coverage and touches no tiny-skia. v3 also drops
// the 8 px gutter for Draft (no AA ⇒ no overscan), ~halving the per-band raster
// + finish cost. The scene number is the realistic blended speedup.

fn render_demo_quality(quality: Quality) -> f64 {
    let mut fonts = Fonts::new();
    let assets = Assets::new();
    let mut buf = vec![0u8; (DEMO_W * DEMO_H * 3) as usize];
    measure(|| {
        vyr_core::render_with_quality(
            DEMO_IR,
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
            quality,
        )
        .expect("demo fixture renders");
    })
}

fn bench_ir_scene_exact() -> f64 {
    render_demo_quality(Quality::Exact)
}

fn bench_ir_scene_draft() -> f64 {
    render_demo_quality(Quality::Draft)
}

/// F16 `Quality::Fast` (#27) — the middle tier's blended scene cost. Read it
/// as `(fast - draft) / (exact - draft)`: the fraction of the AA bill Fast
/// still pays, which is exactly what anti-aliasing the curves costs.
fn bench_ir_scene_fast() -> f64 {
    render_demo_quality(Quality::Fast)
}

/// The 480x270 PANEL scene at one tier — the REPRESENTATIVE frame (it is the
/// shape of the M4/LVGL fixture: a lot of flat area, a few curved widgets),
/// where the 120x120 DEMO_IR is deliberately curve-dense. The middle tier's
/// verdict differs sharply between the two, and quoting only one of them
/// would be picking the answer.
fn render_panel_quality(quality: Quality) -> f64 {
    let mut fonts = bench_fonts();
    let assets = Assets::new();
    let mut buf = vec![0u8; (PANEL_W * PANEL_H * 3) as usize];
    measure(|| {
        vyr_core::render_with_quality(
            PANEL_NEXT_IR,
            &mut fonts,
            &assets,
            Rect {
                x: 0,
                y: 0,
                w: PANEL_W,
                h: PANEL_H,
            },
            &mut buf,
            (PANEL_W * 3) as usize,
            quality,
        )
        .expect("panel renders");
    })
}

fn bench_panel_exact() -> f64 {
    render_panel_quality(Quality::Exact)
}

fn bench_panel_fast() -> f64 {
    render_panel_quality(Quality::Fast)
}

fn bench_panel_draft() -> f64 {
    render_panel_quality(Quality::Draft)
}

/// Band height for the #32 memo benches — the M4 workload's own
/// (`vyr_size::workload::BAND_H`), so the host number is measuring the same
/// repetition factor the MCU pays.
const MEMO_BAND_H: u32 = 16;

/// **#32: the flattened-contour memo, priced.** One 480x320 panel frame as
/// 16-row bands, either carrying ONE `Shapes` across every band (`memo`) or
/// handing each band a zero-budget one — which admits nothing and is therefore
/// bit-for-bit the pre-#32 painter.
///
/// The host is the WEAK case on purpose: x86-64 has hardware f64, so `cosf`
/// costs a few cycles here and ~1,145 instructions on an M4F, where `libm`
/// promotes to f64 and the FPU is single-precision. Whatever this pair shows
/// on a desktop, the MCU shows several times more — which is exactly why the
/// bill went unnoticed until it was measured on the part
/// (`docs/measurements/lvgl-gap.md` §2.2). Keeping both here means a future
/// change that quietly stops the memo hitting shows up on the machine the
/// gate runs on.
fn render_panel_banded(memo: bool) -> f64 {
    let mut fonts = bench_fonts();
    let assets = Assets::new();
    let stride = (PANEL_W * 3) as usize;
    let mut buf = vec![0u8; stride * PANEL_H as usize];
    let mut carried = Shapes::new();
    measure(|| {
        let mut y = 0;
        while y < PANEL_H {
            let h = MEMO_BAND_H.min(PANEL_H - y);
            let band = &mut buf[y as usize * stride..(y + h) as usize * stride];
            let mut fresh = Shapes::with_budget(0);
            let shapes = if memo { &mut carried } else { &mut fresh };
            vyr_core::render_with_shapes(
                PANEL_NEXT_IR,
                &mut fonts,
                &assets,
                shapes,
                Rect {
                    x: 0,
                    y: y as i32,
                    w: PANEL_W,
                    h,
                },
                band,
                stride,
                Quality::Exact,
            )
            .expect("panel band renders");
            y += h;
        }
    })
}

fn bench_panel_banded_memo() -> f64 {
    render_panel_banded(true)
}

fn bench_panel_banded_plain() -> f64 {
    render_panel_banded(false)
}

/// The gauge ring — the #27 curve — at one tier, on its own band canvas.
/// `prim/ring_{exact,fast,draft}` is the per-op price list for the tier
/// decision: Fast must equal Exact here (it routes the curve to the same
/// tiny-skia polygon) and Draft must be far below both.
fn bench_ring_quality(quality: Quality) -> f64 {
    let mut c = TinySkiaCanvas::new_with_quality(
        Rect {
            x: 0,
            y: 0,
            w: 120,
            h: 120,
        },
        quality,
    )
    .expect("pixmap");
    // Seed an opaque backdrop: the Draft/Fast rgb surface composites over it,
    // so the measured op is a source-over onto real pixels, not onto zeroes.
    c.fill_rrect(
        Rect {
            x: 0,
            y: 0,
            w: 120,
            h: 120,
        },
        0,
        Rgb {
            r: 0x22,
            g: 0x26,
            b: 0x2B,
        },
        0xFF,
    );
    measure(|| c.ring(60, 60, 24, 4, INK, 0xFF))
}

fn bench_ring_exact() -> f64 {
    bench_ring_quality(Quality::Exact)
}

fn bench_ring_fast() -> f64 {
    bench_ring_quality(Quality::Fast)
}

fn bench_ring_draft() -> f64 {
    bench_ring_quality(Quality::Draft)
}

// --- Scope benches ---------------------------------------------------------
// Dedicated dense scope-like workload to compare decimation modes in the same
// renderer path and quality tier.

fn scope_points_csv() -> &'static str {
    static CSV: OnceLock<String> = OnceLock::new();
    CSV.get_or_init(|| {
        let mut vals = Vec::with_capacity(2400);
        for i in 0..2400i32 {
            let base = (i * 37 + i / 7) % 101;
            let spike = if i % 251 < 3 { 35 } else { 0 };
            let dip = if i % 389 < 2 { -28 } else { 0 };
            vals.push((base + spike + dip).clamp(0, 100).to_string());
        }
        vals.join(",")
    })
}

fn scope_ir(decimate: &str) -> String {
    format!(
        r##"{{"w":320,"h":240,"root":{{"name":"view","attrs":{{"background":"#0B0F14"}},"children":[
          {{"name":"vy_frame","attrs":{{"x":"6","y":"6","width":"308","height":"228",
            "background":"#0F1620","border_width":"1","border_color":"#2A3E55","radius":"0"}}}},
          {{"name":"vy_chart","attrs":{{"x":"10","y":"10","width":"300","height":"220",
            "mode":"scope","decimate":"{}","show_background":"0","show_grid":"1","show_frame":"1",
            "show_markers":"0","div_count_x":"9","div_count_y":"7","line_width":"1",
            "line_color":"#65FF9B","range_min":"0","range_max":"100","points":"{}"}}}}
        ]}}}}"##,
        decimate,
        scope_points_csv()
    )
}

fn scope_ir_none() -> &'static str {
    static IR: OnceLock<String> = OnceLock::new();
    IR.get_or_init(|| scope_ir("none"))
}

fn scope_ir_minmax() -> &'static str {
    static IR: OnceLock<String> = OnceLock::new();
    IR.get_or_init(|| scope_ir("minmax"))
}

fn bench_scope_scene_draft_none() -> f64 {
    let mut fonts = Fonts::new();
    let assets = Assets::new();
    let mut buf = vec![0u8; (SCOPE_W * SCOPE_H * 3) as usize];
    measure(|| {
        vyr_core::render_with_quality(
            scope_ir_none(),
            &mut fonts,
            &assets,
            Rect {
                x: 0,
                y: 0,
                w: SCOPE_W,
                h: SCOPE_H,
            },
            &mut buf,
            (SCOPE_W * 3) as usize,
            Quality::Draft,
        )
        .expect("scope none renders");
    })
}

fn bench_scope_scene_draft_minmax() -> f64 {
    let mut fonts = Fonts::new();
    let assets = Assets::new();
    let mut buf = vec![0u8; (SCOPE_W * SCOPE_H * 3) as usize];
    measure(|| {
        vyr_core::render_with_quality(
            scope_ir_minmax(),
            &mut fonts,
            &assets,
            Rect {
                x: 0,
                y: 0,
                w: SCOPE_W,
                h: SCOPE_H,
            },
            &mut buf,
            (SCOPE_W * 3) as usize,
            Quality::Draft,
        )
        .expect("scope minmax renders");
    })
}

/// One opaque radius-0 rect at Draft — the pure integer span-fill cost, the
/// micro-bench twin of the Exact `prim/fill_rrect_64` (which uses radius 8).
/// This is the raw lever: an opaque axis-aligned fill with no AA.
fn bench_fill_rect_draft() -> f64 {
    let mut c = TinySkiaCanvas::new_with_quality(
        Rect {
            x: 0,
            y: 0,
            w: 120,
            h: 120,
        },
        Quality::Draft,
    )
    .expect("pixmap");
    measure(|| {
        c.fill_rrect(
            Rect {
                x: 20,
                y: 20,
                w: 64,
                h: 64,
            },
            0,
            INK,
            0xFF,
        )
    })
}

/// The Exact twin of [`bench_fill_rect_draft`] — radius 0, alpha 255, so the
/// ONLY difference vs Draft is the AA path. The ratio of the two ns/px is the
/// honest per-op lever the fast path pulls.
fn bench_fill_rect_exact() -> f64 {
    let mut c = band_canvas();
    measure(|| {
        c.fill_rrect(
            Rect {
                x: 20,
                y: 20,
                w: 64,
                h: 64,
            },
            0,
            INK,
            0xFF,
        )
    })
}

/// Render DEMO_IR full-frame at `quality` and return the RGB888 bytes —
/// the fidelity-delta source.
fn demo_bytes(quality: Quality) -> Vec<u8> {
    let mut fonts = Fonts::new();
    let assets = Assets::new();
    let mut buf = vec![0u8; (DEMO_W * DEMO_H * 3) as usize];
    vyr_core::render_with_quality(
        DEMO_IR,
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
        quality,
    )
    .expect("demo renders");
    buf
}

/// The #21 honest fidelity delta: Draft vs Exact on the SAME fixture —
/// differing-pixel count + %, max single-channel error, and the fast-path
/// coverage % (how much of the frame the integer path actually carried).
/// Logged, not gated (Draft is its own tier; it is NEVER asserted == Exact).
fn log_fast_fidelity() {
    // #27: the middle tier's fidelity, stated the way the issue demands —
    // against BOTH neighbours, same renderer, same scene.
    let exact = demo_bytes(Quality::Exact);
    let fast = demo_bytes(Quality::Fast);
    let draft = demo_bytes(Quality::Draft);
    let total_px = (DEMO_W * DEMO_H) as u64;
    let count = |a: &[u8], b: &[u8]| -> (u64, u8) {
        let mut n = 0u64;
        let mut m = 0u8;
        for (x, y) in a.chunks_exact(3).zip(b.chunks_exact(3)) {
            if x != y {
                n += 1;
                for k in 0..3 {
                    m = m.max(x[k].abs_diff(y[k]));
                }
            }
        }
        (n, m)
    };
    let (fe, fem) = count(&fast, &exact);
    let (fd, fdm) = count(&fast, &draft);
    let mut fonts = Fonts::new();
    let assets = Assets::new();
    let mut buf = vec![0u8; (DEMO_W * DEMO_H * 3) as usize];
    let stats = vyr_core::render_with_quality(
        DEMO_IR,
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
        Quality::Fast,
    )
    .expect("demo renders");
    log(&format!(
        "#27 Fast fidelity (scene/ir_full): vs Exact {fe}/{total_px} px differ \
         (max channel {fem}/255); vs Draft {fd}/{total_px} px differ (max channel {fdm}/255); \
         integer fast-path coverage {}/{} delivered px ({:.1}%) — the shortfall IS the \
         anti-aliased curve area",
        stats.fastpath_pixels,
        stats.pixels_written,
        100.0 * stats.fastpath_pixels as f64 / stats.pixels_written as f64,
    ));
}

fn log_draft_fidelity() {
    let exact = demo_bytes(Quality::Exact);
    let draft = demo_bytes(Quality::Draft);
    let mut diff_px = 0u64;
    let mut max_chan = 0u8;
    for (e, d) in exact.chunks_exact(3).zip(draft.chunks_exact(3)) {
        if e != d {
            diff_px += 1;
            for k in 0..3 {
                max_chan = max_chan.max(e[k].abs_diff(d[k]));
            }
        }
    }
    let total_px = (DEMO_W * DEMO_H) as u64;
    // Fast-path coverage from the stats (a second render — cheap, one frame).
    let mut fonts = Fonts::new();
    let assets = Assets::new();
    let mut buf = vec![0u8; (DEMO_W * DEMO_H * 3) as usize];
    let stats = vyr_core::render_with_quality(
        DEMO_IR,
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
        Quality::Draft,
    )
    .expect("demo renders");
    log(&format!(
        "F16 Draft fidelity (scene/ir_full): {diff_px}/{total_px} px differ ({:.1}%), \
         max channel error {max_chan}/255; fast-path coverage {}/{} delivered px ({:.1}%) \
         — v3 is fully integer (no tiny-skia); the differing pixels are the AA fringe \
         Exact smooths and Draft's hard rounded edges do not",
        100.0 * diff_px as f64 / total_px as f64,
        stats.fastpath_pixels,
        stats.pixels_written,
        100.0 * stats.fastpath_pixels as f64 / stats.pixels_written as f64,
    ));
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
        // F16 (#16): the same scene at Exact vs Draft — the blended per-pixel
        // speedup (opaque rects fast, curves fall back), plus the raw opaque
        // radius-0 fill lever both ways.
        Bench {
            name: "scene/ir_full_exact",
            pixels: (DEMO_W * DEMO_H) as f64,
            run: bench_ir_scene_exact,
        },
        Bench {
            name: "scene/ir_full_draft",
            pixels: (DEMO_W * DEMO_H) as f64,
            run: bench_ir_scene_draft,
        },
        Bench {
            name: "scene/panel_exact",
            pixels: (PANEL_W * PANEL_H) as f64,
            run: bench_panel_exact,
        },
        Bench {
            name: "scene/panel_fast",
            pixels: (PANEL_W * PANEL_H) as f64,
            run: bench_panel_fast,
        },
        Bench {
            name: "scene/panel_draft",
            pixels: (PANEL_W * PANEL_H) as f64,
            run: bench_panel_draft,
        },
        // #32: the contour memo, both sides of it. `plain` is the pre-#32
        // painter (zero-budget cache ⇒ re-flatten every band), `memo` is one
        // cache carried across the 20 bands.
        Bench {
            name: "scene/panel_banded_plain",
            pixels: (PANEL_W * PANEL_H) as f64,
            run: bench_panel_banded_plain,
        },
        Bench {
            name: "scene/panel_banded_memo",
            pixels: (PANEL_W * PANEL_H) as f64,
            run: bench_panel_banded_memo,
        },
        Bench {
            name: "scene/ir_full_fast",
            pixels: (DEMO_W * DEMO_H) as f64,
            run: bench_ir_scene_fast,
        },
        // #27: the curve primitive priced at all three tiers. The ring bbox
        // (radius 24 + width 4 ⇒ 52x52) is the ns/px normalizer.
        Bench {
            name: "prim/ring_exact",
            pixels: 52.0 * 52.0,
            run: bench_ring_exact,
        },
        Bench {
            name: "prim/ring_fast",
            pixels: 52.0 * 52.0,
            run: bench_ring_fast,
        },
        Bench {
            name: "prim/ring_draft",
            pixels: 52.0 * 52.0,
            run: bench_ring_draft,
        },
        Bench {
            name: "prim/fill_rect0_exact",
            pixels: 64.0 * 64.0,
            run: bench_fill_rect_exact,
        },
        Bench {
            name: "prim/fill_rect0_draft",
            pixels: 64.0 * 64.0,
            run: bench_fill_rect_draft,
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
        Bench {
            name: "scene/clip_full",
            pixels: (DEMO_W * DEMO_H) as f64,
            run: bench_clip_scene,
        },
        Bench {
            name: "scene/clip_flat",
            pixels: (DEMO_W * DEMO_H) as f64,
            run: bench_clip_flat,
        },
        Bench {
            name: "scene/panel_full",
            pixels: (PANEL_W * PANEL_H) as f64,
            run: bench_panel_full,
        },
        Bench {
            name: "scene/panel_dirty_incr",
            pixels: panel_dirty_pixels(),
            run: bench_panel_dirty_incr,
        },
        Bench {
            name: "scene/scope_full_none_draft",
            pixels: (SCOPE_W * SCOPE_H) as f64,
            run: bench_scope_scene_draft_none,
        },
        Bench {
            name: "scene/scope_full_minmax_draft",
            pixels: (SCOPE_W * SCOPE_H) as f64,
            run: bench_scope_scene_draft_minmax,
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

    // `vyr-bench only <substring>` runs just the matching rows, in a tight
    // loop, and prints nothing else — the shape `perf record` needs to
    // attribute cost to ONE tier's render path (#27) instead of to the
    // 30-bench average.
    if mode == "only" {
        let want = std::env::args().nth(2).unwrap_or_default();
        for b in benches() {
            if !b.name.contains(&want) {
                continue;
            }
            let ns = (b.run)();
            log(&format!("{:24} {:9.1} ns/iter", b.name, ns));
        }
        return std::process::ExitCode::SUCCESS;
    }

    let mut results = std::collections::BTreeMap::new();
    let mut raw_ns = std::collections::BTreeMap::new();
    for b in benches() {
        let ns = (b.run)();
        let nspx = ns / b.pixels;
        results.insert(b.name.to_string(), nspx);
        raw_ns.insert(b.name.to_string(), ns);
        log(&format!(
            "{:24} {:9.1} ns/iter  {:6.2} ns/px",
            b.name, ns, nspx
        ));
    }

    // The runtime-story headlines, spelled out (raw per-frame ns — the
    // ns/px rows above normalize differently per bench).
    if let (Some(full), Some(incr)) = (
        raw_ns.get("scene/panel_full"),
        raw_ns.get("scene/panel_dirty_incr"),
    ) {
        log(&format!(
            "dirty-rect incremental win: panel full {:.0} ns vs dirty {:.0} ns = {:.1}x \
             (480x320, one toggle flips, diff included)",
            full,
            incr,
            full / incr
        ));
    }
    if let (Some(clip), Some(flat)) = (raw_ns.get("scene/clip_full"), raw_ns.get("scene/clip_flat"))
    {
        log(&format!(
            "clip-stack overhead: clipped {:.0} ns vs flat {:.0} ns = {:+.1}% \
             (same op mix; masks + fate checks are the delta)",
            clip,
            flat,
            (clip / flat - 1.0) * 100.0
        ));
    }

    // F16 (#16) headlines: the Draft tier's measured value, in both the
    // per-op lever and the blended-scene form, plus the fidelity it costs.
    if let (Some(re), Some(rd)) = (
        results.get("prim/fill_rect0_exact"),
        results.get("prim/fill_rect0_draft"),
    ) {
        log(&format!(
            "F16 Draft lever (opaque radius-0 fill): Exact {re:.2} ns/px vs Draft {rd:.2} ns/px \
             = {:.1}x faster per pixel (integer span fill, no AA)",
            re / rd
        ));
    }
    if let (Some(se), Some(sd)) = (
        raw_ns.get("scene/ir_full_exact"),
        raw_ns.get("scene/ir_full_draft"),
    ) {
        log(&format!(
            "F16 Draft scene (DEMO_IR full frame): Exact {se:.0} ns vs Draft {sd:.0} ns \
             = {:.2}x faster (v3 — all ops integer: rects/rounded fills+strokes/curves/gradient, \
             gutter-off, 100% fast-path, no tiny-skia)",
            se / sd
        ));
    }
    // #27: where the middle tier lands between its two neighbours. The
    // fraction is the whole verdict — 0% means Fast is Draft-priced, 100%
    // means anti-aliasing the curves cost the entire Exact bill.
    if let (Some(se), Some(sf), Some(sd)) = (
        raw_ns.get("scene/panel_exact"),
        raw_ns.get("scene/panel_fast"),
        raw_ns.get("scene/panel_draft"),
    ) {
        let frac = 100.0 * (sf - sd) / (se - sd);
        log(&format!(
            "#27 Fast tier (PANEL 480x270, the representative frame): Exact {se:.0} ns / \
             Fast {sf:.0} ns / Draft {sd:.0} ns — Fast is {:.2}x Draft and {:.2}x cheaper \
             than Exact, i.e. {frac:.0}% of the Exact-over-Draft bill",
            sf / sd,
            se / sf
        ));
    }
    if let (Some(se), Some(sf), Some(sd)) = (
        raw_ns.get("scene/ir_full_exact"),
        raw_ns.get("scene/ir_full_fast"),
        raw_ns.get("scene/ir_full_draft"),
    ) {
        let frac = 100.0 * (sf - sd) / (se - sd);
        log(&format!(
            "#27 Fast tier (DEMO_IR 120x120, curve-dense): Exact {se:.0} ns / Fast {sf:.0} ns / \
             Draft {sd:.0} ns — Fast is {:.2}x Draft and {:.2}x cheaper than Exact, \
             i.e. it pays {frac:.0}% of the Exact-over-Draft bill to anti-alias the curves",
            sf / sd,
            se / sf
        ));
    }
    if let (Some(re), Some(rf), Some(rd)) = (
        raw_ns.get("prim/ring_exact"),
        raw_ns.get("prim/ring_fast"),
        raw_ns.get("prim/ring_draft"),
    ) {
        log(&format!(
            "#27 Fast curve primitive (ring r24 w4): Exact {re:.0} ns / Fast {rf:.0} ns / \
             Draft {rd:.0} ns — Fast pays Exact's price for the curve itself \
             (+{:.0}% for the rgb scratch round-trip), which is the tier's whole cost model",
            100.0 * (rf - re) / re
        ));
    }
    if let (Some(sn), Some(sm)) = (
        raw_ns.get("scene/scope_full_none_draft"),
        raw_ns.get("scene/scope_full_minmax_draft"),
    ) {
        let speedup = sn / sm;
        log(&format!(
            "scope decimation A/B (Draft, dense 320x240 scope): none {:.0} ns vs minmax {:.0} ns = {:.2}x faster with minmax",
            sn, sm, speedup
        ));
    }
    if let (Some(plain), Some(memo)) = (
        raw_ns.get("scene/panel_banded_plain"),
        raw_ns.get("scene/panel_banded_memo"),
    ) {
        log(&format!(
            "#32 contour memo (PANEL 480x320 Exact, {MEMO_BAND_H}-row bands): \
             re-flatten {plain:.0} ns vs memoised {memo:.0} ns = {:.2}x, {:+.1}% of the frame. \
             Host only — x86-64 has hardware f64; on the M4F the same memo is worth \
             13.1 M of 64.4 M insns/frame (20.3%) because libm computes f32 trig in f64",
            plain / memo,
            100.0 * (memo - plain) / plain,
        ));
    }
    log_draft_fidelity();
    log_fast_fidelity();

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
                        // RETRY-CONFIRM: a regression must REPRODUCE to fail.
                        // Micro-benches with ~ms total windows can be poisoned
                        // by a single scheduler/turbo blip even through the
                        // median (observed: glyph_run 1.4→2.8 ns/px right
                        // after a fresh compile, clean on re-run). One re-run
                        // of just the offender keeps the gate honest without
                        // making it flaky.
                        let again = benches()
                            .into_iter()
                            .find(|c| c.name == name)
                            .map(|c| (c.run)() / c.pixels);
                        match again {
                            Some(second) if second > b * REGRESSION_X => {
                                failed = true;
                                log(&format!(
                                    "REGRESSION {name}: {nspx:.2} then {second:.2} ns/px \
                                     vs baseline {b:.2} (> {REGRESSION_X}x, reproduced)"
                                ));
                            }
                            Some(second) => {
                                log(&format!(
                                    "noise {name}: {nspx:.2} ns/px did not reproduce \
                                     ({second:.2} on retry, baseline {b:.2}) — not a failure"
                                ));
                            }
                            None => {
                                failed = true;
                                log(&format!("REGRESSION {name}: bench vanished on retry"));
                            }
                        }
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

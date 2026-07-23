//! vyr-cli — the std shell. IR JSON in → PNG out (the farm contract), plus a
//! `selftest-png` mode that renders the shared demo scene (the exact golden
//! pixels) AND the F5 text fixture so "does the painter+text work here" is a
//! one-command question.
//!
//! Fonts (F5): core never touches the filesystem (invariant I7) — THIS shell
//! loads every `*.ttf`/`*.otf` from the fonts dir at startup and registers
//! the bytes with `vyr_core::Fonts` (registry name = file stem, lowercased).
//! Dir resolution: `$VYR_FONTS` if set, else `<this crate's repo>/fonts`
//! baked in via `CARGO_MANIFEST_DIR` — so both the standalone repo build and
//! the awto-vyvanse submodule build (whose checkout carries its own fonts/)
//! find their fonts with NO environment setup, exactly what the render farm
//! needs. A missing dir is a loud WARN, and text then hard-errors honestly.
//!
//! Images (F6): same I7 split — THIS shell walks the parsed request for
//! `src` attrs, decodes each PNG (the `png` crate) and registers the RGBA
//! pixels with `vyr_core::Assets` under the VERBATIM `src` string (core
//! looks names up exactly as the IR carries them). Path resolution per src:
//! a leading LVGL `A:` FS-driver prefix is stripped DEFENSIVELY (the farm
//! sends vyr the plain path; the prefix is LVGL-specific lowering), then an
//! absolute path is used as-is, else relative to `$VYR_ASSETS` if set, else
//! relative to the CWD (the farm's vyr_server runs with cwd = the vyvanse
//! repo root, so repo-relative srcs resolve). A missing or undecodable
//! asset is a NAMED hard error BEFORE any pixel (invariant I6) — exit 2,
//! the farm's honest-FAIL contract, never a blank box.
//!
//! Logging (awto convention): every line timestamped, mirrored to BOTH
//! stderr (live) and ./tmp/vyr-cli.log (retroactive review, append). Format:
//! `HH:MM:SS.ffffff UTC  LEVEL [vyr-cli] message`.
//!
//! Heap accounting (Dan's memory-profile ask): a counting global allocator
//! wraps the system one — exact bytes, exact peak, zero sampling — and every
//! render logs `heap peak` + fills `RenderStats.peak_alloc_bytes`, so the
//! memory story is MEASURED on every run, not modelled. (`unsafe` lives in
//! this shell only — vyr-core stays `forbid(unsafe_code)`.)

use std::alloc::{GlobalAlloc, Layout, System};
use std::io::Write as _;
use std::process::ExitCode;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use vyr_core::demo::{DEMO_H, DEMO_W, TEXT_IR, demo_scene};
use vyr_core::{Assets, Fonts, Rect, RgbaImage, TinySkiaCanvas};

/// Counting allocator: tracks live bytes + high-water mark around the system
/// allocator. Relaxed ordering is fine — the counters are statistics, and
/// the render path is single-threaded.
struct CountingAlloc;

static HEAP_LIVE: AtomicUsize = AtomicUsize::new(0);
static HEAP_PEAK: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for CountingAlloc {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let p = unsafe { System.alloc(layout) };
        if !p.is_null() {
            let live = HEAP_LIVE.fetch_add(layout.size(), Ordering::Relaxed) + layout.size();
            HEAP_PEAK.fetch_max(live, Ordering::Relaxed);
        }
        p
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { System.dealloc(ptr, layout) };
        HEAP_LIVE.fetch_sub(layout.size(), Ordering::Relaxed);
    }
}

#[global_allocator]
static ALLOC: CountingAlloc = CountingAlloc;

fn heap_now() -> (usize, usize) {
    (
        HEAP_LIVE.load(Ordering::Relaxed),
        HEAP_PEAK.load(Ordering::Relaxed),
    )
}

/// The baked default fonts dir: the repo this binary was built from. Works
/// unmoved for both the standalone repo and the vyvanse submodule checkout;
/// `$VYR_FONTS` overrides for a relocated binary.
const BAKED_FONTS_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../fonts");

fn timestamp() -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
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

fn log(level: &str, msg: &str) {
    let line = format!("{}  {:5} [vyr-cli] {}", timestamp(), level, msg);
    eprintln!("{line}");
    // Workspace-relative ./tmp/ per the awto logging rule; best-effort (a
    // read-only cwd must not turn logging into a crash).
    if std::fs::create_dir_all("tmp").is_ok()
        && let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open("tmp/vyr-cli.log")
    {
        let _ = writeln!(f, "{line}");
    }
}

/// Load + register every `*.ttf` / `*.otf` in the fonts dir (module docs).
/// Returns the registry; an unreadable dir is a WARN (text will then
/// hard-error per honest failure), an unparseable font file is an ERROR.
fn load_fonts() -> Fonts {
    let dir = std::env::var("VYR_FONTS").unwrap_or_else(|_| BAKED_FONTS_DIR.to_string());
    let mut fonts = Fonts::new();
    let entries = match std::fs::read_dir(&dir) {
        Ok(e) => e,
        Err(e) => {
            log(
                "WARN",
                &format!("fonts dir {dir} unreadable ({e}) — text renders will hard-error"),
            );
            return fonts;
        }
    };
    let mut names: Vec<String> = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        let ext = path
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| e.to_ascii_lowercase());
        if !matches!(ext.as_deref(), Some("ttf") | Some("otf")) {
            continue;
        }
        let Some(stem) = path.file_stem().and_then(|s| s.to_str()) else {
            continue;
        };
        match std::fs::read(&path) {
            Ok(bytes) => {
                let n = bytes.len();
                match fonts.register(stem, bytes) {
                    Ok(()) => names.push(format!("{} ({n} B)", stem.to_lowercase())),
                    Err(e) => log("ERROR", &format!("register {}: {e:?}", path.display())),
                }
            }
            Err(e) => log("ERROR", &format!("read {}: {e}", path.display())),
        }
    }
    log("INFO", &format!("fonts from {dir}: [{}]", names.join(", ")));
    fonts
}

/// Collect every `src` attr in the request tree — VERBATIM strings, deduped,
/// first-seen order (the registry-key contract: core looks up exactly what
/// the IR carries).
fn collect_srcs(n: &vyr_core::ir::Node, out: &mut Vec<String>) {
    if let Some(s) = n.str_attr("src")
        && !out.contains(&s)
    {
        out.push(s);
    }
    for c in &n.children {
        collect_srcs(c, out);
    }
}

/// Resolve a verbatim IR `src` to a filesystem path (module docs): strip a
/// defensive LVGL `A:` FS-driver prefix, then absolute as-is, else relative
/// to `$VYR_ASSETS`, else relative to the CWD (the farm server's cwd is the
/// vyvanse repo root).
fn resolve_src(src: &str) -> std::path::PathBuf {
    let path = std::path::Path::new(src.strip_prefix("A:").unwrap_or(src));
    if path.is_absolute() {
        return path.to_path_buf();
    }
    match std::env::var("VYR_ASSETS") {
        Ok(base) => std::path::Path::new(&base).join(path),
        Err(_) => path.to_path_buf(),
    }
}

/// Decode one PNG to straight RGBA8888. EXPAND+STRIP_16 normalises palette /
/// sub-byte / 16-bit sources to 8-bit channels; the remaining colour types
/// are widened to RGBA here so core sees ONE pixel format (its contract).
fn decode_png(path: &std::path::Path) -> Result<RgbaImage, String> {
    let file = std::fs::File::open(path).map_err(|e| format!("open: {e}"))?;
    let mut decoder = png::Decoder::new(std::io::BufReader::new(file));
    decoder.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let mut reader = decoder.read_info().map_err(|e| format!("header: {e}"))?;
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let info = reader
        .next_frame(&mut buf)
        .map_err(|e| format!("decode: {e}"))?;
    buf.truncate(info.buffer_size());
    let rgba: Vec<u8> = match info.color_type {
        png::ColorType::Rgba => buf,
        png::ColorType::Rgb => buf
            .chunks_exact(3)
            .flat_map(|p| [p[0], p[1], p[2], 0xFF])
            .collect(),
        png::ColorType::Grayscale => buf.iter().flat_map(|&g| [g, g, g, 0xFF]).collect(),
        png::ColorType::GrayscaleAlpha => buf
            .chunks_exact(2)
            .flat_map(|p| [p[0], p[0], p[0], p[1]])
            .collect(),
        png::ColorType::Indexed => {
            return Err("indexed PNG survived EXPAND (decoder bug?)".into());
        }
    };
    RgbaImage::new(info.width, info.height, rgba).map_err(|e| format!("{e:?}"))
}

/// Pre-load every asset the request references into a `vyr_core::Assets`
/// registry (module docs: resolution rules, verbatim-name keys). Any
/// missing/undecodable asset is a NAMED error — the caller exits BEFORE
/// rendering (invariant I6).
fn load_assets(req: &vyr_core::ir::Request) -> Result<Assets, String> {
    let mut srcs = Vec::new();
    collect_srcs(&req.root, &mut srcs);
    let mut assets = Assets::new();
    let mut names = Vec::new();
    for src in srcs {
        let path = resolve_src(&src);
        let img = decode_png(&path)
            .map_err(|e| format!("asset {src:?} (resolved: {}): {e}", path.display()))?;
        names.push(format!("{src} ({}x{})", img.w(), img.h()));
        assets
            .register(&src, img)
            .map_err(|e| format!("register {src:?}: {e:?}"))?;
    }
    if !assets.is_empty() {
        log(
            "INFO",
            &format!("assets: [{}] ({} B RGBA)", names.join(", "), assets.bytes()),
        );
    }
    Ok(assets)
}

fn write_png(path: &str, w: u32, h: u32, rgb: &[u8]) -> Result<(), String> {
    let file = std::fs::File::create(path).map_err(|e| format!("create {path}: {e}"))?;
    let mut enc = png::Encoder::new(std::io::BufWriter::new(file), w, h);
    enc.set_color(png::ColorType::Rgb);
    enc.set_depth(png::BitDepth::Eight);
    enc.write_header()
        .and_then(|mut hdr| hdr.write_image_data(rgb))
        .map_err(|e| format!("encode {path}: {e}"))
}

fn selftest_png(out: &str) -> ExitCode {
    log(
        "INFO",
        &format!("selftest-png start → {out} ({DEMO_W}x{DEMO_H})"),
    );
    let area = Rect {
        x: 0,
        y: 0,
        w: DEMO_W,
        h: DEMO_H,
    };
    let Some(mut canvas) = TinySkiaCanvas::new(area) else {
        log("ERROR", "pixmap allocation failed");
        return ExitCode::FAILURE;
    };
    let t0 = std::time::Instant::now();
    demo_scene(&mut canvas);
    let mut buf = vec![0u8; (DEMO_W * DEMO_H * 3) as usize];
    let stats = canvas.finish_into_rgb888(&mut buf, (DEMO_W * 3) as usize);
    let dt = t0.elapsed();
    // ns/px: the canonical metric (I3) — frame timing lives in the shell,
    // pixel counts in core.
    let px = (DEMO_W * DEMO_H) as f64;
    log(
        "INFO",
        &format!(
            "rendered in {:.3} ms ({:.1} ns/px frame-level), stats: {stats:?}",
            dt.as_secs_f64() * 1e3,
            dt.as_nanos() as f64 / px
        ),
    );
    if let Err(e) = write_png(out, DEMO_W, DEMO_H, &buf) {
        log("ERROR", &e);
        return ExitCode::FAILURE;
    }
    // F5 leg: the shared text fixture through the real IR path + the fonts
    // this binary found — proves font discovery, the registry and the glyph
    // path end-to-end (the farm's exact wiring).
    let text_out = match out.rsplit_once('.') {
        Some((stem, ext)) => format!("{stem}-text.{ext}"),
        None => format!("{out}-text.png"),
    };
    let mut fonts = load_fonts();
    let mut tbuf = vec![0u8; (DEMO_W * DEMO_H * 3) as usize];
    let t1 = std::time::Instant::now();
    match vyr_core::render_with_fonts(TEXT_IR, &mut fonts, area, &mut tbuf, (DEMO_W * 3) as usize) {
        Ok(tstats) => {
            log(
                "INFO",
                &format!(
                    "text fixture rendered in {:.3} ms, stats: {tstats:?}",
                    t1.elapsed().as_secs_f64() * 1e3
                ),
            );
            if let Err(e) = write_png(&text_out, DEMO_W, DEMO_H, &tbuf) {
                log("ERROR", &e);
                return ExitCode::FAILURE;
            }
        }
        Err(e) => {
            log("ERROR", &format!("text fixture failed: {e:?}"));
            return ExitCode::FAILURE;
        }
    }
    log("ALERT", &format!("selftest-png ok → {out} + {text_out}"));
    ExitCode::SUCCESS
}

/// `vyr-cli render <ir.json> <out.png> [--draft]` — the farm contract, at a
/// chosen quality tier (docs/quality-tiers.md). `--draft` selects
/// [`vyr_core::Quality::Draft`] (integer, no-AA, MCU-budgeted); the default is
/// `Quality::Exact` (the oracle). The two tiers produce DIFFERENT pixels by
/// design — that is what makes a host-side Exact-vs-Draft fidelity comparison
/// possible without an MCU in the loop.
fn render(ir_path: &str, out: &str, quality: vyr_core::Quality) -> ExitCode {
    log("INFO", &format!("render {ir_path} → {out} ({quality:?})"));
    let ir = match std::fs::read_to_string(ir_path) {
        Ok(s) => s,
        Err(e) => {
            log("ERROR", &format!("read {ir_path}: {e}"));
            return ExitCode::FAILURE;
        }
    };
    // Frame size comes from the request itself ({"w","h","root"} — the farm
    // contract); parse first so the buffer is allocated to the real size.
    let req = match vyr_core::ir::Request::parse(&ir) {
        Ok(r) => r,
        Err(e) => {
            log("ERROR", &format!("bad request: {e:?}"));
            return ExitCode::from(2);
        }
    };
    let (w, h) = (req.w, req.h);
    let mut fonts = load_fonts();
    // Asset preload (F6): every src decoded + registered BEFORE pixels; a
    // failure here is the honest hard exit, same as a render error.
    let assets = match load_assets(&req) {
        Ok(a) => a,
        Err(e) => {
            log("ERROR", &format!("asset preload failed: {e}"));
            return ExitCode::from(2);
        }
    };
    let mut buf = vec![0u8; (w * h * 3) as usize];
    match req.render_with_quality(
        &mut fonts,
        &assets,
        Rect { x: 0, y: 0, w, h },
        &mut buf,
        (w * 3) as usize,
        quality,
    ) {
        Ok(mut stats) => {
            let (live, peak) = heap_now();
            stats.peak_alloc_bytes = peak as u64;
            log("INFO", &format!("stats: {stats:?}"));
            log(
                "INFO",
                &format!(
                    "heap: peak {peak} B ({:.1} KiB), live-after-render {live} B — \
                     measured by the counting allocator, includes font/asset \
                     registries + parsed IR + the gutter pixmap",
                    peak as f64 / 1024.0
                ),
            );
            if let Err(e) = write_png(out, w, h, &buf) {
                log("ERROR", &e);
                return ExitCode::FAILURE;
            }
            log("ALERT", &format!("render ok → {out}"));
            ExitCode::SUCCESS
        }
        Err(e) => {
            // Honest failure (I6): unimplemented/unknown is a hard, loud exit
            // — the farm treats non-zero as FAIL, never a blank frame.
            log("ERROR", &format!("render failed: {e:?}"));
            ExitCode::from(2)
        }
    }
}

/// `vyr-cli measure <font> <size_px> <text>` — text measurement as JSON on
/// stdout (the one machine-readable line; logs stay on stderr/file). Gives a
/// caller BOTH boxes before rendering anything: the layout box
/// (advance × ascent..descent) and the ink box (where paint would land,
/// relative to pen origin/baseline). Measuring warms the glyph cache.
fn measure(font: &str, size_s: &str, text: &str) -> ExitCode {
    let Ok(size) = size_s.parse::<u32>() else {
        log("ERROR", &format!("size {size_s:?} is not a u32"));
        return ExitCode::from(2);
    };
    let mut fonts = load_fonts();
    match fonts.measure(font, size, text) {
        Ok(m) => {
            let ink = match m.ink {
                Some(r) => format!(
                    "{{\"x\":{},\"y\":{},\"w\":{},\"h\":{}}}",
                    r.x, r.y, r.w, r.h
                ),
                None => "null".to_string(),
            };
            println!(
                "{{\"font\":{font:?},\"size_px\":{size},\"text\":{text:?},\
                 \"advance\":{},\"ascent\":{},\"descent\":{},\"line_height\":{},\
                 \"ink\":{ink}}}",
                m.advance,
                m.ascent,
                m.descent,
                m.height()
            );
            ExitCode::SUCCESS
        }
        Err(e) => {
            log("ERROR", &format!("measure failed: {e:?}"));
            ExitCode::from(2)
        }
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.as_slice() {
        [cmd, out] if cmd == "selftest-png" => selftest_png(out),
        [cmd, ir, out] if cmd == "render" => render(ir, out, vyr_core::Quality::Exact),
        [cmd, ir, out, flag] if cmd == "render" && flag == "--draft" => {
            render(ir, out, vyr_core::Quality::Draft)
        }
        [cmd, font, size, text] if cmd == "measure" => measure(font, size, text),
        _ => {
            eprintln!("usage: vyr-cli selftest-png <out.png>");
            eprintln!(
                "       vyr-cli render <ir.json> <out.png> [--draft]   (env: VYR_FONTS=<dir>)"
            );
            eprintln!("       vyr-cli measure <font> <size_px> <text>   → JSON on stdout");
            ExitCode::from(2)
        }
    }
}

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
//! Logging (awto convention): every line timestamped, mirrored to BOTH
//! stderr (live) and ./tmp/vyr-cli.log (retroactive review, append). Format:
//! `HH:MM:SS.ffffff UTC  LEVEL [vyr-cli] message`.

use std::io::Write as _;
use std::process::ExitCode;
use std::time::{SystemTime, UNIX_EPOCH};

use vyr_core::demo::{DEMO_H, DEMO_W, TEXT_IR, demo_scene};
use vyr_core::{Fonts, Rect, TinySkiaCanvas};

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

fn render(ir_path: &str, out: &str) -> ExitCode {
    log("INFO", &format!("render {ir_path} → {out}"));
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
    let mut buf = vec![0u8; (w * h * 3) as usize];
    match req.render_with_fonts(
        &mut fonts,
        Rect { x: 0, y: 0, w, h },
        &mut buf,
        (w * 3) as usize,
    ) {
        Ok(stats) => {
            log("INFO", &format!("stats: {stats:?}"));
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

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.as_slice() {
        [cmd, out] if cmd == "selftest-png" => selftest_png(out),
        [cmd, ir, out] if cmd == "render" => render(ir, out),
        _ => {
            eprintln!("usage: vyr-cli selftest-png <out.png>");
            eprintln!("       vyr-cli render <ir.json> <out.png>   (env: VYR_FONTS=<dir>)");
            ExitCode::from(2)
        }
    }
}

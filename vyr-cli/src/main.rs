//! vyr-cli — the std shell. IR JSON in → PNG out (the farm contract), plus a
//! `selftest-png` mode that renders the shared demo scene (the exact golden
//! pixels) so "does the painter work here" is a one-command question.
//!
//! Logging (awto convention): every line timestamped, mirrored to BOTH
//! stderr (live) and ./tmp/vyr-cli.log (retroactive review, append). Format:
//! `HH:MM:SS.ffffff UTC  LEVEL [vyr-cli] message`.

use std::io::Write as _;
use std::process::ExitCode;
use std::time::{SystemTime, UNIX_EPOCH};

use vyr_core::demo::{DEMO_H, DEMO_W, demo_scene};
use vyr_core::{Rect, TinySkiaCanvas};

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
    log("ALERT", &format!("selftest-png ok → {out}"));
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
    // Frame size from env (farm contract), demo size default until F3/F7
    // finalise the request shape.
    let w: u32 = std::env::var("VYR_W")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(DEMO_W);
    let h: u32 = std::env::var("VYR_H")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(DEMO_H);
    let mut buf = vec![0u8; (w * h * 3) as usize];
    match vyr_core::render(&ir, Rect { x: 0, y: 0, w, h }, &mut buf, (w * 3) as usize) {
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
            eprintln!("       vyr-cli render <ir.json> <out.png>   (env: VYR_W, VYR_H)");
            ExitCode::from(2)
        }
    }
}

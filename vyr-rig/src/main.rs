//! vyr-rig CLI — the F18 rig driver (see lib docs). `./dev.py anim` /
//! `./dev.py ladder` are the canonical entry points; this binary is what
//! they (and the qemu-arm cross-ISA rung) invoke.
//!
//! ```text
//! vyr-rig anim --size W --frames N [--dump-dir D] [--hashes-out F]
//!              [--stats-out F] [--check F | --bless F]
//! vyr-rig ladder [--json-out F] [--check | --record]
//! ```
//!
//! `--check`/`--bless` compare/write the committed [`HashChain`] golden;
//! `ladder --check`/`--record` mirror vyr-bench's baseline gate
//! (`vyr-rig/baseline.json`, 1.5× + retry-confirm). Exit codes: 0 ok,
//! 1 failure (divergence/regression), 2 usage.
//!
//! Logging (awto convention): every line timestamped, mirrored to stderr and
//! `./tmp/vyr-rig.log` (append).

use std::io::Write as _;
use std::process::ExitCode;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use vyr_rig::{
    ACCEPT_FRAMES, ACCEPT_W, HashChain, RUNGS, RungReport, measure_rung, run_anim, rung,
};

/// A ladder regression must exceed baseline × this to fail — and then
/// REPRODUCE on a retry (vyr-bench's noise discipline, same constant).
const REGRESSION_X: f64 = 1.5;

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
    let line = format!("{}  {:5} [vyr-rig] {}", timestamp(), level, msg);
    eprintln!("{line}");
    if std::fs::create_dir_all("tmp").is_ok()
        && let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open("tmp/vyr-rig.log")
    {
        let _ = writeln!(f, "{line}");
    }
}

fn write_png(path: &std::path::Path, w: u32, h: u32, rgb: &[u8]) -> Result<(), String> {
    let file = std::fs::File::create(path).map_err(|e| format!("create {path:?}: {e}"))?;
    let mut enc = png::Encoder::new(std::io::BufWriter::new(file), w, h);
    enc.set_color(png::ColorType::Rgb);
    enc.set_depth(png::BitDepth::Eight);
    enc.write_header()
        .and_then(|mut hdr| hdr.write_image_data(rgb))
        .map_err(|e| format!("encode {path:?}: {e}"))
}

/// `--key value` lookup over the raw arg list.
fn arg_val(args: &[String], key: &str) -> Option<String> {
    args.iter()
        .position(|a| a == key)
        .and_then(|i| args.get(i + 1).cloned())
}

fn anim(args: &[String]) -> ExitCode {
    let size: u32 = match arg_val(args, "--size")
        .as_deref()
        .unwrap_or(&ACCEPT_W.to_string())
        .parse()
    {
        Ok(v) => v,
        Err(_) => {
            log("ERROR", "--size must be a rung width");
            return ExitCode::from(2);
        }
    };
    let Some((w, h)) = rung(size) else {
        let widths: Vec<String> = RUNGS.iter().map(|r| r.0.to_string()).collect();
        log(
            "ERROR",
            &format!(
                "--size {size} is not a rung (ladder widths: {})",
                widths.join("/")
            ),
        );
        return ExitCode::from(2);
    };
    let frames: u32 = match arg_val(args, "--frames")
        .as_deref()
        .unwrap_or(&ACCEPT_FRAMES.to_string())
        .parse()
    {
        Ok(v) if v > 0 => v,
        _ => {
            log("ERROR", "--frames must be a positive integer");
            return ExitCode::from(2);
        }
    };
    let dump_dir = arg_val(args, "--dump-dir").map(std::path::PathBuf::from);
    if let Some(d) = &dump_dir
        && let Err(e) = std::fs::create_dir_all(d)
    {
        log("ERROR", &format!("create {d:?}: {e}"));
        return ExitCode::FAILURE;
    }
    log(
        "INFO",
        &format!(
            "anim start: {w}x{h}, {frames} frames @ logical 60 fps, arch {}, dump {}",
            std::env::consts::ARCH,
            dump_dir
                .as_ref()
                .map(|d| d.display().to_string())
                .unwrap_or_else(|| "off".into()),
        ),
    );
    let t0 = Instant::now();
    let mut dump_err: Option<String> = None;
    let run = match run_anim(w, h, frames, |f, rgb| {
        if let Some(d) = &dump_dir
            && dump_err.is_none()
            && let Err(e) = write_png(&d.join(format!("frame-{f:04}.png")), w, h, rgb)
        {
            dump_err = Some(e);
        }
        // Progress heartbeat (long-run conventions): every 100 frames.
        if f % 100 == 0 && f > 0 {
            log(
                "INFO",
                &format!("  frame {f}/{frames} ok (incremental==full so far)"),
            );
        }
    }) {
        Ok(r) => r,
        Err(e) => {
            log("ERROR", &format!("anim FAILED: {e}"));
            return ExitCode::FAILURE;
        }
    };
    if let Some(e) = dump_err {
        log("ERROR", &format!("PNG dump failed: {e}"));
        return ExitCode::FAILURE;
    }
    let wall = t0.elapsed();
    let ms_frame = wall.as_secs_f64() * 1e3 / frames as f64;
    log(
        "INFO",
        &format!(
            "anim done: {frames} frames in {:.2} s ({ms_frame:.3} ms/frame full+incremental+verify\
             {}), dirty {:.1}%/step avg, run hash {:#018x}",
            wall.as_secs_f64(),
            if dump_dir.is_some() { "+png" } else { "" },
            run.dirty_pct(),
            run.run_hash
        ),
    );
    let chain = HashChain::from_run(&run);
    if let Some(path) = arg_val(args, "--hashes-out") {
        if let Err(e) = std::fs::write(&path, chain.to_json()) {
            log("ERROR", &format!("write {path}: {e}"));
            return ExitCode::FAILURE;
        }
        log("INFO", &format!("hash chain → {path}"));
    }
    if let Some(path) = arg_val(args, "--stats-out") {
        // The run summary perf-history consumes (the chain file stays a pure
        // golden). wall_s is host wall time — NON-target-indicative under
        // emulation by definition; the consumer labels it.
        let json = format!(
            "{{\"w\":{w},\"h\":{h},\"frames\":{frames},\"arch\":\"{}\",\
             \"wall_s\":{:.3},\"ms_frame\":{ms_frame:.4},\"dirty_pct\":{:.2},\
             \"run_hash\":\"{:#018x}\",\"dumped_pngs\":{}}}\n",
            std::env::consts::ARCH,
            wall.as_secs_f64(),
            run.dirty_pct(),
            run.run_hash,
            dump_dir.is_some(),
        );
        if let Err(e) = std::fs::write(&path, json) {
            log("ERROR", &format!("write {path}: {e}"));
            return ExitCode::FAILURE;
        }
    }
    match (arg_val(args, "--check"), arg_val(args, "--bless")) {
        (Some(_), Some(_)) => {
            log("ERROR", "--check and --bless are mutually exclusive");
            return ExitCode::from(2);
        }
        (Some(path), None) => {
            let golden = match std::fs::read_to_string(&path)
                .map_err(|e| e.to_string())
                .and_then(|s| HashChain::parse(&s))
            {
                Ok(g) => g,
                Err(e) => {
                    log(
                        "ERROR",
                        &format!("golden {path}: {e} (bless one first: --bless)"),
                    );
                    return ExitCode::FAILURE;
                }
            };
            if let Some(diff) = chain.diff(&golden) {
                log(
                    "ERROR",
                    &format!(
                        "HASH CHAIN DIVERGED from {path}: {diff} — investigate before re-blessing"
                    ),
                );
                return ExitCode::FAILURE;
            }
            log(
                "ALERT",
                &format!("hash chain matches {path} ({frames} frames byte-pinned)"),
            );
        }
        (None, Some(path)) => {
            if let Err(e) = std::fs::write(&path, chain.to_json()) {
                log("ERROR", &format!("write {path}: {e}"));
                return ExitCode::FAILURE;
            }
            log(
                "ALERT",
                &format!(
                    "hash chain BLESSED → {path} (commit it as its own reviewed change; \
                     run hash {:#018x})",
                    run.run_hash
                ),
            );
        }
        (None, None) => {}
    }
    ExitCode::SUCCESS
}

// --- ladder -------------------------------------------------------------------

fn baseline_path() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("baseline.json")
}

/// The two gated metrics per rung, vyr-bench-key style.
fn baseline_entries(r: &RungReport) -> [(String, f64); 2] {
    [
        (format!("ladder/full_{}x{}", r.w, r.h), r.full_ns_px()),
        (format!("ladder/incr_{}x{}", r.w, r.h), r.incr_ns_dirty_px()),
    ]
}

fn log_rung(r: &RungReport) {
    log(
        "INFO",
        &format!(
            "{:>9}  full {:>9.3} ms {:>6.2} ns/px {:>7.1} fps {:>6.2}x60 | \
             incr {:>7.3} ms {:>6.2} ns/dpx {:>8.1} fps {:>7.2}x60 | dirty {:>5.2}%",
            format!("{}x{}", r.w, r.h),
            r.full_ns / 1e6,
            r.full_ns_px(),
            r.fps_full(),
            r.headroom_full(),
            r.incr_ns / 1e6,
            r.incr_ns_dirty_px(),
            r.fps_incr(),
            r.headroom_incr(),
            r.dirty_pct(),
        ),
    );
}

fn rung_json(r: &RungReport) -> String {
    format!(
        "{{\"w\":{},\"h\":{},\"full_ns\":{:.1},\"incr_ns\":{:.1},\"dirty_px\":{:.1},\
         \"full_ns_px\":{:.4},\"incr_ns_dirty_px\":{:.4},\"dirty_pct\":{:.3},\
         \"fps_full\":{:.2},\"fps_incr\":{:.2},\"headroom_full_x\":{:.3},\"headroom_incr_x\":{:.3}}}",
        r.w,
        r.h,
        r.full_ns,
        r.incr_ns,
        r.dirty_px,
        r.full_ns_px(),
        r.incr_ns_dirty_px(),
        r.dirty_pct(),
        r.fps_full(),
        r.fps_incr(),
        r.headroom_full(),
        r.headroom_incr(),
    )
}

fn ladder(args: &[String]) -> ExitCode {
    let check = args.iter().any(|a| a == "--check");
    let record = args.iter().any(|a| a == "--record");
    if check && record {
        log("ERROR", "--check and --record are mutually exclusive");
        return ExitCode::from(2);
    }
    log(
        "INFO",
        &format!(
            "ladder start: {} rungs to 4K, median of {} rounds (warmup 2), \
             60-step incremental window",
            RUNGS.len(),
            7
        ),
    );
    let reports: Vec<RungReport> = RUNGS
        .iter()
        .map(|&(w, h)| {
            let r = measure_rung(w, h);
            log_rung(&r); // progress: a rung logs the moment it finishes
            r
        })
        .collect();
    if let (Some(top), true) = (reports.last(), reports.len() > 1) {
        log(
            "INFO",
            &format!(
                "the 4K story: full {:.1} ms ({:.2}x the 60fps budget) vs incremental \
                 {:.2} ms ({:.1}x headroom) — incremental is where 60 fps @ 4K lives",
                top.full_ns / 1e6,
                1.0 / top.headroom_full(),
                top.incr_ns / 1e6,
                top.headroom_incr(),
            ),
        );
    }
    if let Some(path) = arg_val(args, "--json-out") {
        let rows: Vec<String> = reports.iter().map(rung_json).collect();
        let json = format!("{{\"rungs\":[\n  {}\n]}}\n", rows.join(",\n  "));
        if let Err(e) = std::fs::write(&path, json) {
            log("ERROR", &format!("write {path}: {e}"));
            return ExitCode::FAILURE;
        }
        log("INFO", &format!("ladder JSON → {path}"));
    }

    if record {
        let mut map = std::collections::BTreeMap::new();
        for r in &reports {
            for (k, v) in baseline_entries(r) {
                map.insert(k, v);
            }
        }
        let json = serde_json::to_string_pretty(&map).expect("baseline serializes");
        if let Err(e) = std::fs::write(baseline_path(), json + "\n") {
            log("ERROR", &format!("write baseline: {e}"));
            return ExitCode::FAILURE;
        }
        log(
            "ALERT",
            &format!(
                "ladder baseline RECORDED → {} (commit it as its own reviewed change)",
                baseline_path().display()
            ),
        );
        return ExitCode::SUCCESS;
    }
    if check {
        let base: std::collections::BTreeMap<String, f64> =
            match std::fs::read_to_string(baseline_path())
                .map_err(|e| e.to_string())
                .and_then(|s| serde_json::from_str(&s).map_err(|e| e.to_string()))
            {
                Ok(b) => b,
                Err(e) => {
                    log(
                        "ERROR",
                        &format!("no usable baseline ({e}) — record one first (ladder --record)"),
                    );
                    return ExitCode::FAILURE;
                }
            };
        let mut failed = false;
        for r in &reports {
            for (name, nspx) in baseline_entries(r) {
                let Some(b) = base.get(&name) else {
                    failed = true;
                    log(
                        "ERROR",
                        &format!("REGRESSION-GATE GAP: {name} has no baseline entry"),
                    );
                    continue;
                };
                if nspx > b * REGRESSION_X {
                    // Retry-confirm (vyr-bench's discipline): a regression
                    // must REPRODUCE — re-measure just the offending rung.
                    let again = measure_rung(r.w, r.h);
                    let second = baseline_entries(&again)
                        .iter()
                        .find(|(k, _)| *k == name)
                        .map(|(_, v)| *v)
                        .unwrap();
                    if second > b * REGRESSION_X {
                        failed = true;
                        log(
                            "ERROR",
                            &format!(
                                "REGRESSION {name}: {nspx:.2} then {second:.2} ns/px vs \
                                 baseline {b:.2} (> {REGRESSION_X}x, reproduced)"
                            ),
                        );
                    } else {
                        log(
                            "INFO",
                            &format!(
                                "noise {name}: {nspx:.2} ns/px did not reproduce \
                                 ({second:.2} on retry, baseline {b:.2}) — not a failure"
                            ),
                        );
                    }
                } else if nspx * REGRESSION_X < *b {
                    log(
                        "INFO",
                        &format!(
                            "improvement {name}: {nspx:.2} ns/px vs baseline {b:.2} \
                             (consider re-recording)"
                        ),
                    );
                }
            }
        }
        if failed {
            log("ERROR", "ladder check FAILED");
            return ExitCode::FAILURE;
        }
        log("ALERT", "ladder check OK (all rungs within budget)");
    }
    ExitCode::SUCCESS
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("anim") => anim(&args[1..]),
        Some("ladder") => ladder(&args[1..]),
        _ => {
            eprintln!(
                "usage: vyr-rig anim --size W --frames N [--dump-dir D] [--hashes-out F]\n\
                 \x20                  [--stats-out F] [--check F | --bless F]\n\
                 \x20      vyr-rig ladder [--json-out F] [--check | --record]"
            );
            ExitCode::from(2)
        }
    }
}

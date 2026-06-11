//! The conformance-fixtures harness (invariant I8, the vyvanse machine
//! contract — coordinated on awto-vyvanse#321).
//!
//! vyvanse GENERATES fixtures and commits them into `tests/fixtures/`:
//! one `<label>.json` per case in the exact wire-request format
//! (`{"schema_version", "w", "h", "root": <XmlElement.to_dict()>}`), plus
//! `manifest.json`: a list of `{file, label, expect}` where `expect` is
//! `"render"` (must produce a non-blank frame) or `"error:<Variant>"` (must
//! fail with that named `RenderError` — expected honest failures are
//! first-class, never skipped rows). This test walks the manifest; vyr CI
//! thereby runs the vyvanse-authored conformance set WITHOUT importing
//! vyvanse.
//!
//! Fonts: the vendored `fonts/` dir is registered (text fixtures may rely on
//! roboto). Image fixtures will need a manifest `assets` extension — design
//! it on #321 when the first one lands.

use vyr_core::{Assets, Fonts, Rect, RenderError};

fn error_variant(e: &RenderError) -> &'static str {
    match e {
        RenderError::UnknownWidget(_) => "UnknownWidget",
        RenderError::BadIr(_) => "BadIr",
        RenderError::MissingAsset(_) => "MissingAsset",
        RenderError::BadAsset(_) => "BadAsset",
        RenderError::Unimplemented(_) => "Unimplemented",
        RenderError::UnknownFont(_) => "UnknownFont",
        RenderError::BadFont(_) => "BadFont",
        RenderError::MissingGlyph(_) => "MissingGlyph",
    }
}

fn fixture_fonts() -> Fonts {
    let mut fonts = Fonts::new();
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../fonts");
    if let Ok(entries) = std::fs::read_dir(dir) {
        for e in entries.flatten() {
            let p = e.path();
            let ext = p.extension().and_then(|x| x.to_str()).unwrap_or("");
            if matches!(ext, "ttf" | "otf")
                && let (Some(stem), Ok(bytes)) =
                    (p.file_stem().and_then(|s| s.to_str()), std::fs::read(&p))
            {
                fonts
                    .register(&stem.to_lowercase(), bytes)
                    .expect("vendored font registers");
            }
        }
    }
    fonts
}

#[test]
fn schema_version_is_checked() {
    // Accepted version: parses (seed-box.json carries it; this asserts the
    // negative side too — the contract is hard, not advisory).
    let bad = r#"{"schema_version":"9.9-other","w":10,"h":10,"root":{"name":"view"}}"#;
    let err = vyr_core::ir::Request::parse(bad).expect_err("unknown schema_version must fail");
    assert!(matches!(err, RenderError::BadIr(_)), "got {err:?}");
    // Omitted version: still accepted (older senders), per the #321 handoff.
    let none = r#"{"w":10,"h":10,"root":{"name":"view"}}"#;
    vyr_core::ir::Request::parse(none).expect("omitted schema_version stays accepted");
}

#[test]
fn conformance_fixtures() {
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
    let manifest: Vec<serde_json::Value> = serde_json::from_str(
        &std::fs::read_to_string(dir.join("manifest.json")).expect("manifest.json"),
    )
    .expect("manifest parses");
    assert!(!manifest.is_empty(), "manifest must not be empty");

    let mut fonts = fixture_fonts();
    let assets = Assets::new();
    let mut failures: Vec<String> = Vec::new();

    for entry in &manifest {
        let file = entry["file"].as_str().expect("manifest entry: file");
        let label = entry["label"].as_str().expect("manifest entry: label");
        let expect = entry["expect"].as_str().expect("manifest entry: expect");
        let ir = std::fs::read_to_string(dir.join(file)).expect("fixture file");

        let req = match vyr_core::ir::Request::parse(&ir) {
            Ok(r) => r,
            Err(e) => {
                // A parse error is only acceptable when the manifest expects
                // exactly that.
                if expect != format!("error:{}", error_variant(&e)) {
                    failures.push(format!("{label}: parse failed {e:?} (expected {expect})"));
                }
                continue;
            }
        };
        let (w, h) = (req.w, req.h);
        let mut buf = vec![0u8; (w * h * 3) as usize];
        let result = req.render_with(
            &mut fonts,
            &assets,
            Rect { x: 0, y: 0, w, h },
            &mut buf,
            (w * 3) as usize,
        );
        match (result, expect) {
            (Ok(stats), "render") => {
                if stats.pixels_written == 0 {
                    failures.push(format!("{label}: rendered Ok but wrote 0 pixels (blank)"));
                }
            }
            (Ok(_), exp) => {
                failures.push(format!("{label}: rendered Ok but manifest expects {exp}"));
            }
            (Err(e), exp) => {
                let got = format!("error:{}", error_variant(&e));
                if got != exp {
                    failures.push(format!("{label}: {got} ({e:?}) but manifest expects {exp}"));
                }
            }
        }
    }
    assert!(
        failures.is_empty(),
        "conformance fixtures failed:\n{}",
        failures.join("\n")
    );
}

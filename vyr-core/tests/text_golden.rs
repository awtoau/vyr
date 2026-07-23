//! F5 goldens: text renders deterministically, band-exactly, from a cache
//! that rasterizes each (font, size, codepoint) exactly once — and fails
//! honestly (missing glyph / unknown font / no fonts), never a tofu box.
//!
//! Fonts are loaded from ../fonts (the vendored standard fonts) — tests are
//! std; core still only ever sees bytes (invariant I7).

use vyr_core::{Fonts, Rect, RenderError, render, render_with_fonts};

const W: u32 = 120;
const H: u32 = 120;

/// Committed golden (FNV-1a 64). Re-bless: ./dev.py test --bless.
const TEXT_GOLDEN_FNV1A: u64 = 0x7697_E6D0_6BAB_2320;

/// The shared F5 fixture (label + centred button label + lcd, runs crossing
/// the band seams) — also what vyr-bench's text scene measures.
const FIXTURE: &str = vyr_core::demo::TEXT_IR;

fn roboto() -> Fonts {
    let mut fonts = Fonts::new();
    fonts
        .register("roboto", include_bytes!("../../fonts/roboto.ttf").to_vec())
        .expect("roboto parses");
    fonts
}

fn fnv1a(data: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(0x100_0000_01b3);
    }
    h
}

fn full(fonts: &mut Fonts) -> Vec<u8> {
    let mut buf = vec![0u8; (W * H * 3) as usize];
    let stats = render_with_fonts(
        FIXTURE,
        fonts,
        Rect {
            x: 0,
            y: 0,
            w: W,
            h: H,
        },
        &mut buf,
        (W * 3) as usize,
    )
    .expect("text fixture renders");
    assert!(stats.pixels_written > 0);
    assert!(
        stats.pixels_by_class[vyr_core::OpClass::Glyph as usize] > 0,
        "glyph pixels counted into the Glyph op class"
    );
    buf
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
fn text_golden_hash() {
    let buf = full(&mut roboto());
    dump_png("text-golden-f5.png", &buf);
    let h = fnv1a(&buf);
    if std::env::var_os("VYR_BLESS").is_some() {
        eprintln!("BLESS: TEXT_GOLDEN_FNV1A = {h:#018X}");
        return;
    }
    assert_eq!(h, TEXT_GOLDEN_FNV1A, "text golden drifted");
}

/// The F5 band-equivalence proof: label/button/lcd runs crossing the band
/// seams, stitched from even (30) AND uneven (17) band heights, must be
/// byte-identical to the full frame — cached masks blitted at integer pens
/// with deterministic integer blending make this hold (painter docs).
#[test]
fn text_band_equivalence() {
    let mut fonts = roboto();
    let full = full(&mut fonts);
    let stride = (W * 3) as usize;
    for band_h in [30u32, 17u32] {
        let mut banded = vec![0u8; stride * H as usize];
        let mut y = 0;
        while y < H {
            let h = band_h.min(H - y);
            let band = &mut banded[y as usize * stride..(y + h) as usize * stride];
            render_with_fonts(
                FIXTURE,
                &mut fonts,
                Rect {
                    x: 0,
                    y: y as i32,
                    w: W,
                    h,
                },
                band,
                stride,
            )
            .expect("band renders");
            y += h;
        }
        if full != banded {
            let diffs = full.iter().zip(&banded).filter(|(a, b)| a != b).count();
            dump_png(&format!("text-band-fail-full-{band_h}.png"), &full);
            dump_png(&format!("text-band-fail-banded-{band_h}.png"), &banded);
            panic!("band_h={band_h}: text band stitch differs ({diffs} bytes)");
        }
    }
}

/// The exactly-once acceptance: two labels SHARING glyphs (same font+size)
/// rasterize each unique (font,size,cp) once; a second render through the
/// same `Fonts` rasterizes nothing new.
#[test]
fn glyph_cache_rasterizes_exactly_once() {
    let ir = r#"{"w":120,"h":120,"root":{"name":"view","children":[
        {"name":"vy_label","attrs":{"x":"4","y":"10","text":"vyr"}},
        {"name":"vy_label","attrs":{"x":"4","y":"40","text":"very vyr"}}]}}"#;
    // Unique codepoints across both runs at (roboto, 14): v y r space e = 5.
    let mut fonts = roboto();
    let mut buf = vec![0u8; (W * H * 3) as usize];
    let area = Rect {
        x: 0,
        y: 0,
        w: W,
        h: H,
    };
    let stats1 =
        render_with_fonts(ir, &mut fonts, area, &mut buf, (W * 3) as usize).expect("labels render");
    assert_eq!(
        stats1.glyphs_rasterized, 5,
        "each unique (font,size,cp) rasterized exactly once"
    );
    assert_eq!(stats1.glyph_cache_entries, 5);
    assert!(stats1.glyph_cache_bytes > 0, "masks hold A8 bytes");
    let buf1 = buf.clone();
    let stats2 =
        render_with_fonts(ir, &mut fonts, area, &mut buf, (W * 3) as usize).expect("re-render");
    assert_eq!(
        stats2.glyphs_rasterized, 5,
        "second render rasterized NOTHING new (pure cached blits)"
    );
    assert_eq!(buf, buf1, "cached-blit render is byte-identical");
}

/// Missing glyph = hard error naming font + codepoint (I6: never tofu).
/// U+E000 (private use) has no Roboto glyph.
#[test]
fn missing_glyph_is_honest_error() {
    let ir = r#"{"w":120,"h":120,"root":{"name":"view","children":[
        {"name":"vy_label","attrs":{"x":"4","y":"10","text":"ok\uE000"}}]}}"#;
    let mut buf = vec![0u8; (W * H * 3) as usize];
    let err = render_with_fonts(
        ir,
        &mut roboto(),
        Rect {
            x: 0,
            y: 0,
            w: W,
            h: H,
        },
        &mut buf,
        (W * 3) as usize,
    )
    .expect_err("missing glyph must hard-error");
    match err {
        RenderError::MissingGlyph(msg) => {
            assert!(msg.contains("E000"), "names the codepoint: {msg}");
            assert!(msg.contains("roboto"), "names the font: {msg}");
        }
        other => panic!("expected MissingGlyph, got {other:?}"),
    }
}

/// Unknown font = hard error naming what IS available.
#[test]
fn unknown_font_is_honest_error() {
    let ir = r#"{"w":120,"h":120,"root":{"name":"view","children":[
        {"name":"vy_label","attrs":{"x":"4","y":"10","text":"hi","style_text_font":"comic_sans_12"}}]}}"#;
    let mut buf = vec![0u8; (W * H * 3) as usize];
    let err = render_with_fonts(
        ir,
        &mut roboto(),
        Rect {
            x: 0,
            y: 0,
            w: W,
            h: H,
        },
        &mut buf,
        (W * 3) as usize,
    )
    .expect_err("unknown font must hard-error");
    match err {
        RenderError::UnknownFont(msg) => {
            assert!(msg.contains("comic_sans"), "names the request: {msg}");
            assert!(msg.contains("roboto"), "names the available fonts: {msg}");
        }
        other => panic!("expected UnknownFont, got {other:?}"),
    }
}

/// The fonts-less `render()` convenience stays honest: text through it is an
/// UnknownFont error (there is nothing to draw with), never a blank.
#[test]
fn render_without_fonts_is_honest_error() {
    let ir = r#"{"w":120,"h":120,"root":{"name":"view","children":[
        {"name":"vy_label","attrs":{"x":"4","y":"10","text":"hi"}}]}}"#;
    let mut buf = vec![0u8; (W * H * 3) as usize];
    let err = render(
        ir,
        Rect {
            x: 0,
            y: 0,
            w: W,
            h: H,
        },
        &mut buf,
        (W * 3) as usize,
    )
    .expect_err("no fonts registered → text must hard-error");
    assert!(matches!(err, RenderError::UnknownFont(_)), "{err:?}");
}

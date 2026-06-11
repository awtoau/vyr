//! F4 wave 1 goldens: vy_radio + vy_checkbox composites (the #313-matching
//! marks + measure-centred labels), deterministic and band-exact.

use vyr_core::{Fonts, Rect};

const W: u32 = 120;
const H: u32 = 120;

/// Committed golden (FNV-1a 64). Re-bless: ./dev.py test --bless.
const STRUCTURE_GOLDEN_FNV1A: u64 = 0xBDEA_6FD5_08EA_90C8;

/// Radio selected/unselected + checkbox checked/unchecked, labels on, rows
/// crossing the 30/60/90 band seams.
const FIXTURE: &str = r##"{
  "schema_version": "0.6-vyvanse",
  "w": 120, "h": 120,
  "root": {"name": "view", "children": [
    {"name": "vy_radio", "attrs": {"x": "8", "y": "10", "width": "104", "height": "20",
      "text": "Pick me", "value": "1"}},
    {"name": "vy_radio", "attrs": {"x": "8", "y": "38", "width": "104", "height": "20",
      "text": "Or me", "value": "0"}},
    {"name": "vy_checkbox", "attrs": {"x": "8", "y": "66", "width": "104", "height": "20",
      "text": "Ticked", "checked": "1"}},
    {"name": "vy_checkbox", "attrs": {"x": "8", "y": "94", "width": "104", "height": "20",
      "text": "Clear", "value": "0"}}
  ]}
}"##;

fn roboto() -> Fonts {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../fonts/roboto.ttf");
    let bytes = std::fs::read(&path).expect("vendored fonts/roboto.ttf");
    let mut fonts = Fonts::new();
    fonts.register("roboto", bytes).expect("roboto registers");
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

fn render_full(fonts: &mut Fonts) -> Vec<u8> {
    let req = vyr_core::ir::Request::parse(FIXTURE).expect("fixture parses");
    let mut buf = vec![0u8; (W * H * 3) as usize];
    let stats = req
        .render_with_fonts(
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
        .expect("fixture renders");
    assert!(stats.pixels_written > 0);
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
fn structure_golden_hash() {
    let mut fonts = roboto();
    let buf = render_full(&mut fonts);
    dump_png("structure-golden-f4.png", &buf);
    let h = fnv1a(&buf);
    if std::env::var_os("VYR_BLESS").is_some() {
        eprintln!("BLESS: STRUCTURE_GOLDEN_FNV1A = {h:#018X}");
        return;
    }
    assert_eq!(h, STRUCTURE_GOLDEN_FNV1A, "structure golden drifted");
}

#[test]
fn structure_band_equivalence() {
    let mut fonts = roboto();
    let full = render_full(&mut fonts);
    let req = vyr_core::ir::Request::parse(FIXTURE).expect("fixture parses");
    let stride = (W * 3) as usize;
    for band_h in [30u32, 17u32] {
        let mut banded = vec![0u8; stride * H as usize];
        let mut y = 0;
        while y < H {
            let h = band_h.min(H - y);
            let band = &mut banded[y as usize * stride..(y + h) as usize * stride];
            req.render_with_fonts(
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
        assert_eq!(
            full, banded,
            "band_h={band_h}: structure band stitch differs"
        );
    }
}

#[test]
fn remaining_structure_widgets_still_error() {
    // dropdown/table/chart stay honest errors until the placeholder wave is
    // coordinated on awto-vyvanse#321 (the farm contract encodes them).
    for name in ["vy_dropdown", "vy_table", "vy_chart"] {
        let ir = format!(
            r#"{{"w":120,"h":120,"root":{{"name":"view","children":[
                {{"name":"{name}","attrs":{{"x":"0","y":"0","width":"50","height":"20"}}}}]}}}}"#
        );
        let mut buf = vec![0u8; (W * H * 3) as usize];
        let err = vyr_core::render(
            &ir,
            Rect {
                x: 0,
                y: 0,
                w: W,
                h: H,
            },
            &mut buf,
            (W * 3) as usize,
        )
        .expect_err("structure widget must still hard-error");
        assert!(matches!(err, vyr_core::RenderError::Unimplemented(_)));
    }
}

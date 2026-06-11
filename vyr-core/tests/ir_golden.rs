//! F3-lite goldens: the non-text IR subset renders, deterministically, with
//! byte-exact band equivalence — and text/unknown widgets hard-error (I6).

use vyr_core::{Rect, render};

const W: u32 = 120;
const H: u32 = 120;

/// Committed golden (FNV-1a 64). Re-bless: ./dev.py test --bless.
const IR_GOLDEN_FNV1A: u64 = 0xFDBF_1D98_1927_E3BB;

/// One of each F3-lite widget (shared with vyr-bench — see demo::DEMO_IR).
const FIXTURE: &str = vyr_core::demo::DEMO_IR;

fn fnv1a(data: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(0x100_0000_01b3);
    }
    h
}

fn full() -> Vec<u8> {
    let mut buf = vec![0u8; (W * H * 3) as usize];
    let stats = render(
        FIXTURE,
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
fn ir_golden_hash() {
    let buf = full();
    dump_png("ir-golden-f3.png", &buf);
    let h = fnv1a(&buf);
    if std::env::var_os("VYR_BLESS").is_some() {
        eprintln!("BLESS: IR_GOLDEN_FNV1A = {h:#018X}");
        return;
    }
    assert_eq!(h, IR_GOLDEN_FNV1A, "IR golden drifted");
}

#[test]
fn ir_band_equivalence() {
    let full = full();
    let stride = (W * 3) as usize;
    for band_h in [30u32, 17u32] {
        let mut banded = vec![0u8; stride * H as usize];
        let mut y = 0;
        while y < H {
            let h = band_h.min(H - y);
            let band = &mut banded[y as usize * stride..(y + h) as usize * stride];
            render(
                FIXTURE,
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
        assert_eq!(full, banded, "band_h={band_h}: IR band stitch differs");
    }
}

#[test]
fn text_widget_is_honest_error() {
    let ir = r#"{"w":120,"h":120,"root":{"name":"view","children":[
        {"name":"vy_label","attrs":{"x":"0","y":"0","width":"100","height":"20","text":"hi"}}]}}"#;
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
    .expect_err("pre-F5 text must hard-error");
    assert!(matches!(err, vyr_core::RenderError::Unimplemented(_)));
}

#[test]
fn unknown_widget_is_honest_error() {
    let ir = r#"{"w":120,"h":120,"root":{"name":"view","children":[
        {"name":"vy_mystery","attrs":{"x":"0","y":"0","width":"10","height":"10"}}]}}"#;
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
    .expect_err("unknown vy_ must hard-error");
    assert!(matches!(err, vyr_core::RenderError::UnknownWidget(_)));
}

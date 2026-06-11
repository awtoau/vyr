//! F3 clip-stack goldens: containers clip their children (radius-aware),
//! deterministically, with byte-exact band equivalence — the plan's F3
//! acceptance ("a child overflowing a rounded container clips identically
//! full-frame vs banded"). The fixture overflows every op class (path
//! fills, glyph blits, image blits) through both a ROUNDED clip (the A8
//! mask path, nested two deep) and a PURE-RECT clip (the exact integer
//! span fast path) — see `demo::CLIP_IR`.

use vyr_core::demo::{CLIP_IR, IMAGE_ASSET};
use vyr_core::{Assets, Fonts, Rect, RgbaImage};

const W: u32 = 120;
const H: u32 = 120;

/// Committed golden (FNV-1a 64). Re-bless: ./dev.py test --bless.
const CLIP_GOLDEN_FNV1A: u64 = 0x4E06_03B5_BD51_F336;

fn fnv1a(data: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(0x100_0000_01b3);
    }
    h
}

fn roboto() -> Fonts {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../fonts/roboto.ttf");
    let bytes = std::fs::read(&path).expect("vendored fonts/roboto.ttf");
    let mut fonts = Fonts::new();
    fonts.register("roboto", bytes).expect("roboto registers");
    fonts
}

fn checker_assets() -> Assets {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/assets/checker-24.png");
    let file = std::fs::File::open(&path).expect("committed tests/assets/checker-24.png");
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

fn render_full(fonts: &mut Fonts, assets: &Assets) -> Vec<u8> {
    let mut buf = vec![0u8; (W * H * 3) as usize];
    let stats = vyr_core::render_with(
        CLIP_IR,
        fonts,
        assets,
        Rect {
            x: 0,
            y: 0,
            w: W,
            h: H,
        },
        &mut buf,
        (W * 3) as usize,
    )
    .expect("clip fixture renders");
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

fn px(buf: &[u8], x: u32, y: u32) -> [u8; 3] {
    let i = ((y * W + x) * 3) as usize;
    [buf[i], buf[i + 1], buf[i + 2]]
}

#[test]
fn clip_golden_hash() {
    let mut fonts = roboto();
    let buf = render_full(&mut fonts, &checker_assets());
    dump_png("clip-golden-f3b.png", &buf);
    let h = fnv1a(&buf);
    if std::env::var_os("VYR_BLESS").is_some() {
        eprintln!("BLESS: CLIP_GOLDEN_FNV1A = {h:#018X}");
        return;
    }
    assert_eq!(h, CLIP_GOLDEN_FNV1A, "clip golden drifted");
}

/// THE F3 acceptance: the overflowing-children scene clips identically
/// full-frame vs banded — even (30-row) AND uneven (17-row) splits,
/// byte-exact. The clip mask is rebuilt per band from world-quantized
/// polygons; this is the test that proves those bytes are band-invariant.
#[test]
fn clip_band_equivalence() {
    let mut fonts = roboto();
    let assets = checker_assets();
    let full = render_full(&mut fonts, &assets);
    let stride = (W * 3) as usize;
    for band_h in [30u32, 17u32] {
        let mut banded = vec![0u8; stride * H as usize];
        let mut y = 0;
        while y < H {
            let h = band_h.min(H - y);
            let band = &mut banded[y as usize * stride..(y + h) as usize * stride];
            vyr_core::render_with(
                CLIP_IR,
                &mut fonts,
                &assets,
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
        assert_eq!(full, banded, "band_h={band_h}: clip band stitch differs");
    }
}

/// Spot-probes that the clip actually TRIMS — radius-aware:
/// the disc child overflows its rounded container, so pixels inside the
/// container's bounding RECT but outside its corner ARC must be untouched
/// backdrop, while pixels inside the arc keep the disc colour. Pixels past
/// the flat edges must be backdrop too.
#[test]
fn clip_trims_radius_aware() {
    let mut fonts = roboto();
    let buf = render_full(&mut fonts, &checker_assets());
    let backdrop = [0xF0, 0xEE, 0xE8];
    // Container 1: world rect 10..82 x 8..80, radius 18; BR arc centre
    // (64, 62). The disc (centre (72,68), r 26, #E0501E) covers all of
    // these world points geometrically:
    // (90, 60): right of the container's flat right edge (x >= 82) — clipped.
    assert_eq!(px(&buf, 90, 60), backdrop, "flat-edge overflow must clip");
    // (72, 85): below the bottom edge (y >= 80) — clipped.
    assert_eq!(px(&buf, 72, 85), backdrop, "bottom overflow must clip");
    // (79, 76): inside the bounding rect, but dist to BR arc centre
    // = sqrt(15^2 + 14^2) ~ 20.5 > 18 — outside the ARC, clipped. This is
    // the radius-aware bite.
    assert_eq!(px(&buf, 79, 76), backdrop, "corner-arc overflow must clip");
    // (70, 70): dist to BR arc centre = sqrt(6^2 + 8^2) = 10 < 16 — well
    // inside the arc; the disc paints here.
    assert_eq!(
        px(&buf, 70, 70),
        [0xE0, 0x50, 0x1E],
        "inside arc keeps disc"
    );
    // Container 2 (pure rect, 10..74 x 88..112): the green frame child
    // extends to world x..94, y..124 but must stop at the rect edge.
    assert_eq!(px(&buf, 80, 100), backdrop, "rect-clip right overflow");
    assert_eq!(px(&buf, 60, 115), backdrop, "rect-clip bottom overflow");
    // (55, 110): inside the rect clip, left of the overlapping checker image
    // (x >= 62) and below the label's descender rows (ink ends ~107).
    assert_eq!(
        px(&buf, 55, 110),
        [0x2E, 0x8B, 0x57],
        "inside rect keeps frame fill"
    );
}

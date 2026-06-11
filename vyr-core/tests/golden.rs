//! F1 goldens: determinism + band equivalence.
//!
//! - `golden_hash`: the fixture scene's RGB888 output must hash to the
//!   committed constant — byte-stable across machines (invariant I2). To
//!   re-bless after a REVIEWED rendering change: run with `VYR_BLESS=1` and
//!   paste the printed hash.
//! - `band_equivalence`: the same scene rendered as horizontal bands and
//!   stitched must be byte-identical to the full-frame render (invariant I1
//!   — banding is the only code path, full-frame is just one big band).
//! - Set `VYR_TEST_DUMP=1` to also write PNGs to ../tmp/ for eyeballing.

use vyr_core::demo::{DEMO_H, DEMO_W, demo_scene};
use vyr_core::{Canvas, Rect, TinySkiaCanvas};

const W: u32 = DEMO_W;
const H: u32 = DEMO_H;

/// Committed golden (FNV-1a 64 of the RGB888 buffer). Re-bless via VYR_BLESS=1.
const GOLDEN_FNV1A: u64 = 0x19EB_E8B0_3CC8_F0E1;

/// The shared F1 scene (vyr_core::demo) — one of each primitive, crossing
/// the 30/60/90 band seams, AA edges + an alpha blend in play.
fn draw_scene(c: &mut dyn Canvas) {
    demo_scene(c);
}

fn render_full() -> Vec<u8> {
    let area = Rect {
        x: 0,
        y: 0,
        w: W,
        h: H,
    };
    let mut canvas = TinySkiaCanvas::new(area).expect("pixmap");
    draw_scene(&mut canvas);
    let mut buf = vec![0u8; (W * H * 3) as usize];
    let stats = canvas.finish_into_rgb888(&mut buf, (W * 3) as usize);
    assert!(stats.pixels_written > 0, "counters wired");
    buf
}

fn render_banded(band_h: u32) -> Vec<u8> {
    let stride = (W * 3) as usize;
    let mut out = vec![0u8; stride * H as usize];
    let mut y = 0;
    while y < H {
        let h = band_h.min(H - y);
        let area = Rect {
            x: 0,
            y: y as i32,
            w: W,
            h,
        };
        let mut canvas = TinySkiaCanvas::new(area).expect("pixmap");
        draw_scene(&mut canvas);
        let band = &mut out[y as usize * stride..(y + h) as usize * stride];
        canvas.finish_into_rgb888(band, stride);
        y += h;
    }
    out
}

fn fnv1a(data: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(0x100_0000_01b3);
    }
    h
}

fn dump_png(name: &str, buf: &[u8]) {
    if std::env::var_os("VYR_TEST_DUMP").is_none() {
        return;
    }
    // Workspace-relative ./tmp/ per the awto logging rules (gitignored).
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../tmp");
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join(name);
    let file = std::fs::File::create(&path).unwrap();
    let mut enc = png::Encoder::new(std::io::BufWriter::new(file), W, H);
    enc.set_color(png::ColorType::Rgb);
    enc.set_depth(png::BitDepth::Eight);
    enc.write_header().unwrap().write_image_data(buf).unwrap();
    eprintln!("dumped {}", path.display());
}

#[test]
fn golden_hash() {
    let buf = render_full();
    dump_png("golden-f1.png", &buf);
    let h = fnv1a(&buf);
    if std::env::var_os("VYR_BLESS").is_some() {
        eprintln!("BLESS: GOLDEN_FNV1A = {h:#018X}");
        return;
    }
    assert_eq!(
        h, GOLDEN_FNV1A,
        "golden drifted — investigate before re-blessing"
    );
}

#[test]
fn band_equivalence() {
    let full = render_full();
    // 30-row bands (4 even bands) AND a deliberately awkward 17-row split
    // (uneven tail) — both must stitch to the exact full-frame bytes.
    for band_h in [30u32, 17u32] {
        let banded = render_banded(band_h);
        if full != banded {
            let stride = (W * 3) as usize;
            let mut diffs = 0usize;
            let mut first = None;
            for (i, (a, b)) in full.iter().zip(banded.iter()).enumerate() {
                if a != b {
                    diffs += 1;
                    if first.is_none() {
                        first = Some(i);
                    }
                }
            }
            let i = first.unwrap();
            let (row, col) = (i / stride, (i % stride) / 3);
            dump_png(&format!("band-fail-full-{band_h}.png"), &full);
            dump_png(&format!("band-fail-banded-{band_h}.png"), &banded);
            panic!(
                "band_h={band_h}: {diffs} differing bytes, first at row {row} col {col} \
                 (full rgb {:?} vs banded {:?})",
                &full[i - i % 3..i - i % 3 + 3],
                &banded[i - i % 3..i - i % 3 + 3]
            );
        }
    }
}

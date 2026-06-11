//! vyr-rig — F18 "the rig" (issue #18): the emulated heavy-work test rig.
//!
//! Three jobs, one crate:
//!
//! 1. **The deterministic animation driver.** [`scene_ir`] is a parametric
//!    scene in which the FRAME INDEX drives every animated property — no
//!    clocks, so invariant I2 holds frame-for-frame. [`run_anim`] renders
//!    every frame BOTH ways — full, and incrementally from the previous
//!    frame via `dirty_rects`/`render_incremental` — and asserts per-frame
//!    BYTE-EQUALITY (any divergence is a hard error naming the frame: the
//!    runtime loop, proven continuously). Per-frame FNV-1a hashes chain into
//!    a run hash; [`HashChain`] is the committed golden
//!    (`vyr-rig/hashchain.json`), so a 600-frame lossless-video regression
//!    costs kilobytes, not megabytes. The PNG sequence / ffmpeg video are
//!    `./tmp` artifacts the shell assembles (`./dev.py anim`).
//!
//! 2. **The resolution ladder.** [`measure_rung`] times the same scene at
//!    [`RUNGS`] (120 → 3840×2160 — the top rung IS 4K UHD) with the
//!    vyr-bench methodology (warmup + median of rounds): ns/px full,
//!    ns/dirty-px incremental over a 60-step window spanning toggle edges,
//!    dirty-area %, and fps headroom vs the 60 fps budget. The check/record
//!    baseline (`vyr-rig/baseline.json`, 1.5× + retry-confirm) mirrors
//!    vyr-bench's gate.
//!
//! 3. **Cross-ISA self-containment.** The font and the checker asset are
//!    `include_bytes!`-baked, so the SAME binary cross-builds for
//!    `armv7-unknown-linux-musleabihf` and runs under `qemu-arm-static` with
//!    no filesystem layout to reproduce — frame hashes byte-compared against
//!    the x86 run are the cross-ISA determinism proof (F1's last acceptance
//!    box, across an entire ISA).
//!
//! Std on purpose: this is a shell-side harness like vyr-bench. F18 adds
//! NOTHING to vyr-core — the rig drives the existing public surface.

use std::time::Instant;

use vyr_core::ir::Request;
use vyr_core::{Assets, Fonts, Rect, RgbaImage, dirty_rects};

// --- scene parameters --------------------------------------------------------

/// The design space every rung is an integer scale of.
pub const DESIGN_W: i64 = 480;
/// See [`DESIGN_W`].
pub const DESIGN_H: i64 = 270;

/// The resolution ladder (16:9; the top rung IS 4K UHD). The 120 rung's
/// height rounds 67.5 → 68 — the one non-exact 9/16.
pub const RUNGS: &[(u32, u32)] = &[
    (120, 68),
    (240, 135),
    (480, 270),
    (960, 540),
    (1920, 1080),
    (3840, 2160),
];

/// Rung width → `(w, h)`; rung widths are the ladder's whole vocabulary.
pub fn rung(width: u32) -> Option<(u32, u32)> {
    RUNGS.iter().copied().find(|&(w, _)| w == width)
}

/// The acceptance-run shape (issue #18): 600 frames — 10 s at the logical
/// 60 fps — at the 480 rung.
pub const ACCEPT_W: u32 = 480;
/// See [`ACCEPT_W`].
pub const ACCEPT_FRAMES: u32 = 600;

/// Scene revision tag stored in hash chains: bump when [`scene_ir`] changes
/// so a stale chain fails loudly as "different scene", never as a phantom
/// pixel regression.
pub const SCENE_VERSION: &str = "rig-v1";

// Self-contained assets (module docs, job 3). The checker is the SAME
// committed F6 asset the goldens use; the registry key matches its
// fixture-contract name.
const ROBOTO_TTF: &[u8] = include_bytes!("../../fonts/roboto.ttf");
const CHECKER_PNG: &[u8] = include_bytes!("../../vyr-core/tests/assets/checker-24.png");
/// The registry key [`scene_ir`]'s `src` references (the F6 verbatim-name
/// contract).
pub const CHECKER_NAME: &str = "checker-24.png";

/// The rig's font registry: the embedded Roboto only.
pub fn rig_fonts() -> Fonts {
    let mut fonts = Fonts::new();
    fonts
        .register("roboto", ROBOTO_TTF.to_vec())
        .expect("embedded roboto parses");
    fonts
}

/// The rig's asset registry: the embedded checker, decoded once.
pub fn rig_assets() -> Assets {
    let mut reader = png::Decoder::new(std::io::Cursor::new(CHECKER_PNG))
        .read_info()
        .expect("embedded checker header");
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let info = reader
        .next_frame(&mut buf)
        .expect("embedded checker decodes");
    buf.truncate(info.buffer_size());
    let img = RgbaImage::new(info.width, info.height, buf).expect("checker dims");
    let mut assets = Assets::new();
    assets
        .register(CHECKER_NAME, img)
        .expect("register checker");
    assets
}

// --- the parametric scene ------------------------------------------------------

/// Design-x → screen px (floor; exact for the 16:9 rungs).
fn sx(v: i64, w: u32) -> i64 {
    v * w as i64 / DESIGN_W
}

/// Design-y → screen px.
fn sy(v: i64, h: u32) -> i64 {
    v * h as i64 / DESIGN_H
}

/// Slider triangle wave 0..100..0 — period 100 frames; the knob moves EVERY
/// frame.
pub fn slider_value(f: u32) -> i64 {
    let p = (f % 100) as i64;
    if p <= 50 { p * 2 } else { (100 - p) * 2 }
}

/// Toggle square wave: flips every 30 frames (0.5 s at the logical 60 fps) —
/// the known blink period.
pub fn toggle_on(f: u32) -> bool {
    (f / 30).is_multiple_of(2)
}

/// Progress sawtooth 0..98 — period 50 frames; the wrap is a deliberately
/// large dirty step.
pub fn progress_value(f: u32) -> i64 {
    ((f % 50) * 2) as i64
}

/// Mover-disc x in DESIGN space, RELATIVE to its clipping container: sweeps
/// −48 → 248 (period 148 frames), crossing BOTH rounded clip edges of the
/// 200-wide container every cycle — the moving-widget-across-clip-boundary
/// exercise (and, because incremental repaint renders arbitrary dirty rects,
/// the per-frame band-seam exercise too).
pub fn mover_x(f: u32) -> i64 {
    -48 + (f as i64 * 2) % 296
}

/// Checker top-left in DESIGN space: a diagonal roam wrapping at different
/// periods per axis (140 / 90 frames) so the path precesses over the other
/// widgets (z-order restack exercise).
pub fn checker_pos(f: u32) -> (i64, i64) {
    ((f as i64 * 3) % 420, 40 + (f as i64 * 2) % 180)
}

/// The parametric rig scene. Animated (all pure functions of `frame`):
/// slider sweep, toggle blink, progress sawtooth, a disc crossing its
/// container's rounded CLIP edges, a digits-only frame counter
/// (glyph-cache-friendly: ten glyphs ever), and the 24×24 checker
/// translating at NATURAL size (the v1 image contract — position scales,
/// pixels don't). Static chrome: header frame + title, gauge ring (vyr
/// renders gauges as static rings until F4 sweep arcs — animating `value`
/// would dirty without painting), footer rule + credit. Geometry is a pure
/// scale of the 480×270 design space; font sizes floor at 1 px (size 0 is
/// junk IR).
pub fn scene_ir(w: u32, h: u32, frame: u32) -> String {
    let fs_title = sy(14, h).max(1);
    let fs_counter = sy(16, h).max(1);
    let fs_footer = sy(10, h).max(1);
    let bw1 = sy(1, h).max(1);
    let bw2 = sy(2, h).max(1);
    let mover_d = sy(48, h); // one scale for both axes — it stays a circle
    let (ckx, cky) = checker_pos(frame);
    format!(
        r##"{{
  "schema_version": "0.6-vyvanse",
  "w": {w}, "h": {h},
  "root": {{"name": "view", "attrs": {{"background": "#22262B"}}, "children": [
    {{"name": "vy_frame", "attrs": {{"x": "{hdr_x}", "y": "{hdr_y}", "width": "{hdr_w}", "height": "{hdr_h}",
      "background": "#2E3440", "radius": "{hdr_r}", "border_width": "{bw1}", "border_color": "#4C566A"}}}},
    {{"name": "vy_label", "attrs": {{"x": "{ttl_x}", "y": "{ttl_y}", "width": "{ttl_w}", "height": "{ttl_h}",
      "text": "vyr rig", "color": "#ECEFF4", "font_family": "roboto", "font_size": "{fs_title}"}}}},
    {{"name": "vy_lcd", "attrs": {{"x": "{ctr_x}", "y": "{ctr_y}", "width": "{ctr_w}", "height": "{ctr_h}",
      "text": "{frame:04}", "color": "#A3BE8C", "font_family": "roboto", "font_size": "{fs_counter}"}}}},
    {{"name": "vy_gauge", "attrs": {{"x": "{gau_x}", "y": "{gau_y}", "width": "{gau_d}", "height": "{gau_d}",
      "color": "#88C0D0"}}}},
    {{"name": "vy_container", "attrs": {{"x": "{con_x}", "y": "{con_y}", "width": "{con_w}", "height": "{con_h}",
      "radius": "{con_r}", "background": "#2E3440", "border_width": "{bw2}", "border_color": "#4C566A"}},
      "children": [
        {{"name": "vy_circle", "attrs": {{"x": "{mov_x}", "y": "{mov_y}", "width": "{mover_d}", "height": "{mover_d}",
          "background": "#E0501E"}}}}
      ]}},
    {{"name": "vy_slider", "attrs": {{"x": "{sli_x}", "y": "{sli_y}", "width": "{sli_w}", "height": "{sli_h}",
      "value": "{sli_v}"}}}},
    {{"name": "vy_toggle", "attrs": {{"x": "{tog_x}", "y": "{tog_y}", "width": "{tog_w}", "height": "{tog_h}",
      "value": "{tog_v}"}}}},
    {{"name": "vy_progress", "attrs": {{"x": "{pro_x}", "y": "{pro_y}", "width": "{pro_w}", "height": "{pro_h}",
      "value": "{pro_v}"}}}},
    {{"name": "vy_line", "attrs": {{"x": "{rul_x}", "y": "{rul_y}", "width": "{rul_w}", "height": "{rul_h}",
      "background": "#4C566A"}}}},
    {{"name": "vy_label", "attrs": {{"x": "{fot_x}", "y": "{fot_y}", "width": "{fot_w}", "height": "{fot_h}",
      "text": "awto / vyr rig", "color": "#7A869A", "font_family": "roboto", "font_size": "{fs_footer}"}}}},
    {{"name": "vy_image", "attrs": {{"x": "{img_x}", "y": "{img_y}", "width": "24", "height": "24",
      "src": "{src}"}}}}
  ]}}
}}"##,
        hdr_x = sx(8, w),
        hdr_y = sy(6, h),
        hdr_w = sx(464, w),
        hdr_h = sy(34, h),
        hdr_r = sy(6, h),
        ttl_x = sx(20, w),
        ttl_y = sy(14, h),
        ttl_w = sx(200, w),
        ttl_h = sy(18, h),
        ctr_x = sx(380, w),
        ctr_y = sy(13, h),
        ctr_w = sx(88, w),
        ctr_h = sy(20, h),
        gau_x = sx(24, w),
        gau_y = sy(56, h),
        gau_d = sy(80, h),
        con_x = sx(140, w),
        con_y = sy(52, h),
        con_w = sx(200, w),
        con_h = sy(120, h),
        con_r = sy(16, h),
        mov_x = sx(mover_x(frame), w),
        mov_y = sy(36, h),
        sli_x = sx(24, w),
        sli_y = sy(200, h),
        sli_w = sx(300, w),
        sli_h = sy(18, h),
        sli_v = slider_value(frame),
        tog_x = sx(360, w),
        tog_y = sy(198, h),
        tog_w = sx(44, w),
        tog_h = sy(22, h),
        tog_v = if toggle_on(frame) { 1 } else { 0 },
        pro_x = sx(24, w),
        pro_y = sy(230, h),
        pro_w = sx(300, w),
        pro_h = sy(12, h),
        pro_v = progress_value(frame),
        rul_x = sx(8, w),
        rul_y = sy(252, h),
        rul_w = sx(464, w),
        rul_h = sy(2, h).max(1),
        fot_x = sx(8, w),
        fot_y = sy(256, h),
        fot_w = sx(200, w),
        fot_h = sy(12, h),
        img_x = sx(ckx, w),
        img_y = sy(cky, h),
        src = CHECKER_NAME,
    )
}

// --- FNV-1a hashing + the chain ----------------------------------------------

/// FNV-1a 64 offset basis (public so callers can fold chains themselves).
pub const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x100_0000_01b3;

/// FNV-1a 64 — the repo's golden-hash function (`tests/golden.rs`).
pub fn fnv1a(data: &[u8]) -> u64 {
    let mut h = FNV_OFFSET;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(FNV_PRIME);
    }
    h
}

/// Chain step: fold one frame hash (8 LE bytes) into the run hash — the run
/// hash therefore pins frame ORDER as well as content.
pub fn fnv1a_chain(run: u64, frame_hash: u64) -> u64 {
    let mut h = run;
    for b in frame_hash.to_le_bytes() {
        h ^= b as u64;
        h = h.wrapping_mul(FNV_PRIME);
    }
    h
}

// --- the animation driver -------------------------------------------------------

/// What [`run_anim`] proves + measures (pixel counts only — wall timing is
/// the caller's, per the core/shell clock split).
pub struct AnimRun {
    pub w: u32,
    pub h: u32,
    pub frames: u32,
    /// FNV-1a of every frame's RGB888 bytes, in order.
    pub frame_hashes: Vec<u64>,
    /// [`fnv1a_chain`]-folded run hash (seeded [`FNV_OFFSET`]).
    pub run_hash: u64,
    /// Σ merged dirty area across all incremental steps (px).
    pub dirty_px: u64,
    /// Σ full-frame px across those steps — the dirty-% denominator.
    pub step_px: u64,
}

impl AnimRun {
    /// Mean dirty-area percentage per incremental step.
    pub fn dirty_pct(&self) -> f64 {
        if self.step_px == 0 {
            return 0.0;
        }
        100.0 * self.dirty_px as f64 / self.step_px as f64
    }
}

/// Is the frame one flat colour? (Early-exits on the first differing pixel —
/// effectively free on any real frame.)
fn is_blank(buf: &[u8]) -> bool {
    let first = &buf[..3];
    buf.chunks_exact(3).all(|p| p == first)
}

/// Drive `frames` frames of the rig scene at `w×h`, rendering each BOTH ways
/// — full, and incrementally from the previous frame — with three per-frame
/// validations (vyvanse-style, all hard failures naming the frame):
/// non-blank, incremental==full to the byte, and the FNV hash chain folded
/// for the caller's golden compare. `on_frame(i, rgb888)` sees every proven
/// frame (the PNG-dump hook; pass `|_, _| {}` when not dumping).
pub fn run_anim(
    w: u32,
    h: u32,
    frames: u32,
    mut on_frame: impl FnMut(u32, &[u8]),
) -> Result<AnimRun, String> {
    if frames == 0 {
        return Err("frames must be > 0".into());
    }
    let mut fonts = rig_fonts();
    let assets = rig_assets();
    let stride = (w * 3) as usize;
    let area = Rect { x: 0, y: 0, w, h };
    let mut full = vec![0u8; stride * h as usize];
    let mut incr = vec![0u8; stride * h as usize];
    let mut prev: Option<Request> = None;
    let mut frame_hashes = Vec::with_capacity(frames as usize);
    let mut run_hash = FNV_OFFSET;
    let mut dirty_px = 0u64;
    for f in 0..frames {
        let ir = scene_ir(w, h, f);
        let req = Request::parse(&ir).map_err(|e| format!("frame {f}: parse: {e:?}"))?;
        req.render_with(&mut fonts, &assets, area, &mut full, stride)
            .map_err(|e| format!("frame {f}: full render: {e:?}"))?;
        if is_blank(&full) {
            return Err(format!(
                "frame {f}: rendered BLANK (uniform {:?}) — a blank render is a bug (I6)",
                &full[..3]
            ));
        }
        match &prev {
            // Frame 0 seeds the incremental chain with the full render's
            // exact bytes (there is no previous frame to diff against).
            None => incr.copy_from_slice(&full),
            Some(p) => {
                let stats = req
                    .render_incremental(p, &mut fonts, &assets, &mut incr, stride)
                    .map_err(|e| format!("frame {f}: incremental render: {e:?}"))?;
                dirty_px += stats.dirty_area_px;
            }
        }
        if full != incr {
            let i = full
                .iter()
                .zip(incr.iter())
                .position(|(a, b)| a != b)
                .unwrap();
            let (row, col) = (i / stride, (i % stride) / 3);
            let p = i - i % 3;
            return Err(format!(
                "frame {f}: INCREMENTAL != FULL — first divergence at row {row} col {col} \
                 (full rgb {:?} vs incremental {:?}); the dirty set missed real change",
                &full[p..p + 3],
                &incr[p..p + 3],
            ));
        }
        let fh = fnv1a(&full);
        frame_hashes.push(fh);
        run_hash = fnv1a_chain(run_hash, fh);
        on_frame(f, &full);
        prev = Some(req);
    }
    Ok(AnimRun {
        w,
        h,
        frames,
        frame_hashes,
        run_hash,
        dirty_px,
        step_px: (frames as u64 - 1) * w as u64 * h as u64,
    })
}

// --- the committed hash chain ---------------------------------------------------

/// The committed golden form of an [`AnimRun`] (`vyr-rig/hashchain.json`).
/// Hashes serialize as hex strings (JSON numbers lose u64 precision; hex
/// diffs are readable). `arch` is informational and EXCLUDED from
/// [`HashChain::diff`] — cross-ISA equality is the whole point.
#[derive(Debug, serde::Serialize, serde::Deserialize)]
pub struct HashChain {
    pub scene: String,
    pub w: u32,
    pub h: u32,
    pub frames: u32,
    /// The logical frame rate the frame indices represent.
    pub fps: u32,
    pub arch: String,
    pub run_hash: String,
    pub frame_hashes: Vec<String>,
}

impl HashChain {
    pub fn from_run(r: &AnimRun) -> Self {
        HashChain {
            scene: SCENE_VERSION.into(),
            w: r.w,
            h: r.h,
            frames: r.frames,
            fps: 60,
            arch: std::env::consts::ARCH.into(),
            run_hash: format!("{:#018x}", r.run_hash),
            frame_hashes: r
                .frame_hashes
                .iter()
                .map(|h| format!("{h:#018x}"))
                .collect(),
        }
    }

    pub fn parse(json: &str) -> Result<HashChain, String> {
        serde_json::from_str(json).map_err(|e| format!("hash chain parse: {e}"))
    }

    pub fn to_json(&self) -> String {
        serde_json::to_string_pretty(self).expect("hash chain serializes") + "\n"
    }

    /// First difference vs `golden` (None = identical run). Scene/geometry
    /// mismatches are named as such — a stale chain must never read as a
    /// pixel regression.
    pub fn diff(&self, golden: &HashChain) -> Option<String> {
        if self.scene != golden.scene {
            return Some(format!(
                "scene {:?} vs golden {:?} — different scene revisions, re-bless deliberately",
                self.scene, golden.scene
            ));
        }
        if (self.w, self.h, self.frames) != (golden.w, golden.h, golden.frames) {
            return Some(format!(
                "run shape {}x{}@{} vs golden {}x{}@{}",
                self.w, self.h, self.frames, golden.w, golden.h, golden.frames
            ));
        }
        for (i, (a, b)) in self
            .frame_hashes
            .iter()
            .zip(golden.frame_hashes.iter())
            .enumerate()
        {
            if a != b {
                return Some(format!("frame {i}: {a} vs golden {b}"));
            }
        }
        if self.run_hash != golden.run_hash {
            return Some(format!(
                "run hash {} vs golden {} (frame hashes match — chain fold bug?)",
                self.run_hash, golden.run_hash
            ));
        }
        None
    }
}

// --- the resolution ladder --------------------------------------------------------

/// The 60 fps frame budget the headroom columns divide into.
pub const BUDGET_NS: f64 = 1e9 / 60.0;

/// Measurement shape — vyr-bench's warmup + median-of-rounds methodology
/// (`vyr-bench/src/main.rs measure`). Not factored into a shared crate
/// because the rig needs ADAPTIVE per-rung iteration counts (a 4K frame is
/// ~1000× a 120-rung frame; vyr-bench's fixed ITERS=50 doesn't transfer) —
/// the 8-line helper is duplicated, the methodology is the shared thing.
const WARMUP_ROUNDS: usize = 2;
const ROUNDS: usize = 7;
/// Full-frame bench: iterations per round target ~4 Mpx of rendering so a
/// round is long enough to time stably at every rung (≈490 iters at 120,
/// 1 at 4K).
const FULL_TARGET_PX: u64 = 4_000_000;
/// The incremental window: 60 consecutive steps (one logical second)
/// starting at frame 8 — spans two toggle edges (frames 30, 60) and a
/// progress wrap (frame 50), so the per-frame cost prices real edges, not
/// just the cheap steady state.
pub const WINDOW_START: u32 = 8;
/// See [`WINDOW_START`].
pub const WINDOW_STEPS: u32 = 60;

fn median_round_ns(mut round: impl FnMut() -> f64) -> f64 {
    for _ in 0..WARMUP_ROUNDS {
        round();
    }
    let mut r: Vec<f64> = (0..ROUNDS).map(|_| round()).collect();
    r.sort_by(|a, b| a.partial_cmp(b).unwrap());
    r[ROUNDS / 2]
}

/// One ladder rung's measured numbers (medians; derivations as methods).
#[derive(Debug, Clone, Copy)]
pub struct RungReport {
    pub w: u32,
    pub h: u32,
    /// Median ns per FULL frame render.
    pub full_ns: f64,
    /// Median over rounds of (mean ns per incremental frame across the
    /// window) — diff cost included, exactly the runtime loop.
    pub incr_ns: f64,
    /// Mean merged dirty px per window step (deterministic — computed from
    /// `dirty_rects`, no timing involved).
    pub dirty_px: f64,
}

impl RungReport {
    pub fn px(&self) -> f64 {
        self.w as f64 * self.h as f64
    }
    pub fn full_ns_px(&self) -> f64 {
        self.full_ns / self.px()
    }
    pub fn incr_ns_dirty_px(&self) -> f64 {
        self.incr_ns / self.dirty_px
    }
    pub fn dirty_pct(&self) -> f64 {
        100.0 * self.dirty_px / self.px()
    }
    pub fn fps_full(&self) -> f64 {
        1e9 / self.full_ns
    }
    pub fn fps_incr(&self) -> f64 {
        1e9 / self.incr_ns
    }
    /// ×60fps-budget headroom (>1 = fits the 16.67 ms budget).
    pub fn headroom_full(&self) -> f64 {
        BUDGET_NS / self.full_ns
    }
    /// See [`RungReport::headroom_full`].
    pub fn headroom_incr(&self) -> f64 {
        BUDGET_NS / self.incr_ns
    }
}

/// Measure one rung (module docs methodology). Renders are `expect`ed — the
/// scene is the rig's own; a render failure here is a bug, not a result.
pub fn measure_rung(w: u32, h: u32) -> RungReport {
    let mut fonts = rig_fonts();
    let assets = rig_assets();
    let stride = (w * 3) as usize;
    let area = Rect { x: 0, y: 0, w, h };
    // The window's requests, parsed OUTSIDE all timing (the runtime loop
    // being priced is render+diff, not JSON).
    let reqs: Vec<Request> = (WINDOW_START..=WINDOW_START + WINDOW_STEPS)
        .map(|f| Request::parse(&scene_ir(w, h, f)).expect("rig scene parses"))
        .collect();
    let mut buf = vec![0u8; stride * h as usize];

    let iters = (FULL_TARGET_PX / (w as u64 * h as u64)).max(1);
    let full_ns = median_round_ns(|| {
        let t0 = Instant::now();
        for _ in 0..iters {
            reqs[0]
                .render_with(&mut fonts, &assets, area, &mut buf, stride)
                .expect("rung full render");
        }
        t0.elapsed().as_nanos() as f64 / iters as f64
    });

    // Incremental: evolve the window's 60 steps on top of the start frame;
    // the restore is OUTSIDE the clock (presentation copies are the flush
    // layer's cost, not the render loop's).
    let mut start = vec![0u8; stride * h as usize];
    reqs[0]
        .render_with(&mut fonts, &assets, area, &mut start, stride)
        .expect("window start render");
    let mut frame = start.clone();
    let incr_ns = median_round_ns(|| {
        frame.copy_from_slice(&start);
        let t0 = Instant::now();
        for s in 0..WINDOW_STEPS as usize {
            reqs[s + 1]
                .render_incremental(&reqs[s], &mut fonts, &assets, &mut frame, stride)
                .expect("window incremental render");
        }
        t0.elapsed().as_nanos() as f64 / WINDOW_STEPS as f64
    });

    let dirty_total: u64 = (0..WINDOW_STEPS as usize)
        .map(|s| {
            dirty_rects(&reqs[s], &reqs[s + 1])
                .iter()
                .map(|r| r.w as u64 * r.h as u64)
                .sum::<u64>()
        })
        .sum();
    RungReport {
        w,
        h,
        full_ns,
        incr_ns,
        dirty_px: dirty_total as f64 / WINDOW_STEPS as f64,
    }
}

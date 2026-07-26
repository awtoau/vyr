//! vyr-scene — frame-indexed animated test scenes, as IR JSON.
//!
//! `no_std + alloc` **on purpose**: the same generator has to run on the host
//! (vyr-rig: hash chain, PNG dump, resolution ladder) and inside the
//! emulated-M4 measurement vehicle (vyr-size `--features run-qemu,rig`). One
//! generator means the two legs animate the *same* pixels, so the M4's
//! instruction counts and the host's dirty-rect statistics describe one scene
//! rather than two.
//!
//! # Two animation paths, one source of truth (#55)
//!
//! [`scene_ir`] emits the scene as an IR JSON STRING for frame *f* — the
//! reference oracle (the goldens, the optional cross-ISA JSON emit, the test's
//! `A` render). It is total and referentially transparent (below).
//!
//! [`scene_tree`] + [`animate`] are the FAST path a real animating device runs
//! and the rig now measures: parse the scene ONCE into a typed
//! [`vyr_core::ir::Request`], record the index-path of every animating node in
//! [`Handles`], then per frame mutate ONLY those fields — writing the typed
//! `Geom`/`Resolved` caches directly through vyr-core's setters, with no
//! `format!`, no re-parse and no whole-tree re-resolve. `animate` drives every
//! field from the SAME driver expressions `scene_ir` uses, so a persistent
//! tree animated to frame *f* renders byte-for-byte like `Request::parse` of
//! `scene_ir(_, _, f, _)` — the property `vyr-rig/tests` asserts across a frame
//! sweep, both detail levels and all three quality tiers.
//!
//! Three rules the scene obeys, all of them load-bearing:
//!
//! 1. **Pure function of the frame index.** No clock, no randomness, no
//!    state carried between frames. `scene_ir(w, h, f, detail)` is total and
//!    referentially transparent, so frame *f* is byte-identical on every
//!    platform and re-running frame *f* after frame *f+900* gives the same
//!    IR (invariant I2).
//! 2. **Integers only.** The generator runs INSIDE the M4's measured window,
//!    so its own cost is charged to the renderer. `libm::sinf` costs ~1,145
//!    M4 instructions per call (soft-f64 kernels — see
//!    `docs/measurements/perf-history.md`, #32), which would be scene
//!    generation masquerading as render cost. Circular motion comes from a
//!    committed Q12 sine table ([`SIN_Q12`], regenerate with
//!    `scripts/gen-sintab.py`) and everything else is integer arithmetic.
//! 3. **Design-space geometry.** Coordinates are authored in a 480×270
//!    design space and scaled by [`sx`]/[`sy`], so the scene runs at every
//!    rung of vyr-rig's resolution ladder without a second layout.
//!
//! # What [`LONG`] exercises that the F18 rig scene does not
//!
//! | element | the cost/failure mode it puts under load |
//! |---|---|
//! | ticker label whose CONTENT rolls through the printable-ASCII repertoire | **glyph-cache churn** — the cache is an unevicted `BTreeMap` in heap, and the old fixture only ever asked for the same dozen glyphs |
//! | `F%05d` counter | **near-static frames** — one digit changes, a few hundred dirty px |
//! | idle phase (frames 41..49 of every 50) | frames where *nothing* but the counter moves — the low tail of the dirty distribution |
//! | root `background` cycling every 50 frames | **full-frame repaints** — the top of the dirty distribution (dirty-rect diffing dirties the whole screen on a root attr change) |
//! | translucent panel that jumps between three slots | the **~30–40 % dirty** middle of the distribution, plus translucent fills and z-order restacking |
//! | `vy_chart` line trace scrolling, `vy_chart` bars | polyline + marker discs + grid; the chart is the scene that binds the gutter/band-equivalence work (#38/#40/#42) |
//! | `vy_gauge` whose diameter breathes over 13 distinct values | **contour-memo churn** (#32, 8 KiB budget) — a memo that only ever sees one radius is not a memo under test |
//! | two discs orbiting through a rounded container's clip edges | curves + **rounded clipping** + widgets leaving/entering the clip |
//! | checker blitted at natural size AND `fit: cover` | **image blits**, unscaled and scaled |
//!
//! Rotation and scale are not in the IR (#17 is open), so rotational motion
//! is expressed the way the current vocabulary allows: orbiting discs, a
//! scrolling trace, and a breathing ring.

#![no_std]
#![forbid(unsafe_code)]

extern crate alloc;

use alloc::string::String;
use alloc::vec::Vec;
use core::fmt::Write as _;
use vyr_core::ir::{Request, parse_color};

// --- suite identity ----------------------------------------------------------

/// Version of the *measurement suite* this crate contributes to (#43: the
/// suite is versioned data — adding a scene is a suite bump that obliges a
/// replay of the ledger's history against the new suite). Recorded in every
/// hash chain so a chain can never be silently compared across suites.
pub const SUITE_VERSION: &str = "suite-2";

/// The design space every rung is an integer scale of (shared with
/// `vyr_rig::DESIGN_W`, deliberately: one design space, two scenes).
pub const DESIGN_W: i64 = 480;
/// See [`DESIGN_W`].
pub const DESIGN_H: i64 = 270;

/// The logical frame rate a frame index represents.
pub const FPS: u32 = 60;

/// The deep preset's length, in frames — 20 s at [`FPS`].
///
/// NOT a period in the strict sense: the drivers' cycles are deliberately
/// coprime (24, 25, 30, 50, 100 and the ticker's 279), so the scene as a
/// whole does not repeat inside any preset, and [`counter_text`] never
/// repeats at all. What IS true, and is what makes a long run meaningful,
/// is that every driver's VALUE SET is bounded and fully visited well inside
/// this length (asserted). Beyond it a run stops adding coverage and becomes
/// purely an endurance test: heap drift, unbounded caches.
pub const PERIOD: u32 = 1200;

/// Theme/animation block length, in frames. A block is [`ACTIVE`] frames of
/// motion followed by an idle tail, and the root backdrop changes at every
/// block boundary (a root attr change dirties the whole screen).
pub const BLOCK: u32 = 50;
/// Frames of motion at the head of each [`BLOCK`]; the remainder are idle
/// (only the frame counter advances) — the near-static tail of the
/// dirty-fraction distribution.
pub const ACTIVE: u32 = 40;

// --- detail levels -----------------------------------------------------------

/// How much of the scene to draw. The two levels are separate *scenes* with
/// separate goldens, not a quality knob.
///
/// [`Detail::Full`] is the headline scene. [`Detail::Lite`] exists because
/// the MCU arena is 122,880 B and the Exact tier already peaks at 112,473 B
/// of it: `Lite` shrinks the two unbounded consumers (the glyph repertoire
/// and the chart series) so the scene fits an F429-class part, and it is a
/// deliberate, measured variant rather than a silent shrink of the real one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Detail {
    /// The full scene — 93-glyph ticker repertoire, 40-point trace, bar chart.
    Full,
    /// The MCU-fitting variant — 39-glyph repertoire, 20-point trace, no bars.
    Lite,
}

impl Detail {
    /// The scene id recorded in hash chains and baselines. Bump the revision
    /// suffix whenever [`scene_ir`] changes so a stale chain fails loudly as
    /// "different scene", never as a phantom pixel regression.
    pub fn scene_id(self) -> &'static str {
        match self {
            Detail::Full => "long-v1",
            Detail::Lite => "long-lite-v1",
        }
    }

    /// Parse the CLI spelling (`full` / `lite`).
    pub fn parse(s: &str) -> Option<Detail> {
        match s {
            "full" => Some(Detail::Full),
            "lite" => Some(Detail::Lite),
            _ => None,
        }
    }

    fn repertoire(self) -> &'static str {
        match self {
            // Printable ASCII (U+0020..U+007E — exactly the subset font's
            // range) minus the two JSON-hostile bytes, `"` and `\`.
            Detail::Full => {
                " !#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[]^_\
                 `abcdefghijklmnopqrstuvwxyz{|}~"
            }
            Detail::Lite => "0123456789 ABCDEFGHIJKLMNOPQRSTUVWXYZ.-",
        }
    }

    fn trace_points(self) -> u32 {
        match self {
            Detail::Full => 40,
            Detail::Lite => 20,
        }
    }

    fn bars(self) -> bool {
        self == Detail::Full
    }
}

// --- frame-count presets -----------------------------------------------------

/// Named frame counts. A 1200-frame run is genuinely expensive (see the
/// wall-time table in the measurement report), so the frame count is a
/// PRESET rather than a constant: `Smoke` after every change, `Full` for
/// periodic deep runs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Preset {
    /// 60 frames — 1.0 s. One theme block: every animation class appears
    /// (motion, idle tail, one full-frame repaint) at least once.
    Smoke,
    /// 300 frames — 5.0 s. Six theme blocks; enough to see the glyph cache
    /// stop growing and the memo reach steady state.
    Short,
    /// 1200 frames — 20.0 s ([`PERIOD`]). Every driver's value set is fully
    /// visited long before the end, so the tail is endurance: does the heap
    /// peak settle, does an unevicted cache stop growing?
    Full,
}

impl Preset {
    /// Frames in this preset.
    pub fn frames(self) -> u32 {
        match self {
            Preset::Smoke => 60,
            Preset::Short => 300,
            Preset::Full => PERIOD,
        }
    }

    /// The CLI spelling.
    pub fn name(self) -> &'static str {
        match self {
            Preset::Smoke => "smoke",
            Preset::Short => "short",
            Preset::Full => "full",
        }
    }

    /// Parse the CLI spelling.
    pub fn parse(s: &str) -> Option<Preset> {
        match s {
            "smoke" => Some(Preset::Smoke),
            "short" => Some(Preset::Short),
            "full" => Some(Preset::Full),
            _ => None,
        }
    }

    /// Every preset, cheapest first.
    pub const ALL: &'static [Preset] = &[Preset::Smoke, Preset::Short, Preset::Full];
}

// --- the animation drivers (all pure functions of the frame index) -----------

/// Q12 sine over a 256-step turn: `SIN_Q12[i] == round(4096·sin(2πi/256))`.
/// Committed rather than computed — see the module docs (rule 2).
/// Regenerate: `python3 scripts/gen-sintab.py`.
pub static SIN_Q12: [i32; 256] = [
    0, 101, 201, 301, 401, 501, 601, 700, 799, 897, 995, 1092, 1189, 1285, 1380, 1474, 1567, 1660,
    1751, 1842, 1931, 2019, 2106, 2191, 2276, 2359, 2440, 2520, 2598, 2675, 2751, 2824, 2896, 2967,
    3035, 3102, 3166, 3229, 3290, 3349, 3406, 3461, 3513, 3564, 3612, 3659, 3703, 3745, 3784, 3822,
    3857, 3889, 3920, 3948, 3973, 3996, 4017, 4036, 4052, 4065, 4076, 4085, 4091, 4095, 4096, 4095,
    4091, 4085, 4076, 4065, 4052, 4036, 4017, 3996, 3973, 3948, 3920, 3889, 3857, 3822, 3784, 3745,
    3703, 3659, 3612, 3564, 3513, 3461, 3406, 3349, 3290, 3229, 3166, 3102, 3035, 2967, 2896, 2824,
    2751, 2675, 2598, 2520, 2440, 2359, 2276, 2191, 2106, 2019, 1931, 1842, 1751, 1660, 1567, 1474,
    1380, 1285, 1189, 1092, 995, 897, 799, 700, 601, 501, 401, 301, 201, 101, 0, -101, -201, -301,
    -401, -501, -601, -700, -799, -897, -995, -1092, -1189, -1285, -1380, -1474, -1567, -1660,
    -1751, -1842, -1931, -2019, -2106, -2191, -2276, -2359, -2440, -2520, -2598, -2675, -2751,
    -2824, -2896, -2967, -3035, -3102, -3166, -3229, -3290, -3349, -3406, -3461, -3513, -3564,
    -3612, -3659, -3703, -3745, -3784, -3822, -3857, -3889, -3920, -3948, -3973, -3996, -4017,
    -4036, -4052, -4065, -4076, -4085, -4091, -4095, -4096, -4095, -4091, -4085, -4076, -4065,
    -4052, -4036, -4017, -3996, -3973, -3948, -3920, -3889, -3857, -3822, -3784, -3745, -3703,
    -3659, -3612, -3564, -3513, -3461, -3406, -3349, -3290, -3229, -3166, -3102, -3035, -2967,
    -2896, -2824, -2751, -2675, -2598, -2520, -2440, -2359, -2276, -2191, -2106, -2019, -1931,
    -1842, -1751, -1660, -1567, -1474, -1380, -1285, -1189, -1092, -995, -897, -799, -700, -601,
    -501, -401, -301, -201, -101,
];

/// Q12 sine of a 256-step turn index (wraps).
pub fn sin_q(i: u32) -> i32 {
    SIN_Q12[(i % 256) as usize]
}

/// Q12 cosine of a 256-step turn index (wraps).
pub fn cos_q(i: u32) -> i32 {
    sin_q(i + 64)
}

/// `amp·sin(turn)` in whole pixels, rounding toward zero — the only rounding
/// mode used anywhere in this crate, so the scene has one truncation story.
fn wave(amp: i64, turn: u32) -> i64 {
    amp * sin_q(turn) as i64 / 4096
}

/// The MOTION clock. Every animated property except the frame counter reads
/// this instead of the raw frame index, which freezes the scene for the tail
/// of each [`BLOCK`]: frames 41..49 of every 50 differ from their predecessor
/// only in one LCD digit. Without a deliberate quiet phase every frame would
/// land in the same dirty-fraction bucket and the dirty-rect cost model
/// (`docs/performance.md` §4.2) would be untested at the low end.
pub fn motion_frame(f: u32) -> u32 {
    let block = f / BLOCK;
    let within = f % BLOCK;
    block * BLOCK + if within > ACTIVE { ACTIVE } else { within }
}

/// Is this frame in the idle tail of its block? (Informational — the scene
/// itself derives everything from [`motion_frame`].)
pub fn is_idle(f: u32) -> bool {
    f % BLOCK > ACTIVE
}

/// Backdrop palette index for frame `f`. Changing the ROOT's `background`
/// dirties the whole screen by construction (`vyr_core::dirty`), so this is
/// the scene's full-frame-repaint event, once per [`BLOCK`].
pub fn theme(f: u32) -> usize {
    ((f / BLOCK) % 4) as usize
}

/// Backdrop colours, indexed by [`theme`].
pub const BACKDROPS: [&str; 4] = ["#22262B", "#1B2430", "#241F2B", "#1E2B26"];
/// Card/panel colours, indexed by [`theme`].
pub const CARDS: [&str; 4] = ["#2E3440", "#26313D", "#322A3C", "#26382F"];
/// Accent colours, indexed by [`theme`].
pub const ACCENTS: [&str; 4] = ["#88C0D0", "#E0A85C", "#C08AD0", "#8AD0A2"];

/// The frame-counter text: `F` + five digits of the RAW frame index, so it
/// advances even through the idle tail. Ten digit glyphs ever, and typically
/// only the last one changes — the near-static frame.
pub fn counter_text(f: u32) -> String {
    let mut s = String::with_capacity(6);
    let _ = write!(s, "F{:05}", f % 100_000);
    s
}

/// The glyph-churn driver: a 26-character window that advances one character
/// every 3 motion frames over the detail level's ASCII repertoire. The
/// repertoire is walked in ~279 frames at [`Detail::Full`], so a `short` run
/// sees the glyph cache fill and a `full` run sees whether it then stops
/// growing — the cache has NO eviction, so unbounded growth here is a real
/// MCU failure mode and this is what puts it under load.
pub fn ticker_text(f: u32, detail: Detail) -> String {
    const WINDOW: usize = 26;
    let rep = detail.repertoire().as_bytes();
    let start = (motion_frame(f) / 3) as usize % rep.len();
    let mut s = String::with_capacity(WINDOW);
    for k in 0..WINDOW {
        s.push(rep[(start + k) % rep.len()] as char);
    }
    s
}

/// The scrolling chart trace as a `points` CSV, 0..100. Two harmonics with
/// coprime steps so the wave never repeats within a block; the per-sample
/// step is deliberately small (≤ ~3 design px between neighbours) so no
/// polyline segment has a large vertical extent — the required band overscan
/// tracks exactly that (#38), and a scene that trips the shipped #42 gutter
/// defect would be testing the bug rather than the animation.
pub fn trace_csv(f: u32, detail: Detail) -> String {
    let t = motion_frame(f);
    let n = detail.trace_points();
    let mut s = String::with_capacity(n as usize * 4);
    for i in 0..n {
        if i > 0 {
            s.push(',');
        }
        let v = 50 + wave(30, t * 2 + i * 6) + wave(8, t * 5 + i * 13);
        let _ = write!(s, "{}", v.clamp(0, 100));
    }
    s
}

/// The bar-chart series ([`Detail::Full`] only) — twelve bars on a slower,
/// counter-rotating phase so the two charts never move in lockstep.
pub fn bars_csv(f: u32) -> String {
    let t = motion_frame(f);
    let mut s = String::with_capacity(48);
    for i in 0..12u32 {
        if i > 0 {
            s.push(',');
        }
        let v = 52 + wave(34, 256 - (t + i * 17) % 256);
        let _ = write!(s, "{}", v.clamp(2, 100));
    }
    s
}

/// Gauge diameter in design px: a 24-frame triangle over 13 distinct values.
/// Each distinct diameter is a distinct pair of ring contours, so the #32
/// contour memo (8 KiB budget) is asked to hold thirteen of them instead of
/// one — the churn case the memo has never been measured under.
pub fn gauge_d(f: u32) -> i64 {
    let p = motion_frame(f) % 24;
    60 + if p <= 12 { p as i64 } else { 24 - p as i64 }
}

/// Orbit position of a disc inside its container, in design px RELATIVE to
/// the container: `(rx, ry)` is the ellipse, `turn` the 256-step angle,
/// `d` the disc diameter. Disc B's ellipse is wider than the container, so
/// it crosses the rounded clip edge twice per orbit.
pub fn orbit(cw: i64, ch: i64, rx: i64, ry: i64, turn: u32, d: i64) -> (i64, i64) {
    (
        cw / 2 + rx * cos_q(turn) as i64 / 4096 - d / 2,
        ch / 2 + ry * sin_q(turn) as i64 / 4096 - d / 2,
    )
}

/// Translucent panel slot (0..2) — jumps every 25 motion frames. The jump's
/// WAS ∪ NOW dirty region is the ~30–40 % middle of the dirty distribution.
pub fn panel_slot(f: u32) -> u32 {
    (motion_frame(f) / 25) % 3
}

/// Slider value: triangle 0..100..0, period 100 motion frames.
pub fn slider_value(f: u32) -> i64 {
    let p = (motion_frame(f) % 100) as i64;
    if p <= 50 { p * 2 } else { (100 - p) * 2 }
}

/// Progress sawtooth 0..98, period 50 motion frames.
pub fn progress_value(f: u32) -> i64 {
    ((motion_frame(f) % 50) * 2) as i64
}

/// Toggle square wave, flipping every 30 motion frames.
pub fn toggle_on(f: u32) -> bool {
    (motion_frame(f) / 30).is_multiple_of(2)
}

// --- design-space scaling ------------------------------------------------------

/// Design-x → screen px (floor; exact for the 16:9 ladder rungs).
pub fn sx(v: i64, w: u32) -> i64 {
    v * w as i64 / DESIGN_W
}

/// Design-y → screen px.
pub fn sy(v: i64, h: u32) -> i64 {
    v * h as i64 / DESIGN_H
}

/// The registry key the scene's `src` attrs reference (the F6 verbatim-name
/// contract — the same 24×24 checker both legs already bake in).
pub const CHECKER_NAME: &str = "checker-24.png";

// --- the scene -------------------------------------------------------------------

/// The long animated scene as IR JSON: a pure function of `frame`.
///
/// `w`/`h` are the target frame size (any 16:9 ladder rung); geometry is the
/// [`DESIGN_W`]×[`DESIGN_H`] design space scaled through [`sx`]/[`sy`], and
/// font sizes floor at 1 px because size 0 is junk IR.
///
/// Needs `roboto` in the font registry and [`CHECKER_NAME`] in the asset
/// registry (both legs already register exactly those).
pub fn scene_ir(w: u32, h: u32, frame: u32, detail: Detail) -> String {
    let th = theme(frame);
    let t = motion_frame(frame);
    let backdrop = BACKDROPS[th];
    let card = CARDS[th];
    let accent = ACCENTS[th];

    let fs_body = sy(14, h).max(1);
    let fs_lcd = sy(20, h).max(1);
    let fs_small = sy(10, h).max(1);
    let bw1 = sy(1, h).max(1);
    let bw2 = sy(2, h).max(1);

    let mut kids: Vec<String> = Vec::with_capacity(20);

    // --- header: static chrome + a static title (the cheap baseline) -------
    kids.push(rect(
        "vy_frame",
        sx(8, w),
        sy(6, h),
        sx(464, w),
        sy(30, h),
        &alloc::format!(
            r##""background": "{card}", "radius": "{}", "border_width": "{bw1}", "border_color": "#4C566A""##,
            sy(6, h)
        ),
    ));
    kids.push(text_node(
        "vy_label",
        sx(20, w),
        sy(12, h),
        sx(200, w),
        sy(18, h),
        "vyr long scene",
        "#ECEFF4",
        fs_body,
    ));
    // The near-static element: one digit per frame, and the ONLY thing that
    // moves through the idle tail.
    kids.push(text_node(
        "vy_lcd",
        sx(378, w),
        sy(9, h),
        sx(92, w),
        sy(24, h),
        &counter_text(frame),
        "#A3BE8C",
        fs_lcd,
    ));

    // --- the glyph-cache pressure: rolling ASCII ---------------------------
    kids.push(text_node(
        "vy_label",
        sx(20, w),
        sy(40, h),
        sx(440, w),
        sy(18, h),
        &ticker_text(frame, detail),
        "#D8DEE9",
        fs_body,
    ));

    // --- the breathing gauge (contour-memo churn) --------------------------
    let gd = gauge_d(frame);
    kids.push(rect(
        "vy_gauge",
        sx(20, w),
        sy(62 + (72 - gd) / 2, h),
        sy(gd, h),
        sy(gd, h),
        &alloc::format!(r##""color": "{accent}""##),
    ));

    // --- the clipped orbit container ---------------------------------------
    let (cw, ch) = (200i64, 116i64);
    let (ax, ay) = orbit(cw, ch, 54, 30, t * 3, 44);
    let (bx, by) = orbit(cw, ch, 118, 66, 256 - (t * 5) % 256, 22);
    let (ix, iy) = orbit(cw, ch, 70, 40, t * 2 + 90, 40);
    kids.push(alloc::format!(
        r##"{{"name": "vy_container", "attrs": {{"x": "{cx}", "y": "{cy}", "width": "{cwp}", "height": "{chp}",
      "radius": "{crad}", "background": "{card}", "border_width": "{bw2}", "border_color": "#4C566A"}},
      "children": [
        {{"name": "vy_circle", "attrs": {{"x": "{axp}", "y": "{ayp}", "width": "{adp}", "height": "{adp}",
          "background": "#E0501E"}}}},
        {{"name": "vy_circle", "attrs": {{"x": "{bxp}", "y": "{byp}", "width": "{bdp}", "height": "{bdp}",
          "background": "{accent}"}}}},
        {{"name": "vy_image", "attrs": {{"x": "{ixp}", "y": "{iyp}", "width": "{idp}", "height": "{idp}",
          "src": "{src}", "fit": "cover"}}}}
      ]}}"##,
        cx = sx(136, w),
        cy = sy(62, h),
        cwp = sx(cw, w),
        chp = sy(ch, h),
        crad = sy(16, h),
        axp = sx(ax, w),
        ayp = sy(ay, h),
        adp = sy(44, h),
        bxp = sx(bx, w),
        byp = sy(by, h),
        bdp = sy(22, h),
        ixp = sx(ix, w),
        iyp = sy(iy, h),
        idp = sy(40, h),
        src = CHECKER_NAME,
    ));

    // --- the scrolling trace ------------------------------------------------
    kids.push(alloc::format!(
        r##"{{"name": "vy_chart", "attrs": {{"x": "{x}", "y": "{y}", "width": "{cw2}", "height": "{ch2}",
      "points": "{pts}", "range_min": "0", "range_max": "100", "div_count_x": "5", "div_count_y": "3",
      "line_color": "{accent}", "line_width": "2", "background": "{card}"}}}}"##,
        x = sx(348, w),
        y = sy(62, h),
        cw2 = sx(124, w),
        ch2 = sy(56, h),
        pts = trace_csv(frame, detail),
    ));
    if detail.bars() {
        kids.push(alloc::format!(
            r##"{{"name": "vy_chart", "attrs": {{"x": "{x}", "y": "{y}", "width": "{cw2}", "height": "{ch2}",
      "chart_type": "bar", "points": "{pts}", "range_min": "0", "range_max": "100", "div_count_y": "3",
      "line_color": "#8FBCBB", "background": "{card}"}}}}"##,
            x = sx(348, w),
            y = sy(124, h),
            cw2 = sx(124, w),
            ch2 = sy(54, h),
            pts = bars_csv(frame),
        ));
    }

    // --- the translucent panel: the ~30-40% dirty event ---------------------
    let slot = panel_slot(frame);
    let px = [16i64, 96, 176][slot as usize];
    kids.push(rect(
        "vy_frame",
        sx(px, w),
        sy(126, h),
        sx(280, w),
        sy(88, h),
        &alloc::format!(
            r##""background": "{accent}", "opacity": "{opa}", "radius": "{rad}", "border_width": "{bw1}", "border_color": "#4C566A""##,
            opa = 88 + slot * 40,
            rad = sy(10, h)
        ),
    ));
    // A button (clips its child; the child's ink is inherited) sitting ON the
    // panel, so the translucent fill has real content under and over it.
    // The label declares the button's own rect explicitly. It is tempting to
    // omit x/y/width/height (`TEXT_IR` does, and `align: center` centres in
    // the PARENT either way) — but a 0×0 child is culled on a bbox inflated
    // by 32 px, while a centred run paints wherever the parent's centre puts
    // it. Any dirty rect covering the text but not the button's top-left
    // corner then renders the button WITHOUT its caption. See the report:
    // that mismatch is a real band-equivalence defect in vyr, found by this
    // scene; the scene declares the rect so it measures animation rather
    // than re-testing a known bug.
    kids.push(alloc::format!(
        r##"{{"name": "vy_button", "attrs": {{"x": "{x}", "y": "{y}", "width": "{bw_}", "height": "{bh_}",
      "background": "#3B4252", "radius": "{rad}", "color": "#ECEFF4"}},
      "children": [{{"name": "vy_label", "attrs": {{"x": "0", "y": "0", "width": "{bw_}", "height": "{bh_}",
        "text": "RUN {slot}", "align": "center",
        "font_family": "roboto", "font_size": "{fs_body}"}}}}]}}"##,
        x = sx(px + 190, w),
        y = sy(140, h),
        bw_ = sx(76, w),
        bh_ = sy(26, h),
        rad = sy(6, h),
    ));

    // --- cheap widgets on their own periods ---------------------------------
    kids.push(rect(
        "vy_slider",
        sx(16, w),
        sy(222, h),
        sx(232, w),
        sy(16, h),
        &alloc::format!(r##""value": "{}""##, slider_value(frame)),
    ));
    kids.push(rect(
        "vy_progress",
        sx(16, w),
        sy(244, h),
        sx(232, w),
        sy(10, h),
        &alloc::format!(r##""value": "{}""##, progress_value(frame)),
    ));
    kids.push(rect(
        "vy_toggle",
        sx(262, w),
        sy(220, h),
        sx(44, w),
        sy(22, h),
        &alloc::format!(r##""value": "{}""##, i32::from(toggle_on(frame))),
    ));

    // --- the natural-size roaming blit + the footer --------------------------
    let rx = 320 + wave(140, t * 4);
    let ry = 226 + wave(14, t * 7);
    kids.push(alloc::format!(
        r##"{{"name": "vy_image", "attrs": {{"x": "{x}", "y": "{y}", "width": "24", "height": "24",
      "src": "{src}"}}}}"##,
        x = sx(rx, w),
        y = sy(ry, h),
        src = CHECKER_NAME,
    ));
    kids.push(rect(
        "vy_line",
        sx(8, w),
        sy(258, h),
        sx(464, w),
        sy(2, h).max(1),
        r##""background": "#4C566A""##,
    ));
    kids.push(text_node(
        "vy_label",
        sx(8, w),
        sy(260, h),
        sx(240, w),
        sy(10, h),
        "awto / vyr long scene",
        "#7A869A",
        fs_small,
    ));

    let mut out = String::with_capacity(4096);
    let _ = write!(
        out,
        "{{\n  \"schema_version\": \"0.6-vyvanse\",\n  \"w\": {w}, \"h\": {h},\n  \
         \"root\": {{\"name\": \"view\", \"attrs\": {{\"background\": \"{backdrop}\"}}, \
         \"children\": [\n    {}\n  ]}}\n}}\n",
        kids.join(",\n    ")
    );
    out
}

/// One `{name, attrs}` node with an absolute rect plus `extra` (a
/// pre-rendered `"k": "v"` attr list, no leading comma).
fn rect(name: &str, x: i64, y: i64, w: i64, h: i64, extra: &str) -> String {
    let mut s = String::with_capacity(160);
    let _ = write!(
        s,
        r##"{{"name": "{name}", "attrs": {{"x": "{x}", "y": "{y}", "width": "{w}", "height": "{h}", {extra}}}}}"##
    );
    s
}

/// A text-bearing node (label / lcd) at an explicit size in the `roboto`
/// family — the one font both legs register.
#[allow(clippy::too_many_arguments)]
fn text_node(
    name: &str,
    x: i64,
    y: i64,
    w: i64,
    h: i64,
    text: &str,
    color: &str,
    size: i64,
) -> String {
    rect(
        name,
        x,
        y,
        w,
        h,
        &alloc::format!(
            r##""text": "{text}", "color": "{color}", "font_family": "roboto", "font_size": "{size}""##
        ),
    )
}

// --- the persistent typed tree (#55) -----------------------------------------

/// Design-x → screen px as the render core's geom cast stores it: the parse
/// path resolves `x`/`width` as `(decimal(sx(v)) → f32) as i32`. The scaled
/// coordinates here are exact integers far inside f32's 24-bit mantissa, so
/// routing through `f32` reproduces that cast byte-for-byte (#55).
fn gx(v: i64, w: u32) -> i32 {
    sx(v, w) as f32 as i32
}
/// See [`gx`].
fn gy(v: i64, h: u32) -> i32 {
    sy(v, h) as f32 as i32
}
/// See [`gx`] — the `width`/`height` form (`u32`, clamped at 0 like the parse
/// path's `u32_attr`).
fn gxu(v: i64, w: u32) -> u32 {
    (sx(v, w) as f32).max(0.0) as u32
}
/// See [`gxu`].
fn gyu(v: i64, h: u32) -> u32 {
    (sy(v, h) as f32).max(0.0) as u32
}

/// The index-paths of every animating node in the long scene, recorded once so
/// [`animate`] can address them positionally without re-searching. The tree's
/// structure (node count and order) is frame-invariant within a detail level,
/// so the paths are stable for the life of the run; only the presence of the
/// bar chart ([`Detail::Full`] only) shifts the indices after the trace chart.
#[derive(Debug, Clone)]
pub struct Handles {
    detail: Detail,
    /// header card (`vy_frame`) — its `background` tracks the theme.
    header: usize,
    /// the frame-counter `vy_lcd`.
    lcd: usize,
    /// the rolling-ASCII ticker `vy_label`.
    ticker: usize,
    /// the breathing `vy_gauge` (geometry + accent colour).
    gauge: usize,
    /// the orbit `vy_container`; its `background` tracks the theme and its
    /// three children (circle A `[container,0]`, circle B `[container,1]`,
    /// image `[container,2]`) orbit.
    container: usize,
    /// the scrolling trace `vy_chart`.
    trace: usize,
    /// the bar `vy_chart` — [`Detail::Full`] only.
    bars: Option<usize>,
    /// the translucent `vy_frame` panel (slot geometry, accent, opacity).
    panel: usize,
    /// the `vy_button` (slot geometry); its label is `[button,0]`.
    button: usize,
    /// the `vy_slider`.
    slider: usize,
    /// the `vy_progress`.
    progress: usize,
    /// the `vy_toggle`.
    toggle: usize,
    /// the natural-size roaming `vy_image`.
    roaming: usize,
}

impl Handles {
    /// The stable paths for a detail level — computed from the emit order in
    /// [`scene_ir`] (header 0, title 1, lcd 2, ticker 3, gauge 4, container 5,
    /// trace 6, then the bar chart at 7 for `Full` only, shifting the tail).
    fn for_detail(detail: Detail) -> Handles {
        let mut i = 7usize;
        let bars = if detail.bars() {
            let b = i;
            i += 1;
            Some(b)
        } else {
            None
        };
        let panel = i;
        let button = i + 1;
        let slider = i + 2;
        let progress = i + 3;
        let toggle = i + 4;
        let roaming = i + 5;
        Handles {
            detail,
            header: 0,
            lcd: 2,
            ticker: 3,
            gauge: 4,
            container: 5,
            trace: 6,
            bars,
            panel,
            button,
            slider,
            progress,
            toggle,
            roaming,
        }
    }
}

/// Parse the long scene ONCE (frame 0) into a persistent typed [`Request`] and
/// record the [`Handles`] of its animating nodes. [`animate`] then advances the
/// SAME tree per frame by writing the typed caches directly — no re-parse. The
/// returned tree at frame 0 already IS frame 0 (it was parsed from
/// `scene_ir(w, h, 0, detail)`); a caller that starts at another frame calls
/// [`animate`] before the first render.
///
/// Panics only if `scene_ir(w, h, 0, detail)` is not valid IR, which is a bug
/// in the generator, not a runtime condition.
pub fn scene_tree(w: u32, h: u32, detail: Detail) -> (Request, Handles) {
    let ir = scene_ir(w, h, 0, detail);
    let req = Request::parse(&ir).expect("scene_ir(frame 0) must be valid IR");
    let handles = Handles::for_detail(detail);
    // Cheap structural guard (compiled out in release): the positional paths
    // must land on the nodes they name, or a scene reorder would silently
    // animate the wrong widget. The byte-identity test is the real proof; this
    // fails LOUD and EARLY if the emit order and `Handles::for_detail` drift.
    debug_assert_eq!(name_at(&req, &[handles.header]), Some("vy_frame"));
    debug_assert_eq!(name_at(&req, &[handles.lcd]), Some("vy_lcd"));
    debug_assert_eq!(name_at(&req, &[handles.ticker]), Some("vy_label"));
    debug_assert_eq!(name_at(&req, &[handles.gauge]), Some("vy_gauge"));
    debug_assert_eq!(name_at(&req, &[handles.container]), Some("vy_container"));
    debug_assert_eq!(name_at(&req, &[handles.container, 0]), Some("vy_circle"));
    debug_assert_eq!(name_at(&req, &[handles.container, 1]), Some("vy_circle"));
    debug_assert_eq!(name_at(&req, &[handles.container, 2]), Some("vy_image"));
    debug_assert_eq!(name_at(&req, &[handles.trace]), Some("vy_chart"));
    if let Some(b) = handles.bars {
        debug_assert_eq!(name_at(&req, &[b]), Some("vy_chart"));
    }
    debug_assert_eq!(name_at(&req, &[handles.panel]), Some("vy_frame"));
    debug_assert_eq!(name_at(&req, &[handles.button]), Some("vy_button"));
    debug_assert_eq!(name_at(&req, &[handles.button, 0]), Some("vy_label"));
    debug_assert_eq!(name_at(&req, &[handles.slider]), Some("vy_slider"));
    debug_assert_eq!(name_at(&req, &[handles.progress]), Some("vy_progress"));
    debug_assert_eq!(name_at(&req, &[handles.toggle]), Some("vy_toggle"));
    debug_assert_eq!(name_at(&req, &[handles.roaming]), Some("vy_image"));
    (req, handles)
}

/// The `name` of the node at `path` (for the [`scene_tree`] structural guard).
#[cfg(debug_assertions)]
fn name_at<'a>(req: &'a Request, path: &[usize]) -> Option<&'a str> {
    let mut node = &req.root;
    for &i in path {
        node = node.children.get(i)?;
    }
    Some(node.name.as_str())
}

/// Advance the persistent tree to `frame`: set every animating field via the
/// vyr-core setters, from the SAME driver expressions [`scene_ir`] uses. After
/// this call the tree renders byte-for-byte like `Request::parse` of
/// `scene_ir(w, h, frame, handles.detail)` — the property `vyr-rig/tests`
/// asserts. Pure in `frame`: no residue carries between frames, so a persistent
/// tree replayed across ascending frames matches a fresh parse of each.
pub fn animate(req: &mut Request, handles: &Handles, w: u32, h: u32, frame: u32) {
    let detail = handles.detail;
    let th = theme(frame);
    let t = motion_frame(frame);
    let card = parse_color(CARDS[th]);
    let accent = parse_color(ACCENTS[th]);

    // Root backdrop: read LIVE from the root's `background` attr string at
    // paint (NOT from `Resolved.bg`), so the ROOT alone animates by rewriting
    // the attr; every other node reads its resolved cache.
    req.root.set_attr("background", BACKDROPS[th]);

    // header card fill.
    if let Some(n) = req.root.child_mut(&[handles.header]) {
        n.set_bg(card);
    }
    // the near-static counter + the rolling ticker.
    if let Some(n) = req.root.child_mut(&[handles.lcd]) {
        n.set_text(Some(counter_text(frame)));
    }
    if let Some(n) = req.root.child_mut(&[handles.ticker]) {
        n.set_text(Some(ticker_text(frame, detail)));
    }
    // the breathing gauge: one driver → y + width + height, plus accent arc.
    let gd = gauge_d(frame);
    if let Some(n) = req.root.child_mut(&[handles.gauge]) {
        n.set_geom(gx(20, w), gy(62 + (72 - gd) / 2, h), gyu(gd, h), gyu(gd, h));
        n.set_fg(accent);
    }
    // the clipped orbit container + its three orbiting children.
    if let Some(n) = req.root.child_mut(&[handles.container]) {
        n.set_bg(card);
    }
    let (cw, ch) = (200i64, 116i64);
    let (ax, ay) = orbit(cw, ch, 54, 30, t * 3, 44);
    let (bx, by) = orbit(cw, ch, 118, 66, 256 - (t * 5) % 256, 22);
    let (ix, iy) = orbit(cw, ch, 70, 40, t * 2 + 90, 40);
    if let Some(n) = req.root.child_mut(&[handles.container, 0]) {
        n.set_geom_pos(gx(ax, w), gy(ay, h));
    }
    if let Some(n) = req.root.child_mut(&[handles.container, 1]) {
        n.set_geom_pos(gx(bx, w), gy(by, h));
        n.set_bg(accent);
    }
    if let Some(n) = req.root.child_mut(&[handles.container, 2]) {
        n.set_geom_pos(gx(ix, w), gy(iy, h));
    }
    // the scrolling trace: series + accent line + card fill.
    if let Some(n) = req.root.child_mut(&[handles.trace]) {
        // The CSV is byte-identical to what `scene_ir` emits, so the shared
        // parse rebuilds the same points; the chart's only frame-varying line
        // colour is the accent.
        let _ = n.set_chart_points_csv(&trace_csv(frame, detail));
        n.set_chart_line_col(accent);
        n.set_bg(card);
    }
    // the bar chart (Full only): series + card fill (its line colour is static).
    if let Some(b) = handles.bars
        && let Some(n) = req.root.child_mut(&[b])
    {
        let _ = n.set_chart_points_csv(&bars_csv(frame));
        n.set_bg(card);
    }
    // the translucent panel: slot geometry + accent + the 3-way opacity.
    let slot = panel_slot(frame);
    let px = [16i64, 96, 176][slot as usize];
    if let Some(n) = req.root.child_mut(&[handles.panel]) {
        n.set_geom(gx(px, w), gy(126, h), gxu(280, w), gyu(88, h));
        n.set_bg(accent);
        n.set_opacity((88 + slot * 40) as f32);
    }
    // the button rides the panel; its label reads "RUN {slot}".
    if let Some(n) = req.root.child_mut(&[handles.button]) {
        n.set_geom(gx(px + 190, w), gy(140, h), gxu(76, w), gyu(26, h));
    }
    if let Some(n) = req.root.child_mut(&[handles.button, 0]) {
        n.set_text(Some(alloc::format!("RUN {slot}")));
    }
    // the cheap widgets on their own periods.
    if let Some(n) = req.root.child_mut(&[handles.slider]) {
        n.set_fraction(slider_value(frame) as f32, 0.0, 100.0);
    }
    if let Some(n) = req.root.child_mut(&[handles.progress]) {
        n.set_fraction(progress_value(frame) as f32, 0.0, 100.0);
    }
    if let Some(n) = req.root.child_mut(&[handles.toggle]) {
        n.set_value_on(toggle_on(frame));
    }
    // the natural-size roaming blit.
    let rx = 320 + wave(140, t * 4);
    let ry = 226 + wave(14, t * 7);
    if let Some(n) = req.root.child_mut(&[handles.roaming]) {
        n.set_geom_pos(gx(rx, w), gy(ry, h));
    }
}

// --- FNV-1a hashing + the run chain ------------------------------------------

/// FNV-1a 64 offset basis.
pub const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x100_0000_01b3;

/// FNV-1a 64 — the repo's golden-hash function (`tests/golden.rs`).
pub fn fnv1a(data: &[u8]) -> u64 {
    fnv1a_fold(FNV_OFFSET, data)
}

/// Fold more bytes into a running FNV-1a state — the streaming form the
/// banded M4 leg needs (one hash over a frame it never holds whole).
pub fn fnv1a_fold(mut h: u64, data: &[u8]) -> u64 {
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(FNV_PRIME);
    }
    h
}

/// Chain step: fold one frame hash (8 LE bytes) into the run hash, so the
/// run hash pins frame ORDER as well as content. A run of any length
/// therefore has ONE verifiable digest, and the per-frame hashes beside it
/// say which frame first diverged.
pub fn fnv1a_chain(run: u64, frame_hash: u64) -> u64 {
    fnv1a_fold(run, &frame_hash.to_le_bytes())
}

/// Format a hash the way the committed goldens spell it.
pub fn hex64(h: u64) -> String {
    let mut s = String::with_capacity(18);
    let _ = write!(s, "{h:#018x}");
    s
}

/// The suite address of one measurable run: `<scene>@<w>x<h>/<preset>`.
/// This is the string a harness stores; it is stable, sortable, and says
/// everything needed to reproduce the run.
pub fn suite_key(detail: Detail, w: u32, h: u32, preset: Preset) -> String {
    let mut s = String::with_capacity(32);
    let _ = write!(s, "{}@{}x{}/{}", detail.scene_id(), w, h, preset.name());
    s
}

// The unit tests exercise the PROPERTIES the scene claims (purity,
// periodicity, quiescence, repertoire coverage). Pixel-level truth is the
// committed hash chains' job — `vyr-rig/tests/long_scene.rs`.
#[cfg(test)]
extern crate std;

#[cfg(test)]
mod tests {
    use super::*;
    use alloc::vec::Vec;

    const W: u32 = 480;
    const H: u32 = 270;

    /// Invariant I2 as a property: the scene is a FUNCTION of the frame
    /// index. Re-asking for a frame — including after asking for later ones,
    /// which is what a replay does — must give the same bytes.
    #[test]
    fn scene_is_a_pure_function_of_the_frame_index() {
        for d in [Detail::Full, Detail::Lite] {
            let a = scene_ir(W, H, 137, d);
            let _later = scene_ir(W, H, 900, d);
            let b = scene_ir(W, H, 137, d);
            assert_eq!(a, b, "{} frame 137 is not reproducible", d.scene_id());
        }
    }

    /// Every driver is BOUNDED: over a `Full` run each one revisits its own
    /// value set many times, so a long run is an endurance test rather than
    /// an ever-widening one. (The drivers' cycles are deliberately coprime —
    /// 24, 25, 30, 50, 100, 279 — so the whole scene's least common period
    /// is far longer than any preset; that is a feature, not periodicity.
    /// What a long run must NOT do is invent new state forever, and the
    /// bounded value sets are what guarantees it.)
    #[test]
    fn every_driver_has_a_bounded_value_set() {
        let mut gauges: Vec<i64> = Vec::new();
        let mut slots: Vec<u32> = Vec::new();
        let mut tickers: Vec<String> = Vec::new();
        for f in 0..PERIOD {
            let g = gauge_d(f);
            if !gauges.contains(&g) {
                gauges.push(g);
            }
            let s = panel_slot(f);
            if !slots.contains(&s) {
                slots.push(s);
            }
            let t = ticker_text(f, Detail::Full);
            if !tickers.contains(&t) {
                tickers.push(t);
            }
        }
        assert_eq!(gauges.len(), 13, "gauge diameters: {gauges:?}");
        assert_eq!(slots.len(), 3);
        // 93 window offsets, and no more: the ticker cycles rather than
        // producing a fresh string forever (which would grow the glyph cache
        // without bound and make "does the heap settle?" unanswerable).
        assert_eq!(tickers.len(), 93);
    }

    /// The idle tail is what puts the low end of the dirty-fraction
    /// distribution under test, so the quiescence has to be real: through
    /// the tail, consecutive frames must differ ONLY in the counter text.
    #[test]
    fn idle_frames_change_only_the_counter() {
        let a = scene_ir(W, H, 45, Detail::Full);
        let b = scene_ir(W, H, 46, Detail::Full);
        assert_ne!(a, b, "the counter must still advance while idle");
        let strip = |s: &str, f: u32| s.replace(&counter_text(f), "");
        assert_eq!(
            strip(&a, 45),
            strip(&b, 46),
            "frames 45/46 are in the idle tail and must differ only in the counter"
        );
        assert!(is_idle(45) && is_idle(46));
        assert!(!is_idle(0) && !is_idle(ACTIVE));
    }

    /// A block boundary must change the ROOT background — that is the
    /// scene's full-frame-repaint event, and `vyr_core::dirty` keys on
    /// exactly that attr.
    #[test]
    fn block_boundaries_flip_the_backdrop() {
        assert_ne!(theme(BLOCK - 1), theme(BLOCK));
        assert!(scene_ir(W, H, BLOCK - 1, Detail::Full).contains(BACKDROPS[theme(BLOCK - 1)]));
        assert!(scene_ir(W, H, BLOCK, Detail::Full).contains(BACKDROPS[theme(BLOCK)]));
    }

    /// The glyph-churn claim is that the ticker walks its WHOLE repertoire
    /// within one period. If it did not, the cache would settle early and
    /// the scene would not be pressuring it at all.
    #[test]
    fn the_ticker_covers_its_whole_repertoire_within_a_period() {
        for d in [Detail::Full, Detail::Lite] {
            let mut seen: Vec<char> = Vec::new();
            for f in 0..PERIOD {
                for c in ticker_text(f, d).chars() {
                    if !seen.contains(&c) {
                        seen.push(c);
                    }
                }
            }
            assert_eq!(
                seen.len(),
                d.repertoire().chars().count(),
                "{} ticker missed part of its repertoire",
                d.scene_id()
            );
        }
    }

    /// The scene must never emit a JSON-hostile byte: the ticker walks raw
    /// ASCII into a string attribute, and one stray quote would be junk IR.
    #[test]
    fn no_scene_frame_contains_an_unescaped_quote_or_backslash() {
        for d in [Detail::Full, Detail::Lite] {
            assert!(!d.repertoire().contains('"'));
            assert!(!d.repertoire().contains('\\'));
            for f in [0u32, 7, 123, 999] {
                assert!(!scene_ir(W, H, f, d).contains('\\'));
            }
        }
    }

    /// Geometry scales with the rung: every ladder width must produce a
    /// scene, and the design space must not collapse font sizes to 0 (size
    /// 0 is junk IR).
    #[test]
    fn every_ladder_rung_renders_a_sane_scene() {
        for (w, h) in [(120u32, 68u32), (480, 270), (3840, 2160)] {
            let s = scene_ir(w, h, 300, Detail::Full);
            assert!(s.contains(&alloc::format!("\"w\": {w}, \"h\": {h}")));
            assert!(!s.contains("\"font_size\": \"0\""));
        }
    }

    /// The Q12 table is committed, so a bad regeneration is a silent
    /// geometry change. Pin its shape.
    #[test]
    fn sine_table_is_a_unit_circle() {
        assert_eq!(sin_q(0), 0);
        assert_eq!(sin_q(64), 4096);
        assert_eq!(sin_q(128), 0);
        assert_eq!(sin_q(192), -4096);
        assert_eq!(cos_q(0), 4096);
        for i in 0..256u32 {
            assert_eq!(sin_q(i), sin_q(i + 256));
            assert!(sin_q(i).abs() <= 4096);
        }
    }

    #[test]
    fn presets_and_scene_ids_round_trip() {
        for p in Preset::ALL {
            assert_eq!(Preset::parse(p.name()), Some(*p));
        }
        assert_eq!(Preset::Full.frames(), PERIOD);
        for d in [Detail::Full, Detail::Lite] {
            assert!(!d.scene_id().is_empty());
        }
        assert_eq!(
            suite_key(Detail::Full, 480, 270, Preset::Full),
            "long-v1@480x270/full"
        );
    }
}

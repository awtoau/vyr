//! `--features spicheck` — **how fast can this panel actually be written, and
//! how do we know?**
//!
//! # The question, and why "it looked fine" cannot answer it
//!
//! SPI5 runs at PCLK2/16 = 5.625 MHz because that is the universally-safe rate.
//! A full 240×320 RGB565 frame is 153,600 B, i.e. **218.5 ms** — the reason
//! [`crate::lcd`] is a static-frame path. The divisor is a power of two off a
//! 90 MHz APB2, so there are exactly four rungs: /16, /8, /4, /2 (5.625, 11.25,
//! 22.5, 45 MHz). There is NO authoritative datasheet for these clone panels to
//! call any rung "in spec" or "overclock" — the only qualifier is the read-back
//! this module performs, and it is PER-UNIT.
//!
//! Marginal SPI timing does not fail cleanly. It corrupts pixels. A panel
//! clocked past its setup/hold budget shows a picture — a slightly wrong one —
//! and a human glancing at a dashboard mock-up will call it fine. So this
//! module does not ask a human first. It asks the controller.
//!
//! # The instrument: read the GRAM back
//!
//! The ILI9341 holds the frame in its own memory and will read it back out over
//! the serial port. The existing bit-banged probe already proves the mechanism
//! works on this board — it gets `RDDMADCTL` and `RDDPM` answers on the shared
//! MOSI line. [`crate::lcd::gram_read`] extends that to `RAMRD` (`0x2E`) in
//! bulk.
//!
//! The reader is **deliberately independent of the thing being measured**: it
//! is GPIO bit-banging at a fixed ~0.5–1 MHz, governed by the panel's 6.66 MHz
//! READ ceiling, which SPI5's baud rate has nothing to do with. So the
//! procedure is:
//!
//! 1. Write a deterministic high-transition pattern into a
//!    240×[`CHECK_H`] GRAM window **at the rung under test** — window commands
//!    included, because a corrupt `CASET` is just as much a failure of the rate.
//! 2. Put SPI5 back to the reference /16 and read those pixels back, folding the
//!    raw returned bytes into FNV-1a 64.
//! 3. A rung whose read-back hash equals the **/16 rung's** is bit-exact on the
//!    wire. Not "looks right" — the same bits came back.
//!
//! Nothing is decoded on the way. The hash is over the raw stream, dummy byte
//! and 6-6-6 read format and all, because the verdict is a comparison between
//! rungs and a comparison only needs determinism. Guessing at the panel's read
//! format would be a way to be wrong about the one thing being proved.
//!
//! # Three controls, because an instrument that always agrees proves nothing
//!
//! * **Sensitivity** — the same window is written with a second, different
//!   pattern and re-read. If the two hashes are EQUAL the read is not tracking
//!   the write (a dead line reads a constant), and every "pass" below would be
//!   vacuous. Reported as `sensitive=`.
//! * **Repeatability** — /16 is read twice. If the reader is not deterministic,
//!   a differing hash at 45 MHz is not attributable to the writer.
//!   Reported as `repeatable=`.
//! * **Survival** — /16 is re-run LAST, after every faster rung. If the
//!   panel is still answering with the reference hash, no rung left it wedged
//!   and every verdict in between was taken on a healthy part.
//!
//! If any control fails, the rung results are reported as INCONCLUSIVE rather
//! than as passes.
//!
//! # And then the flush engine
//!
//! With a rate established, the second half of this leg prices the two flush
//! engines against each other in ONE flashed image: programmed I/O
//! ([`crate::lcd::flush_rect_pio`]), chunked DMA
//! ([`crate::spidma::flush_rect_dma`]), and DMA **overlapped** with the next
//! band's render ([`crate::spidma::flush_rect_dma_begin`]). The third is the
//! whole point of DMA here: the CPU currently spends ~512 of every 513 cycles
//! per pixel waiting on a flag, and a render that happens inside that wait is
//! a render that costs nothing.

use vyr_core::{Assets, Fonts, Quality, Rect, RenderError, ir::Request};

use crate::lcd::{
    PANEL_BAND_H, PANEL_H, PANEL_W, SPI_BR, burst_begin, burst_end, gram_read, set_window,
    spi_byte, spi_set_br,
};

#[inline]
fn cyc() -> u32 {
    crate::m4::clock_cycles() as u32
}

/// The rungs, slowest first. `BR` is SPI5 CR1's prescaler field: rate is
/// `PCLK2 >> (BR + 1)` with PCLK2 = 90 MHz.
const RUNGS: [u8; 4] = [3, 2, 1, 0];

/// Rows of the panel used as the check window: 240×160 = 38,400 px, i.e.
/// **76,800 B written** per rung and 115,200 B read back.
///
/// Half the screen rather than all of it, and the reason is the reader: the
/// bit-bang is ~1 MHz by construction, so a full-frame read-back would be
/// several seconds per rung. Half a screen is a large enough sample that a
/// timing fault — which is a bus-wide phenomenon, not a localised one — cannot
/// hide in the unread half, and the write covers 76,800 B of continuous burst,
/// which is the condition that actually stresses the link.
const CHECK_H: u32 = 160;
const CHECK_PX: u32 = PANEL_W * CHECK_H;
/// Bytes asked of `RAMRD`. Three per pixel is the ILI9341's serial read format
/// (6-6-6); if this panel returns two, the extra simply reads on into GRAM and
/// the stream stays deterministic, which is all the comparison needs.
const READ_BYTES: usize = (CHECK_PX as usize) * 3;

/// One deterministic pixel of the test pattern.
///
/// A hash-mixed index rather than a ramp or a checkerboard: what breaks a
/// marginal SPI link is transition density and inter-symbol interference, so
/// the pattern wants every bit of every byte to look independent of its
/// neighbours. Integer-only and stateless, so the M4 and any reader of this
/// file compute the same bytes.
fn pat(i: u32, seed: u32) -> u16 {
    let mut x = i
        .wrapping_mul(2_654_435_761)
        .wrapping_add(seed.wrapping_mul(0x9E37_79B9));
    x ^= x >> 15;
    x = x.wrapping_mul(0x85EB_CA6B);
    x ^= x >> 13;
    x as u16
}

/// How the pattern is clocked out.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Write {
    /// The busy-wait loop: a TXE poll between bytes leaves gaps in the clock.
    Pio,
    /// DMA2, gapless. Only meaningful — and only compiled — without `spipio`
    /// (DMA is the default flush engine).
    #[cfg(not(feature = "spipio"))]
    Dma,
}

/// Write the pattern into the check window **at rung `br`**, and return the
/// cycles the write took.
///
/// The `CASET`/`PASET`/`RAMWR` that open the window go out at `br` too. That is
/// deliberate: "the rate works" has to mean the command channel as well as the
/// pixel stream, and a mis-clocked window would corrupt the picture just as
/// thoroughly as mis-clocked pixels.
///
/// `how` decides whether the pixel stream is gapped (programmed I/O) or gapless
/// (DMA). It matters at the top of the ladder: PIO cannot saturate PCLK2/2 (82
/// measured cycles/pixel against 64 theoretical), so its inter-byte gaps hand
/// the panel recovery time the shipping DMA engine would not. A verdict taken
/// only with PIO could pass a rate that a real flush corrupts.
fn write_pattern(br: u8, seed: u32, how: Write) -> u32 {
    spi_set_br(br);
    set_window(0, 0, PANEL_W, CHECK_H);
    burst_begin();
    let t0 = cyc();
    match how {
        Write::Pio => {
            for i in 0..CHECK_PX {
                let c = pat(i, seed);
                spi_byte((c >> 8) as u8);
                spi_byte(c as u8);
            }
        }
        #[cfg(not(feature = "spipio"))]
        Write::Dma => {
            crate::spidma::dma_stream(CHECK_PX as usize * 2, |k| {
                let c = pat((k / 2) as u32, seed);
                if k % 2 == 0 { (c >> 8) as u8 } else { c as u8 }
            });
        }
    }
    let dt = (cyc()).wrapping_sub(t0);
    burst_end();
    // Back to the reference rung for everything that follows, so the read-back's
    // own window commands can never be the thing that failed.
    spi_set_br(3);
    dt
}

fn hex12(b: &[u8; 12]) -> alloc::string::String {
    let mut s = alloc::string::String::new();
    for x in b {
        let _ = core::fmt::Write::write_fmt(&mut s, format_args!("{x:02x} "));
    }
    s
}

/// Scrub, write at `br`, read back at the fixed bit-bang rate, report.
///
/// **The scrub is the load-bearing part.** Every rung writes into the same GRAM
/// window, so if a rung's write silently did nothing at all — the panel ignored
/// a mis-clocked `RAMWR`, say — the read-back would return the PREVIOUS rung's
/// pixels, and if those were the same pattern the rung would be scored a pass
/// for having written nothing. So the window is first overwritten with the
/// OTHER pattern at the reference /16 rung. The rung under test then has to
/// actually land its own bytes to produce its own reference hash. (The ladder
/// also alternates which pattern each rung writes, which closes the same hole
/// a second way and costs nothing.)
fn measure_rung(emit: &mut dyn FnMut(&str), br: u8, seed: u32, how: Write, label: &str) -> u64 {
    let hz = crate::lcd::spi_hz_for(br);
    // Scrub with the OTHER pattern, always via PIO at the reference rung so the
    // scrub itself can never be the marginal write.
    write_pattern(3, 3 - seed, Write::Pio);
    let wcyc = write_pattern(br, seed, how);
    let mut head = [0u8; 12];
    let (hash, rcyc) = gram_read(0, 0, PANEL_W, CHECK_H, READ_BYTES, &mut head);
    let sysclk = crate::m4::sysclk_hz() as u64;
    let engine = match how {
        Write::Pio => "pio",
        #[cfg(not(feature = "spipio"))]
        Write::Dma => "dma-gapless",
    };
    // Wire time the write SHOULD have taken, from the rate alone: 2 bytes per
    // pixel, 8 bits per byte. Printed beside the measured cycles so the reader
    // can see whether the CPU kept up at this rung as well as whether the bits
    // survived.
    let ideal = (CHECK_PX as u64) * 16 * sysclk / (hz as u64);
    emit(&alloc::format!(
        "INFO  [vyr-size] spirate rung: {label} br={br} spi_hz={hz} write={engine} \
         is_shipped_default={} px={CHECK_PX} wrote_B={} write_cyc={wcyc} ideal_wire_cyc={ideal} \
         cyc_per_px={} read_B={READ_BYTES} read_cyc={rcyc} read_hz={} \
         fnv1a={hash:#018x} head=[{}]",
        br == 3,
        CHECK_PX * 2,
        wcyc as u64 / CHECK_PX as u64,
        ((READ_BYTES as u64) * 8 * sysclk)
            .checked_div(rcyc as u64)
            .unwrap_or(0),
        hex12(&head).trim_end()
    ));
    hash
}

// --- the flush-engine comparison --------------------------------------------

const NBANDS: u32 = PANEL_H / PANEL_BAND_H;

/// Which flush engine one pass used.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Engine {
    /// No flush at all — the render-only baseline every other pass is read
    /// against.
    None,
    /// The existing busy-wait-on-TXE loop.
    Pio,
    /// DMA2 Stream 4 Channel 2, ping-pong staging, waited on per band.
    #[cfg(not(feature = "spipio"))]
    Dma,
    /// DMA started, the NEXT band rendered while it drains, then waited on.
    #[cfg(not(feature = "spipio"))]
    DmaOverlap,
}

impl Engine {
    fn name(self) -> &'static str {
        match self {
            Engine::None => "render-only",
            Engine::Pio => "pio-busywait",
            #[cfg(not(feature = "spipio"))]
            Engine::Dma => "dma-serial",
            #[cfg(not(feature = "spipio"))]
            Engine::DmaOverlap => "dma-overlap",
        }
    }
}

struct Pass {
    /// Every cycle from the first band's start to the last band's end.
    wall: u64,
    /// Cycles inside `render_with_quality` calls.
    render: u64,
    /// Cycles the CPU was blocked on the wire (busy-wait loop, or `dma_wait`).
    blocked: u64,
}

fn band_rect(n: u32) -> Rect {
    Rect {
        x: 0,
        y: (n * PANEL_BAND_H) as i32,
        w: PANEL_W,
        h: PANEL_BAND_H,
    }
}

/// Paint the whole 240×320 screen once with `engine`, timing it.
///
/// Every pass renders the SAME scene through the SAME
/// `render(tree, area, buf, stride)` entry point into the same CCM band buffer;
/// the engines differ only in what happens to those bytes afterwards. The pass
/// therefore doubles as a repaint, which is why this leg can scribble test
/// patterns over the glass and still leave a real picture behind.
fn pass(
    req: &Request,
    fonts: &mut Fonts,
    assets: &Assets,
    band_buf: &mut [u8],
    quality: Quality,
    engine: Engine,
) -> Result<Pass, RenderError> {
    let stride = (PANEL_W * 3) as usize;
    let len = stride * PANEL_BAND_H as usize;
    let mut render = 0u64;
    let mut blocked = 0u64;

    #[cfg(not(feature = "spipio"))]
    crate::spidma::reset_wait_cycles();

    let t0 = cyc();
    match engine {
        #[cfg(not(feature = "spipio"))]
        Engine::DmaOverlap => {
            // Band 0 must exist before the loop: the loop's shape is "flush the
            // band we have, render the next one while it flies".
            let ta = cyc();
            req.render_with_quality(
                fonts,
                assets,
                band_rect(0),
                &mut band_buf[..len],
                stride,
                quality,
            )?;
            render += (cyc()).wrapping_sub(ta) as u64;
            for n in 0..NBANDS {
                // `begin` converts the whole band into SRAM staging before it
                // returns, so `band_buf` is free the instant it does — that is
                // what makes the render below safe to run against the same
                // buffer while the wire is still busy.
                if !crate::spidma::flush_rect_dma_begin(
                    0,
                    n * PANEL_BAND_H,
                    PANEL_W,
                    PANEL_BAND_H,
                    &band_buf[..len],
                    stride,
                ) {
                    return Err(RenderError::BadIr(alloc::format!(
                        "band {n} does not fit the {} B DMA staging buffer",
                        crate::spidma::STAGE_BYTES
                    )));
                }
                if n + 1 < NBANDS {
                    let tb = cyc();
                    req.render_with_quality(
                        fonts,
                        assets,
                        band_rect(n + 1),
                        &mut band_buf[..len],
                        stride,
                        quality,
                    )?;
                    render += (cyc()).wrapping_sub(tb) as u64;
                }
                crate::spidma::flush_dma_end();
            }
        }
        _ => {
            for n in 0..NBANDS {
                let ta = cyc();
                req.render_with_quality(
                    fonts,
                    assets,
                    band_rect(n),
                    &mut band_buf[..len],
                    stride,
                    quality,
                )?;
                let tb = cyc();
                render += tb.wrapping_sub(ta) as u64;
                match engine {
                    Engine::None => {}
                    Engine::Pio => {
                        crate::lcd::flush_rect_pio(
                            0,
                            n * PANEL_BAND_H,
                            PANEL_W,
                            PANEL_BAND_H,
                            &band_buf[..len],
                            stride,
                        );
                        blocked += (cyc()).wrapping_sub(tb) as u64;
                    }
                    #[cfg(not(feature = "spipio"))]
                    Engine::Dma => {
                        crate::spidma::flush_rect_dma(
                            0,
                            n * PANEL_BAND_H,
                            PANEL_W,
                            PANEL_BAND_H,
                            &band_buf[..len],
                            stride,
                        );
                    }
                    #[cfg(not(feature = "spipio"))]
                    Engine::DmaOverlap => unreachable!(),
                }
            }
        }
    }
    let wall = (cyc()).wrapping_sub(t0) as u64;
    #[cfg(not(feature = "spipio"))]
    {
        let (w, te) = crate::spidma::wait_cycles();
        if matches!(engine, Engine::Dma | Engine::DmaOverlap) {
            blocked = w;
            if te != 0 {
                return Err(RenderError::BadIr(alloc::format!(
                    "DMA reported {te} transfer error(s)/timeouts"
                )));
            }
        }
    }
    Ok(Pass {
        wall,
        render,
        blocked,
    })
}

fn report_pass(emit: &mut dyn FnMut(&str), br: u8, engine: Engine, p: &Pass, base: &Pass) {
    let sysclk = crate::m4::sysclk_hz() as u64;
    let ms = |c: u64| c * 1000 / sysclk;
    let us = |c: u64| c * 1_000_000 / sysclk;
    // CPU cycles NOT spent waiting on the wire — the number DMA is supposed to
    // move. Reported alongside the wall clock because the two answer different
    // questions: "how long does a frame take" and "how much of the core does it
    // cost".
    let cpu = p.wall.saturating_sub(p.blocked);
    emit(&alloc::format!(
        "INFO  [vyr-size] spirate flush: engine={} br={br} spi_hz={} bands={NBANDS} \
         wall_cyc={} wall_ms={} render_cyc={} render_ms={} blocked_cyc={} \
         blocked_ms={} cpu_cyc={} cpu_ms={} cpu_pct={} fps_x100={} \
         hidden_render_cyc={} us_per_band={}",
        engine.name(),
        crate::lcd::spi_hz_for(br),
        p.wall,
        ms(p.wall),
        p.render,
        ms(p.render),
        p.blocked,
        ms(p.blocked),
        cpu,
        ms(cpu),
        (cpu * 100).checked_div(p.wall).unwrap_or(0),
        (sysclk * 100).checked_div(p.wall).unwrap_or(0),
        // How much of this pass's render actually disappeared inside the
        // flush: the serial cost is render + flush, so anything the wall clock
        // is short of that is overlap that happened.
        (base.wall + p.wall.saturating_sub(p.render)).saturating_sub(p.wall),
        us(p.wall / NBANDS as u64),
    ));
}

// --- the run -----------------------------------------------------------------

/// The whole leg: machine rate verdict, then engine comparison, then a clean
/// repaint at the build's own rate.
pub fn run(
    emit: &mut dyn FnMut(&str),
    band_buf: &mut [u8],
    quality: Quality,
) -> Result<(), RenderError> {
    emit(&alloc::format!(
        "INFO  [vyr-size] spirate: SPI5 rate ladder + flush-engine comparison. \
         pclk2_hz={} build_br={SPI_BR} build_spi_hz={} \
         spec_write_max_hz=10000000 spec_read_max_hz=6660000 \
         check_window={PANEL_W}x{CHECK_H} check_px={CHECK_PX} \
         reader=bit-bang GPIO (rate-independent), dma={}",
        crate::lcd::PCLK2_HZ,
        crate::lcd::spi_hz_for(SPI_BR),
        !cfg!(feature = "spipio"),
    ));

    // ---- controls first, at the reference rung -------------------------------
    let ref_a = measure_rung(emit, 3, 1, Write::Pio, "reference-A");
    let ref_a2 = measure_rung(emit, 3, 1, Write::Pio, "reference-A-repeat");
    let ref_b = measure_rung(emit, 3, 2, Write::Pio, "sensitivity-B");
    let repeatable = ref_a == ref_a2;
    let sensitive = ref_a != ref_b;
    emit(&alloc::format!(
        "INFO  [vyr-size] spirate controls: repeatable={repeatable} \
         (A={ref_a:#018x} A'={ref_a2:#018x}) sensitive={sensitive} \
         (B={ref_b:#018x}) — repeatable says the reader is deterministic, \
         sensitive says it tracks what was written; without BOTH, every rung \
         verdict below is INCONCLUSIVE rather than a pass"
    ));

    // ---- the ladder ---------------------------------------------------------
    //
    // Each rung is tested with PIO and, where DMA is compiled, again with the
    // gapless DMA stream — the write engine that actually ships. A rung is
    // "best" only if it is bit-exact by BOTH: the gapless test is strictly
    // harder (no inter-byte recovery time), and shipping a rate that only PIO's
    // gaps make survivable would be exactly the wrong lesson to take from this.
    let mut verdict = [false; RUNGS.len()];
    // `mut` only when DMA is compiled (the default), where the DMA re-test
    // writes it; under `spipio` the array stays all-true (there is no gapless
    // engine to fail) and the `pass` below reduces to the PIO verdict alone.
    #[cfg(not(feature = "spipio"))]
    let mut verdict_dma = [true; RUNGS.len()];
    #[cfg(feature = "spipio")]
    let verdict_dma = [true; RUNGS.len()];
    let mut best: u8 = 3;
    for (k, &br) in RUNGS.iter().enumerate() {
        // Alternate the pattern rung by rung, each compared against its OWN
        // /16 reference: a rung that wrote nothing would return its
        // predecessor's bytes, which are now guaranteed to be a different
        // pattern, and would fail rather than inherit a pass.
        let seed = if k % 2 == 0 { 1 } else { 2 };
        let want = if seed == 1 { ref_a } else { ref_b };
        let h = measure_rung(emit, br, seed, Write::Pio, "ladder");
        verdict[k] = h == want;
        #[cfg(not(feature = "spipio"))]
        {
            let hd = measure_rung(emit, br, seed, Write::Dma, "ladder-dma");
            verdict_dma[k] = hd == want;
        }
        let pass = verdict[k] && verdict_dma[k];
        emit(&alloc::format!(
            "INFO  [vyr-size] spirate verdict: br={br} spi_hz={} is_shipped_default={} \
             bit_exact={} bit_exact_pio={} bit_exact_dma={} readback={h:#018x} \
             reference={want:#018x} pattern={seed} full_frame_ms={} conclusive={}",
            crate::lcd::spi_hz_for(br),
            br == 3,
            pass,
            verdict[k],
            verdict_dma[k],
            // 240*320*2 bytes * 8 bits at this rate, in ms.
            (PANEL_W as u64) * (PANEL_H as u64) * 16 * 1000 / crate::lcd::spi_hz_for(br) as u64,
            repeatable && sensitive,
        ));
        if pass && crate::lcd::spi_hz_for(br) > crate::lcd::spi_hz_for(best) {
            best = br;
        }
    }

    // ---- survival: the reference rung again, LAST -----------------------------
    let survive = measure_rung(emit, 3, 1, Write::Pio, "survival-A");
    emit(&alloc::format!(
        "INFO  [vyr-size] spirate survival: after every faster rung the \
         reference /16 read-back is {survive:#018x} vs reference {ref_a:#018x} \
         match={} — a mismatch would mean some rung left the controller in a \
         state that invalidates the verdicts above",
        survive == ref_a
    ));
    emit(&alloc::format!(
        "INFO  [vyr-size] spirate summary: highest bit_exact br={best} \
         spi_hz={} is_shipped_default={} controls_ok={} \
         (pio br=3/2/1/0 -> {}/{}/{}/{}) (dma br=3/2/1/0 -> {}/{}/{}/{})",
        crate::lcd::spi_hz_for(best),
        best == 3,
        repeatable && sensitive && survive == ref_a,
        verdict[0],
        verdict[1],
        verdict[2],
        verdict[3],
        verdict_dma[0],
        verdict_dma[1],
        verdict_dma[2],
        verdict_dma[3],
    ));

    // ---- the flush engines --------------------------------------------------
    //
    // Registries built here rather than inherited: the caller's are whatever
    // the previous leg left behind, and the card's 68-node tree is the largest
    // thing this vehicle parses (see `testcard::reclaim`).
    let mut fonts = Fonts::new();
    fonts.register(
        "roboto",
        crate::opaque(crate::workload::SUBSET_FONT).to_vec(),
    )?;
    let assets = Assets::new();
    #[cfg(feature = "testcard")]
    let scene = crate::opaque(crate::testcard::CARD_IR);
    #[cfg(not(feature = "testcard"))]
    let scene = crate::opaque(crate::lcd::PANEL_IR);
    let req = Request::parse(scene)?;

    // Both ends of the ladder, so the engine comparison is not a single-rate
    // anecdote: at /16 the wire dominates everything and DMA should hide the
    // whole render; at the fastest bit-exact rung the render may be the
    // limit instead, which is the crossover worth knowing about.
    let mut rates = alloc::vec![3u8];
    if best != 3 {
        rates.push(best);
    }
    for br in rates {
        spi_set_br(br);
        let base = pass(&req, &mut fonts, &assets, band_buf, quality, Engine::None)?;
        report_pass(emit, br, Engine::None, &base, &base);
        let pio = pass(&req, &mut fonts, &assets, band_buf, quality, Engine::Pio)?;
        report_pass(emit, br, Engine::Pio, &pio, &base);
        #[cfg(not(feature = "spipio"))]
        {
            let dma = pass(&req, &mut fonts, &assets, band_buf, quality, Engine::Dma)?;
            report_pass(emit, br, Engine::Dma, &dma, &base);
            let ov = pass(
                &req,
                &mut fonts,
                &assets,
                band_buf,
                quality,
                Engine::DmaOverlap,
            )?;
            report_pass(emit, br, Engine::DmaOverlap, &ov, &base);
            emit(&alloc::format!(
                "INFO  [vyr-size] spirate engines: br={br} spi_hz={} \
                 pio_wall={} dma_wall={} overlap_wall={} render_only={} \
                 pio_cpu={} dma_cpu={} overlap_cpu={} \
                 cpu_saved_vs_pio={} overlap_saved_vs_dma={}",
                crate::lcd::spi_hz_for(br),
                pio.wall,
                dma.wall,
                ov.wall,
                base.wall,
                pio.wall.saturating_sub(pio.blocked),
                dma.wall.saturating_sub(dma.blocked),
                ov.wall.saturating_sub(ov.blocked),
                pio.wall
                    .saturating_sub(pio.blocked)
                    .saturating_sub(dma.wall.saturating_sub(dma.blocked)),
                dma.wall.saturating_sub(ov.wall),
            ));
        }
    }

    // ---- leave the glass in a state a human can read ------------------------
    //
    // The rate ladder above overwrote half the screen with pseudo-random bytes.
    // A leg that ends with noise on the panel has destroyed the other half of
    // the evidence, so the last thing it does is repaint at the build's own
    // configured rate — the rate the identity strip claims.
    spi_set_br(SPI_BR);
    let _ = pass(&req, &mut fonts, &assets, band_buf, quality, Engine::Pio)?;
    // MEASURED, not tidiness: the card's 68-node tree plus the identity strip's
    // tree plus the 32,768 B Exact gutter pixmap does not fit the 120 KiB
    // arena, and the first run of this leg died on exactly that
    // (`memory allocation of 32768 bytes failed`) AFTER reporting every number,
    // taking the 480x270 measurement down with it. The card is finished with
    // here; drop it before the strip is parsed.
    drop(req);
    #[cfg(feature = "testcard")]
    {
        let stride = (PANEL_W * 3) as usize;
        let ident = Request::parse(&crate::testcard::identity_ir(
            "PATH lcd: SPI5 -> ILI9341 GRAM",
            &alloc::format!(
                "SPI {} kHz /{} best-bitexact /{}",
                crate::lcd::spi_hz_for(SPI_BR) / 1000,
                1u32 << (SPI_BR as u32 + 1),
                1u32 << (best as u32 + 1)
            ),
            &alloc::format!(
                "flush {} / MADCTL {:#04x}",
                if !cfg!(feature = "spipio") {
                    "DMA2 S4C2"
                } else {
                    "PIO"
                },
                crate::lcd::MADCTL
            ),
        ))?;
        crate::testcard::banded(
            &ident,
            &mut fonts,
            &assets,
            band_buf,
            quality,
            crate::testcard::IDENT_RECT,
            0,
            |y, h, buf| crate::lcd::flush_rect_pio(0, y, PANEL_W, h, buf, stride),
        )?;
    }
    emit(
        "ALERT [vyr-size] spirate: done — the panel has been repainted at the \
         build's configured rate and the identity strip on it names that rate, \
         so a photograph is self-describing",
    );
    Ok(())
}

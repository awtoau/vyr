//! The DEFAULT flush engine (whenever `lcd` is compiled without
//! `--features spipio`) — stream the ILI9341 pixel flush out of SPI5 with
//! **DMA2 Stream 4, Channel 2**, instead of busy-waiting on TXE.
//!
//! # Why, independently of the clock rate
//!
//! The programmed-I/O flush ([`crate::lcd::flush_rect_pio`]) was measured at
//! 513–515 core cycles per dirty pixel against a theoretical 512 at PCLK2/16 —
//! ~99.6 % wire efficiency. That is a good number and a damning one: it says
//! the CPU is keeping the shift register perfectly fed, that tighter code can
//! win nothing, and that ~512 of every 513 cycles are spent in a spin loop
//! waiting for a flag. At Draft the 240×320 render is ~70 ms against a 218 ms
//! flush, so an engine that hands the wire to hardware does not merely save
//! cycles — it lets the next render happen *inside* the current flush.
//!
//! # The mapping is ST's, not a guess
//!
//! SPI5_TX appears twice in the F429's DMA request matrix. Taken from ST's own
//! CubeMX device database (`STM32F429ZI.json` in `embassy-rs/stm32-data-
//! generated`, which is generated from it), for the peripheral at
//! `0x4001_5000` — SPI5:
//!
//! | signal | stream | channel |
//! |---|---|---|
//! | RX | DMA2 Stream 3 | 2 |
//! | **TX** | **DMA2 Stream 4** | **2** |
//! | RX | DMA2 Stream 5 | 7 |
//! | **TX** | DMA2 Stream 6 | 7 |
//!
//! Stream 4 / channel 2 is used here. Nothing else in this vehicle touches
//! DMA2 — the LTDC leg drives its layers from SDRAM through the LTDC's own
//! master, and the FMC has no stream — so there is no arbitration to reason
//! about.
//!
//! # The staging buffer exists because CCM has no DMA path
//!
//! This is the load-bearing constraint, and it is a property of the silicon,
//! not a design preference: **the F429's 64 KiB core-coupled memory at
//! `0x1000_0000` is reachable by the CPU's D-bus only.** DMA cannot see it.
//! vyr's band buffer lives there (see `m4::BAND_BUF`, and `link-board.ld`,
//! which both say so), and it holds RGB888 anyway while the wire wants RGB565 —
//! so a conversion pass into ordinary SRAM was always going to happen. It is
//! the same arithmetic the PIO path already does per pixel; the only new cost
//! is the store, and the new memory is [`STAGE_BYTES`] of `.bss`.
//!
//! # Two engines, one image, and the determinism proof untouched
//!
//! `flush_rect` dispatches here when the feature is on, and
//! [`crate::lcd::flush_rect_pio`] stays compiled either way, so ONE flashed
//! image can price the two against each other with nothing else different.
//! Neither engine touches the renderer: both consume exactly the bytes
//! `render(tree, area, buf, stride)` produced, and the per-band FNV-1a fold in
//! [`crate::lcd::show_panel_scene`] and [`crate::anim`] happens on those bytes
//! BEFORE either engine sees them. A DMA that corrupted the wire could not move
//! a hash — which is precisely why the wire needs its own instrument
//! and does not get to hide behind this one.

// Which half of this module is live depends on which OTHER feature is on.
// A plain display build (no `spipio`) uses the DMA `flush_rect` and nothing
// else; the split begin/end API and the wait-cycle instrumentation exist for
// The two engines are measured against each
// other. Warning about that per feature set would be noise about a module that
// is entirely reachable across the set of builds it exists for.
#![allow(dead_code)]

use core::sync::atomic::{AtomicBool, Ordering};

use crate::lcd::{SPI_CR2, SPI_DR, SPI5, burst_begin, burst_end, rd, rgb565, set_window, wr};

// --- DMA2, stream 4 (RM0090 §10.5) ------------------------------------------

const RCC_AHB1ENR: u32 = 0x4002_3830;
/// AHB1ENR bit 22 — DMA2EN.
const DMA2EN: u32 = 1 << 22;

const DMA2: u32 = 0x4002_6400;
/// High interrupt status / clear registers cover streams 4..7.
const DMA_HISR: u32 = DMA2 + 0x04;
const DMA_HIFCR: u32 = DMA2 + 0x0C;
/// Stream registers start at +0x10 and are 0x18 apart; stream 4 is the fifth.
const S4: u32 = DMA2 + 0x10 + 0x18 * 4;
const S_CR: u32 = 0x00;
const S_NDTR: u32 = 0x04;
const S_PAR: u32 = 0x08;
const S_M0AR: u32 = 0x0C;

/// Stream 4's flags occupy HISR/HIFCR bits 0..5: FEIF4 0, DMEIF4 2, TEIF4 3,
/// HTIF4 4, TCIF4 5.
const TCIF4: u32 = 1 << 5;
const TEIF4: u32 = 1 << 3;
const ALLIF4: u32 = 0b11_1101;

/// Stream 4 configuration, minus the enable bit: channel 2 (SPI5_TX), priority
/// high, byte source and byte destination, memory incrementing, peripheral
/// fixed, memory→peripheral, no circular mode, no interrupts.
///
/// Byte-sized both ends on purpose. SPI5 is configured 8-bit (`DFF = 0`) by
/// [`crate::lcd::spi_init`], and a 16-bit DMA write into an 8-bit data register
/// would be a silent halving of the pixel stream — the kind of fault that
/// produces a plausible-looking picture. The RGB565 byte order (high byte
/// first) is therefore decided in the conversion loop below, exactly as the PIO
/// path decides it.
const CR_BASE: u32 = (2 << 25)      // CHSEL = 2 -> SPI5_TX
    | (0b10 << 16)                  // PL = high
    // MSIZE (bits 14:13) and PSIZE (bits 12:11) are both byte = 0b00, left
    // implicit — they are the reset value and spelling them as `0 << 13` is a
    // no-op the compiler and clippy both object to. Byte both ends is the point
    // (see the doc above); it is enforced by NOT setting these fields.
    | (1 << 10)                     // MINC
    | (0b01 << 6); // DIR = memory -> peripheral

/// Bounded, like every other hardware wait in this vehicle: an unbounded spin
/// on a flag that never asserts is a silent hang with no output. The longest
/// legitimate wait here is one full staging buffer at the slowest rate —
/// 8,192 B at 5.625 MHz = 11.6 ms ≈ 2.1 M core cycles — and a loop pass is a
/// few cycles, so 8 M passes clears it with margin while still bailing out in
/// well under a second if the stream never completes.
const SPIN_LIMIT: u32 = 8_000_000;

// --- the staging buffer -----------------------------------------------------

/// Bytes of ordinary SRAM handed to the DMA engine.
///
/// 8,192 B = exactly one 240×16 band of RGB565 (7,680 B) with room to spare, so
/// [`flush_rect_dma_begin`]'s whole-band single-shot transfer never has to
/// chunk, AND two 4,096 B halves for [`flush_rect_dma`]'s ping-pong. Costed in
/// `.bss`, i.e. against the 192 KiB SRAM the 122,880 B heap arena also comes
/// out of — not against CCM, which DMA cannot reach at all.
pub(crate) const STAGE_BYTES: usize = 8192;
/// Half of [`STAGE_BYTES`]: the ping-pong chunk.
const CHUNK: usize = STAGE_BYTES / 2;

#[repr(C, align(4))]
struct Stage(core::cell::UnsafeCell<[u8; STAGE_BYTES]>);

// SAFETY: single-threaded target with no heap-touching interrupts; the only
// concurrent reader is the DMA engine, and every hand-off below is bracketed by
// `dma_wait`, so the CPU never writes a region a transfer is still reading.
unsafe impl Sync for Stage {}

static STAGE: Stage = Stage(core::cell::UnsafeCell::new([0; STAGE_BYTES]));

/// True while a transfer this module started has not been waited on.
static PENDING: AtomicBool = AtomicBool::new(false);
static INITED: AtomicBool = AtomicBool::new(false);

/// Cycles the CPU spent inside [`dma_wait`] since [`reset_wait_cycles`] —
/// the number that says how much of the flush the core actually paid for.
static mut WAIT_CYCLES: u64 = 0;
static mut TE_ERRORS: u32 = 0;

/// Zero the wait-cycle accumulator. Only the measurement leg calls it.
pub(crate) fn reset_wait_cycles() {
    // SAFETY: single-threaded; no interrupt touches this.
    unsafe { WAIT_CYCLES = 0 };
}

/// `(cycles the CPU spent blocked on DMA, transfer errors seen)`.
pub(crate) fn wait_cycles() -> (u64, u32) {
    // SAFETY: as above.
    unsafe { (WAIT_CYCLES, TE_ERRORS) }
}

fn stage_ptr() -> *mut u8 {
    STAGE.0.get() as *mut u8
}

/// Clock DMA2 and park stream 4 in a known state. Idempotent; the flush path
/// calls it every time and it costs one atomic load after the first.
fn dma_init() {
    if INITED.swap(true, Ordering::Relaxed) {
        return;
    }
    wr(RCC_AHB1ENR, rd(RCC_AHB1ENR) | DMA2EN);
    // Disable the stream and wait for the engine to acknowledge: RM0090 is
    // explicit that a stream's configuration registers are only writable once
    // EN reads back 0.
    wr(S4 + S_CR, 0);
    let mut n = 0u32;
    while rd(S4 + S_CR) & 1 != 0 && n < SPIN_LIMIT {
        n += 1;
        core::hint::spin_loop();
    }
    wr(DMA_HIFCR, ALLIF4);
    wr(S4 + S_PAR, SPI5 + SPI_DR);
}

/// Arm stream 4 on `len` bytes at `src` and let it go.
fn dma_start(src: *const u8, len: usize) {
    debug_assert!(len > 0 && len <= 65_535);
    wr(DMA_HIFCR, ALLIF4);
    wr(S4 + S_M0AR, src as u32);
    wr(S4 + S_NDTR, len as u32);
    wr(S4 + S_CR, CR_BASE | 1);
    PENDING.store(true, Ordering::Relaxed);
}

/// Block until the in-flight transfer has been fully handed to SPI5, charging
/// the cycles spent doing it to [`WAIT_CYCLES`].
///
/// "Fully handed to SPI5" is TCIF, not an empty shift register: the last two
/// bytes are still in the SPI FIFO/shift register afterwards, and
/// [`crate::lcd::burst_end`]'s `spi_drain` is what makes /CS safe to raise.
pub(crate) fn dma_wait() {
    if !PENDING.swap(false, Ordering::Relaxed) {
        return;
    }
    let t0 = crate::m4::clock_cycles() as u32;
    let mut n = 0u32;
    loop {
        let hisr = rd(DMA_HISR);
        if hisr & TCIF4 != 0 {
            break;
        }
        if hisr & TEIF4 != 0 {
            // SAFETY: single-threaded.
            unsafe { TE_ERRORS += 1 };
            break;
        }
        n += 1;
        if n >= SPIN_LIMIT {
            // SAFETY: single-threaded.
            unsafe { TE_ERRORS += 1 };
            break;
        }
        core::hint::spin_loop();
    }
    wr(S4 + S_CR, rd(S4 + S_CR) & !1);
    let mut m = 0u32;
    while rd(S4 + S_CR) & 1 != 0 && m < SPIN_LIMIT {
        m += 1;
        core::hint::spin_loop();
    }
    wr(DMA_HIFCR, ALLIF4);
    let d = (crate::m4::clock_cycles() as u32).wrapping_sub(t0) as u64;
    // SAFETY: single-threaded.
    unsafe { WAIT_CYCLES += d };
}

fn spi_txdma(on: bool) {
    let v = rd(SPI5 + SPI_CR2);
    wr(SPI5 + SPI_CR2, if on { v | 2 } else { v & !2 });
}

/// Stream `n` generated bytes out of SPI5 with DMA, into a GRAM window and a
/// `/CS` burst the CALLER has already opened.
///
/// Exists for the rate ladder, and for a reason that is not convenience.
/// Programmed I/O cannot saturate the wire at the top of the ladder — measured
/// at PCLK2/2, the busy-wait loop needs 82 core cycles per pixel against a
/// theoretical 64, so ~22 % of the wire time is the clock STOPPED between
/// bytes. Those gaps give the panel recovery time that a real DMA flush would
/// not, so a "45 MHz is fine" verdict taken with programmed I/O would not
/// transfer to the engine this module actually ships. DMA emits the stream
/// gapless, which is the harder test and the one that matches production.
pub(crate) fn dma_stream<F: FnMut(usize) -> u8>(n: usize, mut f: F) {
    dma_init();
    spi_txdma(true);
    let base = stage_ptr();
    let mut half = 0usize;
    let mut i = 0usize;
    while i < n {
        let take = CHUNK.min(n - i);
        for k in 0..take {
            // SAFETY: `half * CHUNK + k < STAGE_BYTES` since `take <= CHUNK`;
            // the half being written is not the half a transfer is reading,
            // which the `dma_wait` below each hand-off establishes.
            unsafe { base.add(half * CHUNK + k).write_volatile(f(i + k)) };
        }
        dma_wait();
        // SAFETY: in-bounds by construction, and the CPU will not touch this
        // half again until the next `dma_wait` returns.
        dma_start(unsafe { base.add(half * CHUNK) }, take);
        half ^= 1;
        i += take;
    }
    dma_wait();
    spi_txdma(false);
}

/// Convert `[x, x+w) x [y, y+h)` of `buf` (RGB888, `stride` bytes per row) into
/// the staging buffer and push it out with DMA.
///
/// Ping-pong: fill half A, kick the DMA on it, fill half B while A is on the
/// wire, wait for A, kick B. The conversion is ~6 cycles/pixel against
/// 512 cycles/pixel of wire time at PCLK2/16 (and 64 at PCLK2/2), so the
/// conversion disappears under the transfer at every rung of the ladder and
/// this function's CPU cost collapses to whatever [`dma_wait`] reports.
pub(crate) fn flush_rect_dma(x: u32, y: u32, w: u32, h: u32, buf: &[u8], stride: usize) {
    dma_init();
    set_window(x, y, w, h);
    burst_begin();
    spi_txdma(true);

    let base = stage_ptr();
    let row_bytes = (w * 3) as usize;
    let mut half = 0usize;
    let mut n = 0usize;
    for row in 0..h as usize {
        let off = row * stride;
        for px in buf[off..off + row_bytes].chunks_exact(3) {
            let c = rgb565(px[0], px[1], px[2]);
            // SAFETY: `half * CHUNK + n` is < STAGE_BYTES by the invariant
            // `n < CHUNK` restored at every wrap below, and the half being
            // written is never the half a transfer is reading (the `dma_wait`
            // in the wrap is what establishes that).
            unsafe {
                let p = base.add(half * CHUNK + n);
                p.write_volatile((c >> 8) as u8);
                p.add(1).write_volatile(c as u8);
            }
            n += 2;
            if n == CHUNK {
                dma_wait();
                // SAFETY: in-bounds by construction; the engine reads CHUNK
                // bytes from a region the CPU will not touch until the next
                // `dma_wait` returns.
                dma_start(unsafe { base.add(half * CHUNK) }, CHUNK);
                half ^= 1;
                n = 0;
            }
        }
    }
    if n > 0 {
        dma_wait();
        // SAFETY: as above.
        dma_start(unsafe { base.add(half * CHUNK) }, n);
    }
    dma_wait();
    spi_txdma(false);
    burst_end();
}

/// **Start** a flush and return with the wire still busy — the overlap path.
///
/// Converts the WHOLE rect into the staging buffer up front (so the CPU is
/// finished with both the source band buffer and the staging buffer), arms one
/// DMA transfer, and returns. The caller may then render the NEXT band into the
/// CCM band buffer while this one is on the wire, and must call
/// [`flush_dma_end`] before starting another.
///
/// Returns `false` and does nothing if the rect does not fit [`STAGE_BYTES`] —
/// honest failure rather than a silently truncated band. One 240×16 RGB565 band
/// is 7,680 B, which is what the buffer is sized for.
pub(crate) fn flush_rect_dma_begin(
    x: u32,
    y: u32,
    w: u32,
    h: u32,
    buf: &[u8],
    stride: usize,
) -> bool {
    let need = (w as usize) * (h as usize) * 2;
    if need == 0 || need > STAGE_BYTES {
        return false;
    }
    dma_init();
    let base = stage_ptr();
    let row_bytes = (w * 3) as usize;
    let mut n = 0usize;
    for row in 0..h as usize {
        let off = row * stride;
        for px in buf[off..off + row_bytes].chunks_exact(3) {
            let c = rgb565(px[0], px[1], px[2]);
            // SAFETY: `n < need <= STAGE_BYTES` by the guard above.
            unsafe {
                let p = base.add(n);
                p.write_volatile((c >> 8) as u8);
                p.add(1).write_volatile(c as u8);
            }
            n += 2;
        }
    }
    set_window(x, y, w, h);
    burst_begin();
    spi_txdma(true);
    dma_start(base, need);
    true
}

/// Finish what [`flush_rect_dma_begin`] started: wait out the transfer, drain
/// the shift register and release /CS.
pub(crate) fn flush_dma_end() {
    dma_wait();
    spi_txdma(false);
    burst_end();
}

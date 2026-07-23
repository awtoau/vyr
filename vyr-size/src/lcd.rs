//! `--features lcd` / `--features ltdc` — put vyr's pixels on the
//! STM32F429I-DISC1's own panel (#28, #30). Measurement scaffolding in the
//! vyr-size sense: `unsafe` register pokes live here, vyr-core stays
//! `no_std + alloc` and `forbid(unsafe_code)` and is used through exactly the
//! same `render(tree, area, buf, stride)` entry point as every other leg.
//!
//! This module owns the **command channel and the panel**: SPI5, the three
//! control pins, ST's ILI9341 init sequence, the read-back probe, the
//! 240×320 scene, and the RGB565 conversion. Both display paths use it —
//! `lcd` streams pixels through SPI5 into the controller's GRAM, `ltdc`
//! (see the `ltdc` module) keeps SPI5 for registers only and lets the LTDC
//! peripheral scan a framebuffer out of external SDRAM over the parallel RGB
//! bus. The single register that decides which is [`panel_init`]'s `rgb`
//! argument.
//!
//! # What the hardware actually is (verified, not assumed)
//!
//! Confirmed from ST's own board support package for THIS board — the
//! machine-readable statement of the schematic — fetched by
//! `scripts/lcd-docs.py` into `tmp/lcd-docs/`:
//!
//! * **Controller**: ILI9341, 240×320, `ILI9341_ID = 0x9341`
//!   (`STMicroelectronics/stm32-ili9341` → `ili9341.h`, the submodule
//!   STM32CubeF4 mounts at `Drivers/BSP/Components/ili9341`).
//! * **Command interface**: SPI5, GPIOF AF5 — **SCK PF7, MISO PF8, MOSI
//!   PF9**; **/CS PC2**, **D/CX (WRX) PD13**, **RDX PD12**
//!   (`STMicroelectronics/32f429idiscovery-bsp` → `stm32f429i_discovery.h`,
//!   `DISCOVERY_SPIx*` and `LCD_NCS/WRX/RDX_PIN`). The L3GD20 gyro shares the
//!   bus on **/CS PC1**, so this driver drives PC1 high and keeps it there.
//! * **Bus rate**: PCLK2/16 = 5.625 MHz — ST's own choice, with ST's own
//!   reason: "ILI9341 LCD SPI interface max baudrate is 10 MHz for write and
//!   6.66 MHz for read" (`ili9341_spi.c`, Trezor's copy of the ST driver).
//!   Mode 0 (CPOL=0/CPHA=0), 8-bit, MSB first, software NSS.
//! * **No backlight or DISP GPIO exists.** ST's `BSP_LCD_MspInit` configures
//!   LTDC alternate functions and the three control pins above and nothing
//!   else, so there is no enable line to forget.
//!
//! # SPI-to-GRAM vs LTDC — one register
//!
//! The ILI9341 has its own 240×320×18bpp frame memory and refreshes the glass
//! from it with no host involvement. Whether that memory is fed from the host
//! interface or from the RGB bus is ONE register: `0xF6` (Interface Control)
//! bit RM and field DM. ST's init sets the third parameter to `0x06` — RM=1
//! (GRAM written from the RGB interface), DM=01 (RGB interface display
//! operation), i.e. "LTDC owns the pixels". The `lcd` path sends `0x00`
//! instead — RM=0 (GRAM written from the system/serial interface), DM=00
//! (internal-clock display operation), i.e. "the SPI port owns the pixels".
//! Everything else in the sequence — the power/VCOM/gamma block and MADCTL
//! `0xC8` — is ST's, byte for byte, so the panel is driven at the operating
//! point its vendor specified.
//!
//! Consequences, stated plainly: over SPI a full-screen flush is 153,600
//! bytes at 5.625 MHz ≈ 220 ms, so that path is for static frames. Neither
//! path is in the DWT-timed measurement window (see `show_panel_scene` and
//! `ltdc::show_scene`). Nothing about the existing 480×270
//! measurement changes — `--features board` alone still builds and behaves
//! exactly as before.
//!
//! # [`panel_init`]'s two sequences, and every delta from ST's
//!
//! `rgb = true` reproduces ST's sequence for this board EXACTLY (source:
//! `STMicroelectronics/stm32-ili9341` → `ili9341.c` `ili9341_Init()`,
//! byte-for-byte identical to `trezor-firmware`
//! `core/embed/io/display/stm32f429i-disc1/ili9341_spi.c` `ili9341_init()`),
//! plus the one addition marked below. `rgb = false` is the `lcd` path's
//! serial-GRAM variant:
//!
//! | Step | ST / Trezor | `rgb=true` (ltdc) | `rgb=false` (lcd) | why |
//! |------|-------------|-------------------|-------------------|-----|
//! | `0xB0` RGB Interface Signal Control | `0xC2` | `0xC2` (ST's) | omitted | configures the DPI path only the LTDC leg uses |
//! | `0xF6` Interface Control | `01 00 06` | `01 00 06` (ST's) | `01 00 00` | RM/DM: RGB bus vs serial port feeds GRAM |
//! | `0x3A` COLMOD | (never set) | (not set — ST's) | `0x55` | ST leaves DBI pixel format alone because LTDC sets the DPI format; writing GRAM over SPI needs 16 bpp DBI declared |
//! | `0x2C` trailing RAMWR | sent | sent (ST's) | not sent | ST leaves the panel in "memory write"; the serial path re-issues it per band anyway |
//! | `0x01` SWRESET | (never sent) | sent first | sent first | the panel's RESX is not on a GPIO, so a re-flash without a power cycle would otherwise inherit the previous run's register state |
//! | `0x28` DISPLAY_OFF | Trezor sends, ST does not | sent | sent | Trezor's addition, kept: configure a quiet panel |

// Only the SPI pixel path renders; with `ltdc` the scene comes from
// `crate::ltdc` instead and this module is the command channel alone.
#[cfg(all(feature = "lcd", not(feature = "ltdc")))]
use vyr_core::{Assets, Fonts, Quality, Rect, RenderError};

#[cfg(all(feature = "lcd", not(feature = "ltdc")))]
use crate::opaque;

// --- board addresses (RM0090; the F429 memory map) --------------------------

const RCC_AHB1ENR: *mut u32 = 0x4002_3830 as *mut u32;
const RCC_APB2ENR: *mut u32 = 0x4002_3844 as *mut u32;

const GPIOC: u32 = 0x4002_0800;
const GPIOD: u32 = 0x4002_0C00;
const GPIOF: u32 = 0x4002_1400;

const SPI5: u32 = 0x4001_5000;
const SPI_CR1: u32 = 0x00;
const SPI_SR: u32 = 0x08;
const SPI_DR: u32 = 0x0C;

const SPI_SR_TXE: u32 = 1 << 1;
const SPI_SR_BSY: u32 = 1 << 7;

/// LCD /CS — PC2 (`LCD_NCS_PIN`/`LCD_NCS_GPIO_PORT`).
const CS_PIN: u32 = 2;
/// Gyro /CS — PC1 (`GYRO_CS_PIN`). Driven high and left there: the L3GD20 is
/// on the same SPI5 and would otherwise be free to answer.
const GYRO_CS_PIN: u32 = 1;
/// D/CX — PD13 (`LCD_WRX_PIN`): low = command byte, high = parameter/pixel.
const DC_PIN: u32 = 13;
/// RDX — PD12 (`LCD_RDX_PIN`). Held high (no reads) so the panel never drives
/// the shared data line.
const RDX_PIN: u32 = 12;

/// Every hardware wait in this file is bounded, for the same reason
/// [`crate::m4::clock_init_180mhz`] bounds its spins: an unbounded wait on a
/// flag that never asserts is a silent hang with no output. One SPI byte at
/// 5.625 MHz is 1.42 µs ≈ 256 core cycles at 180 MHz; a loop pass is a few
/// cycles, so 100,000 passes is ~3 orders of magnitude past the longest thing
/// waited on here. On expiry the write is abandoned and the flush continues —
/// a torn frame is visible and diagnosable, a wedged board is not.
const SPIN_LIMIT: u32 = 100_000;

// --- tiny volatile helpers --------------------------------------------------

pub(crate) fn rd(addr: u32) -> u32 {
    // SAFETY: fixed peripheral address, 4-aligned, read-only.
    unsafe { core::ptr::read_volatile(addr as *const u32) }
}

pub(crate) fn wr(addr: u32, v: u32) {
    // SAFETY: fixed peripheral address, 4-aligned.
    unsafe { core::ptr::write_volatile(addr as *mut u32, v) }
}

/// Set a pin to general-purpose push-pull output, high speed.
fn gpio_output(port: u32, pin: u32) {
    let moder = rd(port) & !(0b11 << (pin * 2));
    wr(port, moder | (0b01 << (pin * 2))); // MODER: output
    wr(port + 0x04, rd(port + 0x04) & !(1 << pin)); // OTYPER: push-pull
    let os = rd(port + 0x08) & !(0b11 << (pin * 2));
    wr(port + 0x08, os | (0b10 << (pin * 2))); // OSPEEDR: high
    wr(port + 0x0C, rd(port + 0x0C) & !(0b11 << (pin * 2))); // PUPDR: none
}

/// Set a pin to alternate function `af`, push-pull, high speed.
pub(crate) fn gpio_af(port: u32, pin: u32, af: u32) {
    let os = rd(port + 0x08) & !(0b11 << (pin * 2));
    wr(port + 0x08, os | (0b10 << (pin * 2))); // OSPEEDR: high
    wr(port + 0x04, rd(port + 0x04) & !(1 << pin)); // OTYPER: push-pull
    wr(port + 0x0C, rd(port + 0x0C) & !(0b11 << (pin * 2))); // PUPDR: none
    // AFRL (pins 0..7) at +0x20, AFRH (pins 8..15) at +0x24.
    let (reg, sh) = if pin < 8 {
        (port + 0x20, pin * 4)
    } else {
        (port + 0x24, (pin - 8) * 4)
    };
    wr(reg, (rd(reg) & !(0xF << sh)) | (af << sh));
    let moder = rd(port) & !(0b11 << (pin * 2));
    wr(port, moder | (0b10 << (pin * 2))); // MODER: alternate function
}

/// BSRR: atomic set/reset, so no read-modify-write race with anything else.
fn gpio_set(port: u32, pin: u32, high: bool) {
    wr(port + 0x18, if high { 1 << pin } else { 1 << (pin + 16) });
}

/// Busy-wait `ms` milliseconds off DWT_CYCCNT at the achieved core clock.
///
/// A firmware delay loop, not a host-side timeout: the ILI9341's power-on and
/// sleep-out sequences have mandatory settling times (the datasheet requires
/// 120 ms after SLPOUT before the next command), and there is no other clock
/// on this vehicle. Reads the clock the part actually reached rather than
/// assuming 180 MHz, so a fallback-clock boot still waits long enough.
pub(crate) fn delay_ms(ms: u32) {
    let per_ms = crate::m4::sysclk_hz() / 1000;
    let target = per_ms.saturating_mul(ms);
    let t0 = crate::m4::clock_cycles() as u32;
    while (crate::m4::clock_cycles() as u32).wrapping_sub(t0) < target {
        core::hint::spin_loop();
    }
}

// --- SPI5 -------------------------------------------------------------------

/// Push one byte, waiting for the transmit register to drain first.
/// Returns false if the bounded wait expired (see [`SPIN_LIMIT`]).
fn spi_byte(b: u8) -> bool {
    let mut n = 0u32;
    while rd(SPI5 + SPI_SR) & SPI_SR_TXE == 0 {
        n += 1;
        if n >= SPIN_LIMIT {
            return false;
        }
        core::hint::spin_loop();
    }
    wr(SPI5 + SPI_DR, b as u32);
    true
}

/// Wait for the shift register to empty — MUST happen before /CS is raised or
/// the last byte is cut off mid-frame.
fn spi_drain() -> bool {
    let mut n = 0u32;
    while rd(SPI5 + SPI_SR) & (SPI_SR_TXE | SPI_SR_BSY) != SPI_SR_TXE {
        n += 1;
        if n >= SPIN_LIMIT {
            return false;
        }
        core::hint::spin_loop();
    }
    true
}

fn cs(low: bool) {
    gpio_set(GPIOC, CS_PIN, !low);
}

fn dc_command() {
    gpio_set(GPIOD, DC_PIN, false);
}

fn dc_data() {
    gpio_set(GPIOD, DC_PIN, true);
}

/// One command byte (D/CX low), framed by its own /CS pulse — ST's framing.
fn cmd(c: u8) {
    dc_command();
    cs(true);
    spi_byte(c);
    spi_drain();
    cs(false);
    dc_data();
}

/// One parameter byte (D/CX high), framed by its own /CS pulse.
fn data(d: u8) {
    dc_data();
    cs(true);
    spi_byte(d);
    spi_drain();
    cs(false);
}

/// Open a long data burst: /CS stays low for the whole run (the normal way to
/// stream GRAM; per-byte framing would triple the wire time).
#[cfg(all(feature = "lcd", not(feature = "ltdc")))]
fn burst_begin() {
    dc_data();
    cs(true);
}

#[cfg(all(feature = "lcd", not(feature = "ltdc")))]
fn burst_end() {
    spi_drain();
    cs(false);
}

/// Bring up SPI5 + the three control pins exactly as ST's BSP does.
pub(crate) fn spi_init() {
    // GPIOC (/CS, gyro /CS), GPIOD (D/CX, RDX), GPIOF (SPI5) clocks.
    wr(
        RCC_AHB1ENR as u32,
        rd(RCC_AHB1ENR as u32) | (1 << 2) | (1 << 3) | (1 << 5),
    );
    // SPI5 clock (APB2 bit 20).
    wr(RCC_APB2ENR as u32, rd(RCC_APB2ENR as u32) | (1 << 20));

    gpio_output(GPIOC, CS_PIN);
    gpio_output(GPIOC, GYRO_CS_PIN);
    gpio_output(GPIOD, DC_PIN);
    gpio_output(GPIOD, RDX_PIN);
    gpio_set(GPIOC, CS_PIN, true); // deselect the panel
    gpio_set(GPIOC, GYRO_CS_PIN, true); // and the gyro sharing the bus
    gpio_set(GPIOD, RDX_PIN, true); // never read
    dc_data();

    gpio_af(GPIOF, 7, 5); // SCK
    gpio_af(GPIOF, 9, 5); // MOSI
    // PF8/MISO is deliberately left alone: on this board the panel cannot be
    // read in 2-line mode (ST's driver says so in as many words), so an input
    // that only the gyro drives is left out of the LCD's business.

    // CR1: master, mode 0, 8-bit, MSB first, software NSS asserted, BR = /16
    // (PCLK2 90 MHz -> 5.625 MHz), SPI enabled.
    const MSTR: u32 = 1 << 2;
    const BR_DIV16: u32 = 0b011 << 3;
    const SPE: u32 = 1 << 6;
    const SSI: u32 = 1 << 8;
    const SSM: u32 = 1 << 9;
    wr(SPI5 + SPI_CR1, MSTR | BR_DIV16 | SSI | SSM);
    wr(SPI5 + SPI_CR1, rd(SPI5 + SPI_CR1) | SPE);
}

// --- read-back probe (bit-banged) -------------------------------------------
//
// SPI to a display is write-only: the peripheral will happily clock bytes into
// an unpowered, absent or wrongly-wired panel and report success. The ONLY way
// this vehicle can produce hardware evidence that the ILI9341 is present,
// powered and running the configuration it was sent is to make it talk back.
//
// It cannot be done with SPI5 as configured. ST says so directly: "On
// STM32F429I-Discovery, LCD ID cannot be read then keep a common configuration
// for LCD and GYRO (SPI_DIRECTION_2LINES) ... To read a register a LCD,
// SPI_DIRECTION_1LINE should be set" (`ili9341_spi.c`). The panel's SDO shares
// the MOSI wire (PF9); PF8/MISO belongs to the gyro. Rather than fight the
// peripheral's half-duplex mode — whose continuous clock generation is awkward
// to stop on an exact bit boundary — the probe below bit-bangs PF7/PF9 as
// plain GPIO. It is slow (~450 kHz), it runs ONCE, and it runs LAST, after the
// frame is already on the glass, so it can never be the reason nothing is
// visible.

/// One bit-bang half period. ~600 core cycles at 180 MHz ≈ 3.3 µs, so ~150 kHz.
/// The ILI9341's read cycle minimum is 400 ns (datasheet AC characteristics for
/// the serial interface); this is an order of magnitude slower, because the
/// probe's job is to be believable, not fast.
fn bb_delay() {
    for i in 0..600u32 {
        core::hint::black_box(i);
    }
}

fn gpio_input_pulldown(port: u32, pin: u32) {
    wr(port, rd(port) & !(0b11 << (pin * 2))); // MODER: input
    let pu = rd(port + 0x0C) & !(0b11 << (pin * 2));
    // Pull-down, so a line nobody drives reads a clean 0x00 rather than noise
    // that could be mistaken for a reply.
    wr(port + 0x0C, pu | (0b10 << (pin * 2)));
}

fn gpio_read(port: u32, pin: u32) -> bool {
    rd(port + 0x10) & (1 << pin) != 0
}

/// SCK = PF7, SDA = PF9 while bit-banging.
const BB_SCK: u32 = 7;
const BB_SDA: u32 = 9;

/// Clock out one byte, MSB first, sampled by the panel on the rising edge.
fn bb_write_byte(b: u8) {
    for i in (0..8).rev() {
        gpio_set(GPIOF, BB_SDA, (b >> i) & 1 != 0);
        bb_delay();
        gpio_set(GPIOF, BB_SCK, true);
        bb_delay();
        gpio_set(GPIOF, BB_SCK, false);
    }
}

/// Clock in one byte, MSB first, sampled on the rising edge.
fn bb_read_byte() -> u8 {
    let mut v = 0u8;
    for _ in 0..8 {
        bb_delay();
        gpio_set(GPIOF, BB_SCK, true);
        v = (v << 1) | u8::from(gpio_read(GPIOF, BB_SDA));
        bb_delay();
        gpio_set(GPIOF, BB_SCK, false);
    }
    v
}

/// Send `reg` and clock back `n` RAW bytes (max 5) with no dummy-bit guessing.
///
/// Different ILI9341 read commands prepend a different number of dummy clocks,
/// and implementations disagree about whether that is one bit or one byte.
/// Rather than encode a guess, this returns the unshifted bit stream: the
/// expected payload appears at whatever offset the silicon actually uses, and
/// the log shows the whole thing.
fn bb_read_raw(reg: u8, n: usize) -> [u8; 5] {
    let mut out = [0u8; 5];
    // Take PF7/PF9 away from SPI5 for the duration.
    wr(SPI5 + SPI_CR1, rd(SPI5 + SPI_CR1) & !(1 << 6)); // SPE = 0
    gpio_output(GPIOF, BB_SCK);
    gpio_output(GPIOF, BB_SDA);
    gpio_set(GPIOF, BB_SCK, false); // CPOL = 0: idle low

    dc_command();
    cs(true);
    bb_write_byte(reg);
    dc_data();
    gpio_input_pulldown(GPIOF, BB_SDA); // the panel drives the shared line now
    for slot in out.iter_mut().take(n.min(5)) {
        *slot = bb_read_byte();
    }
    cs(false);

    // Hand the pins back to SPI5 and re-enable it.
    gpio_af(GPIOF, BB_SCK, 5);
    gpio_af(GPIOF, BB_SDA, 5);
    wr(SPI5 + SPI_CR1, rd(SPI5 + SPI_CR1) | (1 << 6)); // SPE = 1
    out
}

/// Interrogate the panel and report what it says.
///
/// Reads, and what a live correctly-configured ILI9341 owes us:
///
/// * `0xD3` RDDID4 — the device ID `0x93 0x41` (ST's `ILI9341_ID = 0x9341`).
/// * `0x0B` RDDMADCTL — should echo the `0xC8` this driver wrote.
/// * `0x0C` RDDCOLMOD — should echo `0x55` (16 bpp DBI), the delta from ST's
///   sequence that makes serial GRAM writes meaningful.
/// * `0x0A` RDDPM — display power mode; bit 2 = display on, bit 4 = sleep out.
///
/// An all-`0x00` reply means the shared data line was never driven, which is
/// INCONCLUSIVE rather than a failure — it says the panel did not answer on
/// this wire, not that it did not receive. Either way this runs after the
/// frame has been flushed and changes nothing on screen.
pub(crate) fn panel_probe(emit: &mut dyn FnMut(&str)) {
    let hex = |b: &[u8; 5]| {
        alloc::format!(
            "{:02x} {:02x} {:02x} {:02x} {:02x}",
            b[0],
            b[1],
            b[2],
            b[3],
            b[4]
        )
    };
    let id = bb_read_raw(0xD3, 5);
    let mad = bb_read_raw(0x0B, 3);
    let col = bb_read_raw(0x0C, 3);
    let pm = bb_read_raw(0x0A, 3);
    let answered = [&id, &mad, &col, &pm]
        .iter()
        .any(|b| b.iter().any(|&x| x != 0x00 && x != 0xFF));
    emit(&alloc::format!(
        "INFO  [vyr-size] lcd probe (bit-banged, raw, dummy bits NOT stripped): \
         RDDID4(D3)=[{}] RDDMADCTL(0B)=[{}] RDDCOLMOD(0C)=[{}] RDDPM(0A)=[{}] answered={answered}",
        hex(&id),
        hex(&mad),
        hex(&col),
        hex(&pm)
    ));
}

// --- ILI9341 ----------------------------------------------------------------

/// Panel geometry (`ILI9341_LCD_PIXEL_WIDTH/HEIGHT` in ST's `ili9341.h`).
pub const PANEL_W: u32 = 240;
/// Panel geometry.
pub const PANEL_H: u32 = 320;

/// ST's init for this exact panel, with the deltas tabulated in the module
/// header. The magic numbers are ST's — `ili9341.c` in
/// `STMicroelectronics/stm32-ili9341`, mirrored by `trezor-firmware`'s
/// `core/embed/io/display/stm32f429i-disc1/ili9341_spi.c` — and are NOT
/// re-derived here from the datasheet.
///
/// `rgb` selects who feeds the controller's GRAM:
///
/// * `true` — the parallel RGB (DPI) bus, i.e. LTDC scan-out. This is ST's
///   own configuration for this board, reproduced exactly.
/// * `false` — the serial (DBI) port, i.e. `0x2C` writes over SPI5.
///
/// Passing a compile-time-constant `rgb` from each leg's entry point means
/// the branch folds away and neither build carries the other's bytes.
pub(crate) fn panel_init(rgb: bool) {
    cmd(0x01); // SWRESET — RESX is not on a GPIO; start from a known state
    delay_ms(150); // datasheet: 120 ms minimum before the next command
    cmd(0x28); // display off while the panel is being configured

    cmd(0xCA);
    for b in [0xC3, 0x08, 0x50] {
        data(b);
    }
    cmd(0xCF); // POWERB
    for b in [0x00, 0xC1, 0x30] {
        data(b);
    }
    cmd(0xED); // POWER_SEQ
    for b in [0x64, 0x03, 0x12, 0x81] {
        data(b);
    }
    cmd(0xE8); // DTCA
    for b in [0x85, 0x00, 0x78] {
        data(b);
    }
    cmd(0xCB); // POWERA
    for b in [0x39, 0x2C, 0x00, 0x34, 0x02] {
        data(b);
    }
    cmd(0xF7); // PRC (pump ratio)
    data(0x20);
    cmd(0xEA); // DTCB
    for b in [0x00, 0x00] {
        data(b);
    }
    cmd(0xB1); // FRMCTR1
    for b in [0x00, 0x1B] {
        data(b);
    }
    cmd(0xB6); // DFC
    for b in [0x0A, 0xA2] {
        data(b);
    }
    cmd(0xC0); // POWER1
    data(0x10);
    cmd(0xC1); // POWER2
    data(0x10);
    cmd(0xC5); // VCOM1
    for b in [0x45, 0x15] {
        data(b);
    }
    cmd(0xC7); // VCOM2
    data(0x90);
    cmd(0x36); // MADCTL — ST's 0xC8 (MY|MX|BGR) for this board's panel
    data(0xC8);
    cmd(0xF2); // 3GAMMA_EN
    data(0x00);
    if rgb {
        // ST's `LCD_RGB_INTERFACE` step: RGB interface signal control. Bit 7
        // = ByPass_MODE (memory), bit 6 = RCM (DE mode, i.e. VSYNC/HSYNC/DE
        // all used), bits 1:0 = VSPL/HSPL polarity — 0xC2 is what ST ships
        // for this panel and what the LTDC timing below is matched to.
        cmd(0xB0);
        data(0xC2);
    }
    // DELTA vs ST when !rgb: 0xB0 is omitted, because that DPI path is unused.
    cmd(0xB6); // DFC again, ST's four-parameter form
    for b in [0x0A, 0xA7, 0x27, 0x04] {
        data(b);
    }

    cmd(0x2A); // CASET 0..239
    for b in [0x00, 0x00, 0x00, 0xEF] {
        data(b);
    }
    cmd(0x2B); // PASET 0..319
    for b in [0x00, 0x00, 0x01, 0x3F] {
        data(b);
    }

    // THE register. Third parameter 0x06 (ST's) => RM=1, GRAM is written from
    // the RGB interface and DM=01 selects RGB-interface display operation:
    // LTDC owns the pixels. 0x00 => RM=0/DM=00, GRAM written from the system
    // (serial) interface and refreshed on the panel's internal clock: the SPI
    // port owns the pixels.
    cmd(0xF6); // Interface Control
    for b in [0x01, 0x00, if rgb { 0x06 } else { 0x00 }] {
        data(b);
    }

    if !rgb {
        // DELTA vs ST: declare the DBI pixel format. ST never sets COLMOD
        // because the DPI format comes from LTDC; a serial GRAM write must
        // say 16 bpp. Sending it in RGB mode would be a deviation from ST's
        // sequence for no gain, so the LTDC leg does not.
        cmd(0x3A);
        data(0x55); // DPI 16 bpp | DBI 16 bpp
    }

    cmd(0x2C); // ST's sequence pokes GRAM here, then settles
    delay_ms(200);

    cmd(0x26); // GAMMA set
    data(0x01);
    cmd(0xE0); // positive gamma
    for b in [
        0x0F, 0x29, 0x24, 0x0C, 0x0E, 0x09, 0x4E, 0x78, 0x3C, 0x09, 0x13, 0x05, 0x17, 0x11, 0x00,
    ] {
        data(b);
    }
    cmd(0xE1); // negative gamma
    for b in [
        0x00, 0x16, 0x1B, 0x04, 0x11, 0x07, 0x31, 0x33, 0x42, 0x05, 0x0C, 0x0A, 0x28, 0x2F, 0x0F,
    ] {
        data(b);
    }

    cmd(0x11); // SLEEP_OUT
    delay_ms(200);
    cmd(0x29); // DISPLAY_ON
    if rgb {
        // ST's sequence ends here: "GRAM start writing". In RGB mode the
        // writer is the DPI bus, so this arms the controller to accept the
        // LTDC scan-out that is about to start.
        cmd(0x2C);
    }
}

/// Point the GRAM address window at `[x, x+w) x [y, y+h)` and leave the panel
/// in "memory write" so the caller can stream pixels.
#[cfg(all(feature = "lcd", not(feature = "ltdc")))]
fn set_window(x: u32, y: u32, w: u32, h: u32) {
    let (x1, y1) = (x + w - 1, y + h - 1);
    cmd(0x2A);
    for b in [(x >> 8) as u8, x as u8, (x1 >> 8) as u8, x1 as u8] {
        data(b);
    }
    cmd(0x2B);
    for b in [(y >> 8) as u8, y as u8, (y1 >> 8) as u8, y1 as u8] {
        data(b);
    }
    cmd(0x2C); // memory write
}

/// RGB888 -> RGB565, the panel's wire format. Truncation, not dithering: the
/// scene is flat-shaded UI, and a dither would put pixels on the glass that
/// the renderer never produced.
#[inline]
pub(crate) fn rgb565(r: u8, g: u8, b: u8) -> u16 {
    ((r as u16 & 0xF8) << 8) | ((g as u16 & 0xFC) << 3) | (b as u16 >> 3)
}

/// Paint the whole panel one colour. The FIRST thing the LCD path does, on
/// purpose: if the scene render later fails, a uniformly coloured panel is
/// still proof that clocks, SPI5, the control pins and the ILI9341's init all
/// work, and separates "pixel path broken" from "renderer broken" by looking.
#[cfg(all(feature = "lcd", not(feature = "ltdc")))]
fn fill(color: u16) {
    set_window(0, 0, PANEL_W, PANEL_H);
    let (hi, lo) = ((color >> 8) as u8, color as u8);
    burst_begin();
    for _ in 0..(PANEL_W * PANEL_H) {
        spi_byte(hi);
        spi_byte(lo);
    }
    burst_end();
}

/// Push one rendered RGB888 band to the panel at `(0, y)`.
///
/// `band` is exactly the buffer vyr-core just rendered into — no copy, no
/// intermediate framebuffer. This is the whole reason banding maps onto this
/// panel: the controller holds the frame memory, so the host never needs one.
#[cfg(all(feature = "lcd", not(feature = "ltdc")))]
fn flush_band(y: u32, h: u32, band: &[u8]) {
    set_window(0, y, PANEL_W, h);
    burst_begin();
    for px in band[..(PANEL_W * h * 3) as usize].chunks_exact(3) {
        let c = rgb565(px[0], px[1], px[2]);
        spi_byte((c >> 8) as u8);
        spi_byte(c as u8);
    }
    burst_end();
}

// --- the panel-native scene -------------------------------------------------

/// Band height for the panel scene: 240×16, matching the 480×16 the
/// measurement workload uses, so the two legs stress the same seam-crossing
/// geometry. Gutter pixmap (240+16)·(16+16)·4 = 32,768 B — half the 480-wide
/// one, comfortably inside the same 120 KiB arena.
pub const PANEL_BAND_H: u32 = 16;

/// RGB888 bytes of one panel band. Smaller than
/// [`crate::workload::BAND_BYTES`], so this leg borrows a prefix of the
/// existing CCM band buffer rather than adding a second one.
pub const PANEL_BAND_BYTES: usize = (PANEL_W as usize) * 3 * (PANEL_BAND_H as usize);

/// 240×320 portrait fixture — the SAME widget vocabulary as
/// [`crate::workload::FIXTURE_IR`] (frame, label, image, gauge, lcd, two
/// sliders, progress, toggle, line), re-laid-out for the panel instead of
/// scaled. Scaling a 480×270 scene onto a 240×320 panel would have meant
/// inventing a resampler and would have made every pixel on the glass a
/// different computation from the one the perf numbers describe.
///
/// The text is deliberately asymmetric and reads left-to-right, so a
/// photograph settles orientation and mirroring (MADCTL) without a scope.
pub const PANEL_IR: &str = r##"{
  "schema_version": "0.6-vyvanse",
  "w": 240, "h": 320,
  "root": {"name": "view", "attrs": {"background": "#22262B"}, "children": [
    {"name": "vy_frame", "attrs": {"x": "8", "y": "8", "width": "224", "height": "40",
      "background": "#2E3440", "radius": "8", "border_width": "1", "border_color": "#4C566A"}},
    {"name": "vy_label", "attrs": {"x": "20", "y": "20", "width": "170", "height": "20",
      "text": "vyr on F429I-DISC1", "color": "#ECEFF4"}},
    {"name": "vy_image", "attrs": {"x": "200", "y": "16", "width": "24", "height": "24",
      "src": "checker-24.png"}},
    {"name": "vy_gauge", "attrs": {"x": "16", "y": "60", "width": "110", "height": "110",
      "value": "65", "color": "#88C0D0"}},
    {"name": "vy_lcd", "attrs": {"x": "138", "y": "100", "width": "90", "height": "24",
      "text": "1480", "color": "#A3BE8C", "style_text_font": "roboto_20"}},
    {"name": "vy_label", "attrs": {"x": "140", "y": "132", "width": "90", "height": "16",
      "text": "rpm", "color": "#7A869A"}},
    {"name": "vy_slider", "attrs": {"x": "16", "y": "186", "width": "208", "height": "18",
      "value": "62"}},
    {"name": "vy_slider", "attrs": {"x": "16", "y": "214", "width": "208", "height": "18",
      "value": "35"}},
    {"name": "vy_progress", "attrs": {"x": "16", "y": "244", "width": "208", "height": "12",
      "value": "80"}},
    {"name": "vy_toggle", "attrs": {"x": "16", "y": "266", "width": "56", "height": "28",
      "value": "1"}},
    {"name": "vy_label", "attrs": {"x": "84", "y": "272", "width": "120", "height": "18",
      "text": "bypass", "color": "#D8DEE9"}},
    {"name": "vy_line", "attrs": {"x": "8", "y": "300", "width": "224", "height": "2",
      "background": "#4C566A"}},
    {"name": "vy_label", "attrs": {"x": "10", "y": "304", "width": "220", "height": "14",
      "text": "awto / vyr 240x320 ILI9341", "color": "#7A869A"}}
  ]}
}"##;

/// FNV-1a 64 over the panel frame — the same fold [`crate::workload`] applies
/// to the 480×270 frame. The panel scene is a different scene so it has its
/// own hash; what matters is that it is STABLE and that the bytes hashed are
/// the bytes sent to the glass.
pub(crate) const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x100_0000_01b3;

pub(crate) fn fnv1a_fold(mut h: u64, data: &[u8]) -> u64 {
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(FNV_PRIME);
    }
    h
}

/// Bring the panel up and draw [`PANEL_IR`] on it, band by band.
///
/// Ordering is the point of this function:
///
/// 1. SPI5 + ILI9341 init, then a solid fill — the pixel path is proven (or
///    disproven) by eye before the renderer is involved at all.
/// 2. For each 240×16 band: `render(...)` into `band_buf`, fold the output
///    bytes into the frame hash, THEN flush to the panel.
///
/// **No part of this is inside a timed window.** The DWT-timed loop lives in
/// [`crate::workload::run`] and never touches the display; flush cost is a
/// driver property and pricing it as render cost would be a lie. `delay_ms`
/// reads DWT_CYCCNT but only ever as a stopwatch for the panel's own settling
/// requirements.
#[cfg(all(feature = "lcd", not(feature = "ltdc")))]
pub fn show_panel_scene(
    emit: &mut dyn FnMut(&str),
    band_buf: &mut [u8],
    quality: Quality,
) -> Result<u64, RenderError> {
    emit(&alloc::format!(
        "INFO  [vyr-size] lcd: ILI9341 {PANEL_W}x{PANEL_H} over SPI5 \
         (SCK PF7 / MOSI PF9 / CS PC2 / DCX PD13, PCLK2/16 = 5.625 MHz), \
         GRAM fed from the serial port (0xF6 RM=0 DM=00)"
    ));
    spi_init();
    panel_init(false); // serial GRAM: this leg's whole point

    // Milestone 1: a solid colour. If the render below dies, THIS is what the
    // board shows, and it says "SPI, control pins and panel init are fine".
    fill(rgb565(0x00, 0x10, 0x30));
    emit(
        "INFO  [vyr-size] lcd: panel init + full-screen fill done (dark blue) — if the board is showing a flat blue screen and nothing else, the pixel path works and the scene render failed",
    );

    let mut fonts = Fonts::new();
    fonts.register("roboto", opaque(crate::workload::SUBSET_FONT).to_vec())?;
    let mut assets = Assets::new();
    assets.register(
        vyr_core::demo::IMAGE_ASSET,
        vyr_core::RgbaImage::new(24, 24, opaque(crate::workload::CHECKER).to_vec())?,
    )?;
    let req = vyr_core::ir::Request::parse(opaque(PANEL_IR))?;

    let stride = (PANEL_W * 3) as usize;
    let mut hash = FNV_OFFSET;
    let mut pixels = 0u64;
    let mut y = 0u32;
    while y < PANEL_H {
        let h = PANEL_BAND_H.min(PANEL_H - y);
        let band = Rect {
            x: 0,
            y: y as i32,
            w: PANEL_W,
            h,
        };
        let len = stride * h as usize;
        // The ONLY render entry point, same as every other leg: a caller-owned
        // buffer, an arbitrary sub-rect of the scene, an explicit stride.
        let stats = req.render_with_quality(
            &mut fonts,
            &assets,
            band,
            &mut band_buf[..len],
            stride,
            quality,
        )?;
        pixels += stats.pixels_written;
        hash = fnv1a_fold(hash, &band_buf[..len]);
        // ...and only THEN does the driver get involved.
        flush_band(y, h, &band_buf[..len]);
        y += h;
    }

    emit(&alloc::format!(
        "INFO  [vyr-size] lcd: panel frame fnv1a={hash:#018x} bands={} \
         pixels_written={pixels} flushed={} B over SPI",
        PANEL_H.div_ceil(PANEL_BAND_H),
        PANEL_W * PANEL_H * 2
    ));
    emit(
        "ALERT [vyr-size] lcd: frame is on the glass — the panel refreshes itself from ILI9341 GRAM, so the image persists after the target halts",
    );
    // LAST, deliberately: ask the controller to identify itself and echo back
    // the configuration it was given. Nothing here changes what is displayed.
    panel_probe(emit);
    Ok(hash)
}

//! `--features ltdc` (#30) — hardware scan-out: FMC/SDRAM + PLLSAI + LTDC
//! driving the ILI9341's 16-bit parallel RGB bus, instead of pushing pixels
//! one bit at a time down SPI5.
//!
//! Strictly additive to `board`, exactly as `lcd` is: `--no-default-features
//! --features board` compiles none of this and remains the byte-for-byte
//! configuration the 480×270 cycle/hash numbers were taken on. `lcd` and
//! `ltdc` are alternative display paths over the SAME [`crate::lcd`] command
//! channel — SPI5 stays the register port either way.
//!
//! # Why this exists
//!
//! The `lcd` path is honest but slow: 153,600 B of RGB565 at 5.625 MHz is
//! ~220 ms per full screen, so it can show a frame but not animate one. The
//! panel's own controller can instead be told to take its pixels from the
//! parallel RGB bus, and the F429's LTDC peripheral will then scan a
//! framebuffer out of memory at a fixed refresh rate with **zero CPU
//! involvement**. Display cost stops being a per-frame charge on the CPU and
//! becomes a memory-bandwidth charge on the bus.
//!
//! # Where the framebuffer lives, and why not SRAM
//!
//! One 240×320 RGB565 framebuffer is 153,600 B and *would* fit the F429's
//! 192 KiB SRAM. It still belongs in SDRAM:
//!
//! * LTDC re-reads the ENTIRE framebuffer every refresh (~65×/s here,
//!   ~10 MB/s), contending on the bus matrix with instruction fetch, the
//!   heap and the renderer. Putting that traffic in external memory keeps it
//!   off the internal SRAM ports.
//! * Double buffering is 307,200 B and does not fit SRAM at all.
//! * The heap arena ([`crate::m4`]) already claims 120 KiB of the 192 KiB.
//!
//! So: FMC SDRAM **bank 2, base `0xD0000000`**, the IS42S16400J-class
//! 64 Mbit / 8 MiB part this board carries. Its bring-up is
//! [`sdram_init`], and it is NOT trusted until [`sdram_test`] has walked the
//! data bus, the address bus and all 8 MiB of it.
//!
//! This leg runs **single buffered** and therefore tears; see [`render_rect`]
//! for why a second buffer is not a free upgrade (the plan's two-frames-stale
//! rule) even though the SDRAM has ample room for one.
//!
//! # Provenance — every constant below has a vendor source
//!
//! Cached by `scripts/lcd-docs.py` into `tmp/lcd-docs/`; nothing here is
//! derived from a datasheet where working reference code exists.
//!
//! * **SDRAM timing/geometry and the 6-command init sequence**:
//!   `STMicroelectronics/32f429idiscovery-bsp` →
//!   `stm32f429i_discovery_sdram.c` (`BSP_SDRAM_Init`,
//!   `BSP_SDRAM_Initialization_sequence`) and `..._sdram.h`
//!   (`SDRAM_DEVICE_ADDR 0xD0000000`, `SDRAM_DEVICE_SIZE 0x800000`,
//!   `REFRESH_COUNT 1386`, the mode-register fields).
//! * **SDRAM pin map (~40 pins, AF12)**: the same file's `BSP_SDRAM_MspInit`
//!   pin-assignment table, reproduced verbatim in [`SDRAM_PINS`].
//! * **LTDC pin map (22 pins — 18 colour + HSYNC/VSYNC/CLK/DE — AF14 with
//!   four exceptions on AF9)**: `..._lcd.c`
//!   `BSP_LCD_MspInit`, reproduced in [`LTDC_PINS`].
//! * **LTDC timing and PLLSAI dividers**: `..._lcd.c` `BSP_LCD_Init`
//!   (`AccumulatedActiveW 269`, `AccumulatedActiveH 323`, `TotalWidth 279`,
//!   `TotalHeigh 327`, `PLLSAIN 192`, `PLLSAIR 4`, `PLLSAIDIVR_8`) with
//!   HSYNC/HBP/VSYNC/VBP from `stm32-ili9341` → `ili9341.h`.
//! * **Register OFFSETS** (not guessed): the CMSIS device header
//!   `STMicroelectronics/cmsis_device_f4` → `stm32f429xx.h`, structs
//!   `LTDC_TypeDef`, `LTDC_Layer_TypeDef`, `FMC_Bank5_6_TypeDef`,
//!   `RCC_TypeDef`.
//! * **Independent cross-check on the same board**: `trezor/trezor-firmware`
//!   → `core/embed/io/display/stm32f429i-disc1/display_ltdc.c`, which agrees
//!   with ST's timing line for line and — unlike ST's ARGB8888 default —
//!   also uses `LTDC_PIXEL_FORMAT_RGB565`, as here.
//!
//! # What is measured, and what is deliberately not
//!
//! The 480×270 workload's DWT window in [`crate::workload::run`] is never
//! entered from this module; the LTDC leg opens its OWN window and reports
//! render and blit cycles separately, so "cost of drawing" is never conflated
//! with "cost of displaying". Scan-out itself costs the CPU nothing at all
//! and is therefore not in any window — it is priced in the refresh rate the
//! hardware reports back.

use vyr_core::{Assets, Fonts, Quality, Rect, RenderError};

use crate::lcd::{
    self, PANEL_BAND_H, PANEL_H, PANEL_IR, PANEL_W, delay_ms, fnv1a_fold, gpio_af, rd, rgb565, wr,
};
use crate::opaque;

// --- RCC / FMC / LTDC addresses (stm32f429xx.h struct offsets) --------------

const RCC: u32 = 0x4002_3800;
const RCC_CR: u32 = RCC; // +0x00
const RCC_PLLCFGR: u32 = RCC + 0x04;
const RCC_AHB1ENR: u32 = RCC + 0x30;
const RCC_AHB3ENR: u32 = RCC + 0x38;
const RCC_APB2ENR: u32 = RCC + 0x44;
const RCC_PLLSAICFGR: u32 = RCC + 0x88;
const RCC_DCKCFGR: u32 = RCC + 0x8C;

const RCC_CR_PLLSAION: u32 = 1 << 28;
const RCC_CR_PLLSAIRDY: u32 = 1 << 29;
const RCC_AHB3ENR_FMCEN: u32 = 1 << 0;
const RCC_APB2ENR_LTDCEN: u32 = 1 << 26;

/// GPIO port bases, A..G (RM0090 memory map, 0x400 apart).
const GPIO: [u32; 7] = [
    0x4002_0000,
    0x4002_0400,
    0x4002_0800,
    0x4002_0C00,
    0x4002_1000,
    0x4002_1400,
    0x4002_1800,
];

/// `FMC_Bank5_6_R_BASE = FMC_R_BASE + 0x140`, then the struct order
/// SDCR[2], SDTR[2], SDCMR, SDRTR, SDSR.
const FMC: u32 = 0xA000_0140;
const FMC_SDCR1: u32 = FMC;
const FMC_SDCR2: u32 = FMC + 0x04;
const FMC_SDTR1: u32 = FMC + 0x08;
const FMC_SDTR2: u32 = FMC + 0x0C;
const FMC_SDCMR: u32 = FMC + 0x10;
const FMC_SDRTR: u32 = FMC + 0x14;
const FMC_SDSR: u32 = FMC + 0x18;
const FMC_SDSR_BUSY: u32 = 1 << 5;

/// `LTDC_BASE = APB2PERIPH_BASE + 0x6800`.
const LTDC: u32 = 0x4001_6800;
const LTDC_SSCR: u32 = LTDC + 0x08;
const LTDC_BPCR: u32 = LTDC + 0x0C;
const LTDC_AWCR: u32 = LTDC + 0x10;
const LTDC_TWCR: u32 = LTDC + 0x14;
const LTDC_GCR: u32 = LTDC + 0x18;
pub(crate) const LTDC_SRCR: u32 = LTDC + 0x24;
/// LTDC_ISR (0x38) / LTDC_ICR (0x3C) — the controller's own error flags:
/// FIFO underrun and transfer error. Hardware's account of bus starvation.
#[cfg(feature = "present")]
pub(crate) const LTDC_ISR: u32 = LTDC + 0x38;
#[cfg(feature = "present")]
pub(crate) const LTDC_ICR: u32 = LTDC + 0x3C;
const LTDC_BCCR: u32 = LTDC + 0x2C;
pub(crate) const LTDC_CPSR: u32 = LTDC + 0x44;
const LTDC_CDSR: u32 = LTDC + 0x48;
/// `LTDC_Layer1_BASE = LTDC_BASE + 0x84`.
const L1: u32 = LTDC + 0x84;
pub(crate) const L1_CR: u32 = L1; // +0x00
const L1_WHPCR: u32 = L1 + 0x04;
const L1_WVPCR: u32 = L1 + 0x08;
pub(crate) const L1_PFCR: u32 = L1 + 0x10;
const L1_CACR: u32 = L1 + 0x14;
const L1_DCCR: u32 = L1 + 0x18;
const L1_BFCR: u32 = L1 + 0x1C;
pub(crate) const L1_CFBAR: u32 = L1 + 0x28;
pub(crate) const L1_CFBLR: u32 = L1 + 0x2C;
const L1_CFBLNR: u32 = L1 + 0x30;

const LTDC_GCR_LTDCEN: u32 = 1 << 0;
/// PCPOL(28) | DEPOL(29) | VSPOL(30) | HSPOL(31). ST selects active-low for
/// HS/VS/DE and "input pixel clock" for PC — i.e. all four bits ZERO — so
/// this mask is cleared and nothing is set in its place.
const LTDC_GCR_POL_MASK: u32 = 0xF << 28;
pub(crate) const LTDC_SRCR_IMR: u32 = 1 << 0;
pub(crate) const LTDC_LX_CR_LEN: u32 = 1 << 0;

// --- panel/SDRAM geometry ---------------------------------------------------

/// `SDRAM_DEVICE_ADDR` — FMC SDRAM bank 2.
pub const SDRAM_BASE: u32 = 0xD000_0000;
/// `SDRAM_DEVICE_SIZE` — 8 MiB (64 Mbit ×16).
pub const SDRAM_BYTES: u32 = 0x0080_0000;

/// The scan-out framebuffer: 240×320×2 = 153,600 B at the base of SDRAM.
pub const FB_BASE: u32 = SDRAM_BASE;
/// Bytes per framebuffer.
pub const FB_BYTES: u32 = PANEL_W * PANEL_H * 2;

/// Everything after the framebuffer is untouched by the display path and is
/// what [`sdram_test`]'s bulk pass mainly exercises.
const FB_END: u32 = FB_BASE + FB_BYTES;

// --- bounded waits ----------------------------------------------------------

/// Read DWT_CYCCNT as an unsigned tick.
#[inline]
pub(crate) fn cyc() -> u32 {
    crate::m4::clock_cycles() as u32
}

/// Spin on `f` for at most `ms` milliseconds of real core time, then give up.
///
/// Same discipline as [`crate::m4::clock_init_180mhz`] and [`crate::lcd`]: an
/// unbounded wait on a flag that never asserts is a silent hang with no
/// output, and a bring-up path is exactly where flags fail to assert. Every
/// caller states what it is waiting for and why its budget is what it is.
pub(crate) fn wait_ms(ms: u32, mut f: impl FnMut() -> bool) -> bool {
    let budget = (crate::m4::sysclk_hz() / 1000).saturating_mul(ms);
    let t0 = cyc();
    loop {
        if f() {
            return true;
        }
        if cyc().wrapping_sub(t0) > budget {
            return false;
        }
        core::hint::spin_loop();
    }
}

// --- GPIO ------------------------------------------------------------------

/// `(port index 0=A.., pin, AF)` for every FMC SDRAM signal, transcribed from
/// the pin-assignment table in ST's `BSP_SDRAM_MspInit`. All AF12 (`FMC`).
///
/// ```text
/// PD0  FMC_D2   PE0  FMC_NBL0  PF0  FMC_A0   PG0  FMC_A10
/// PD1  FMC_D3   PE1  FMC_NBL1  PF1  FMC_A1   PG1  FMC_A11
/// PD8  FMC_D13  PE7  FMC_D4    PF2  FMC_A2   PG8  FMC_SDCLK
/// PD9  FMC_D14  PE8  FMC_D5    PF3  FMC_A3   PG15 FMC_NCAS
/// PD10 FMC_D15  PE9  FMC_D6    PF4  FMC_A4   PG4  FMC_BA0
/// PD14 FMC_D0   PE10 FMC_D7    PF5  FMC_A5   PG5  FMC_BA1
/// PD15 FMC_D1   PE11 FMC_D8    PF11 FMC_NRAS
/// PB5  FMC_SDCKE1 PE12 FMC_D9  PF12 FMC_A6
/// PB6  FMC_SDNE1  PE13 FMC_D10 PF13 FMC_A7
/// PC0  FMC_SDNWE  PE14 FMC_D11 PF14 FMC_A8
///                 PE15 FMC_D12 PF15 FMC_A9
/// ```
const SDRAM_PINS: &[(u8, u8)] = &[
    (1, 5),
    (1, 6), // B
    (2, 0), // C
    (3, 0),
    (3, 1),
    (3, 8),
    (3, 9),
    (3, 10),
    (3, 14),
    (3, 15), // D
    (4, 0),
    (4, 1),
    (4, 7),
    (4, 8),
    (4, 9),
    (4, 10),
    (4, 11),
    (4, 12),
    (4, 13),
    (4, 14),
    (4, 15), // E
    (5, 0),
    (5, 1),
    (5, 2),
    (5, 3),
    (5, 4),
    (5, 5),
    (5, 11),
    (5, 12),
    (5, 13),
    (5, 14),
    (5, 15), // F
    (6, 0),
    (6, 1),
    (6, 4),
    (6, 5),
    (6, 8),
    (6, 15), // G
];
const AF_FMC: u32 = 12;

/// `(port index, pin, AF)` for every LTDC signal, from ST's `BSP_LCD_MspInit`.
/// Note the split: most pins are AF14 (`LTDC`), but PB0/PB1/PG10/PG12 reach
/// LTDC on **AF9**, which is exactly the kind of detail that makes deriving a
/// pin map from a datasheet a mistake.
///
/// ```text
/// R2 PC10  G2 PA6   B2 PD6      HSYNC PC6   VSYNC PA4
/// R3 PB0*  G3 PG10* B3 PG11     CLK   PG7   DE    PF10
/// R4 PA11  G4 PB10  B4 PG12*
/// R5 PA12  G5 PB11  B5 PA3
/// R6 PB1*  G6 PC7   B6 PB8
/// R7 PG6   G7 PD3   B7 PB9        (* = AF9, all others AF14)
/// ```
const LTDC_PINS: &[(u8, u8, u32)] = &[
    (0, 3, 14),
    (0, 4, 14),
    (0, 6, 14),
    (0, 11, 14),
    (0, 12, 14), // A
    (1, 8, 14),
    (1, 9, 14),
    (1, 10, 14),
    (1, 11, 14), // B (AF14)
    (2, 6, 14),
    (2, 7, 14),
    (2, 10, 14), // C
    (3, 3, 14),
    (3, 6, 14),  // D
    (5, 10, 14), // F
    (6, 6, 14),
    (6, 7, 14),
    (6, 11, 14), // G (AF14)
    (1, 0, 9),
    (1, 1, 9), // B (AF9)
    (6, 10, 9),
    (6, 12, 9), // G (AF9)
];

/// Enable the AHB1 clock for every GPIO port named in `pins`, then map each
/// pin to its alternate function.
fn pins_to_af(pins_af: impl Iterator<Item = (u8, u8, u32)>) {
    let mut ports = 0u32;
    let mut list: [(u8, u8, u32); 48] = [(0, 0, 0); 48];
    let mut n = 0usize;
    for p in pins_af {
        ports |= 1 << p.0;
        list[n] = p;
        n += 1;
    }
    wr(RCC_AHB1ENR, rd(RCC_AHB1ENR) | ports);
    // A dummy read-back: RCC enable bits take effect after a bus cycle, and
    // an AF write to a still-unclocked port is silently dropped.
    let _ = rd(RCC_AHB1ENR);
    for &(port, pin, af) in &list[..n] {
        gpio_af(GPIO[port as usize], pin as u32, af);
    }
}

// --- FMC SDRAM --------------------------------------------------------------

/// Snapshot of the SDRAM controller, for the runner to check rather than trust.
pub struct SdramRegs {
    pub sdcr1: u32,
    pub sdcr2: u32,
    pub sdtr1: u32,
    pub sdtr2: u32,
    pub sdrtr: u32,
    pub sdsr: u32,
}

/// Wait for the SDRAM controller to accept another command (bounded).
///
/// Budget: 10 ms. The slowest thing in this sequence is 4 auto-refresh cycles
/// at tRC = 70 ns — under a microsecond — so 10 ms is four orders of
/// magnitude of headroom and still fails fast if the FMC clock is off.
fn sdram_ready() -> bool {
    wait_ms(10, || rd(FMC_SDSR) & FMC_SDSR_BUSY == 0)
}

/// Issue one SDRAM command to bank 2. `nrfs` is the auto-refresh count
/// (1-based, as ST's API states it); `mrd` is the mode-register payload.
fn sdram_cmd(mode: u32, nrfs: u32, mrd: u32) -> bool {
    if !sdram_ready() {
        return false;
    }
    // MODE[2:0] | CTB2(3) | NRFS[8:5] = n-1 | MRD[21:9]
    wr(FMC_SDCMR, mode | (1 << 3) | ((nrfs - 1) << 5) | (mrd << 9));
    sdram_ready()
}

/// Bring up the 8 MiB SDRAM on FMC bank 2 with ST's values for this board.
///
/// The SD clock is HCLK/2 = 90 MHz (`SDCLOCK_PERIOD = FMC_SDRAM_CLOCK_PERIOD_2`),
/// which is what every timing figure below is expressed in. ST's HAL splits
/// the fields across the two banks' registers in a way that is easy to get
/// wrong, and which the RM makes mandatory: **SDCLK, RBURST and RPIPE are
/// only honoured in SDCR1, and TRC/TRP only in SDTR1**, even when the device
/// is on bank 2. That is why SDCR1/SDTR1 are written here at all.
///
/// Field values, all from `BSP_SDRAM_Init`:
///
/// | field | ST | encoded |
/// |-------|----|---------|
/// | NC columns | 8 | `0b00` |
/// | NR rows | 12 | `0b01 << 2` |
/// | MWID | 16-bit | `0b01 << 4` |
/// | NB banks | 4 | `1 << 6` |
/// | CAS | 3 | `0b11 << 7` |
/// | SDCLK | 2×HCLK | `0b10 << 10` |
/// | RBURST | disabled | `0` |
/// | RPIPE | 1 clock | `0b01 << 13` |
/// | TMRD/TXSR/TRAS/TRC/TWR/TRP/TRCD | 2/7/4/7/2/2/2 | each `−1`, packed |
pub fn sdram_init() -> SdramRegs {
    // FMC clock, then every SDRAM pin to AF12.
    wr(RCC_AHB3ENR, rd(RCC_AHB3ENR) | RCC_AHB3ENR_FMCEN);
    let _ = rd(RCC_AHB3ENR);
    pins_to_af(SDRAM_PINS.iter().map(|&(p, n)| (p, n, AF_FMC)));

    // SDCR1 carries the bank-independent fields; SDCR2 the device geometry.
    const SDCR1: u32 = (0b10 << 10) | (0b01 << 13); // SDCLK/2, RBURST off, RPIPE 1
    const SDCR2: u32 = (0b01 << 2) | (0b01 << 4) | (1 << 6) | (0b11 << 7);
    wr(FMC_SDCR1, SDCR1);
    wr(FMC_SDCR2, SDCR2);
    // TRC-1 = 6 at bits 15:12, TRP-1 = 1 at bits 23:20 — SDTR1 only.
    const SDTR1: u32 = (6 << 12) | (1 << 20);
    // TMRD-1=1, TXSR-1=6, TRAS-1=3, TWR-1=1, TRCD-1=1.
    const SDTR2: u32 = 1 | (6 << 4) | (3 << 8) | (1 << 16) | (1 << 24);
    wr(FMC_SDTR1, SDTR1);
    wr(FMC_SDTR2, SDTR2);

    // ST's six-step power-up sequence, in ST's order.
    sdram_cmd(0b001, 1, 0); // 1: clock configuration enable
    delay_ms(1); // 2: ≥100 µs of SDRAM self-initialisation (ST waits 1 ms)
    sdram_cmd(0b010, 1, 0); // 3: PALL — precharge all banks
    sdram_cmd(0b011, 4, 0); // 4: 4 auto-refresh cycles
    // 5: load mode register. ST ORs five named fields, three of which are
    //    zero: BURST_LENGTH_1 (0x000) | BURST_TYPE_SEQUENTIAL (0x000) |
    //    CAS_LATENCY_3 (0x030) | OPERATING_MODE_STANDARD (0x000) |
    //    WRITEBURST_MODE_SINGLE (0x200) — i.e. 0x230.
    const MRD: u32 = 0x230;
    sdram_cmd(0b100, 1, MRD);
    // 6: refresh rate. COUNT = (64 ms / 4096 rows) × 90 MHz − 20 = 1386,
    //    which is precisely ST's REFRESH_COUNT for this board and SD clock.
    wr(FMC_SDRTR, 1386 << 1);

    SdramRegs {
        sdcr1: rd(FMC_SDCR1),
        sdcr2: rd(FMC_SDCR2),
        sdtr1: rd(FMC_SDTR1),
        sdtr2: rd(FMC_SDTR2),
        sdrtr: rd(FMC_SDRTR),
        sdsr: rd(FMC_SDSR),
    }
}

// --- SDRAM memory test ------------------------------------------------------

/// What [`sdram_test`] found. A framebuffer in memory that has not been
/// proven is a framebuffer that will produce a confusing picture and an
/// unfalsifiable bug report, so nothing renders until this is all-zero.
pub struct SdramReport {
    /// Walking-1/walking-0 on the 16 data lines at the base address.
    pub data_bus_errors: u32,
    /// Aliasing across the address lines (wrong NR/NC/NB would show here).
    pub addr_bus_errors: u32,
    /// Full-device pattern mismatches.
    pub pattern_errors: u32,
    /// First failing byte offset, and what was expected vs read there.
    pub first_bad_off: u32,
    pub first_bad_want: u32,
    pub first_bad_got: u32,
    pub bytes: u32,
    pub write_cycles: u32,
    pub read_cycles: u32,
}

impl SdramReport {
    pub fn ok(&self) -> bool {
        self.data_bus_errors == 0 && self.addr_bus_errors == 0 && self.pattern_errors == 0
    }
}

#[inline]
fn w16(off: u32, v: u16) {
    // SAFETY: `off` is a byte offset inside the 8 MiB SDRAM aperture that the
    // FMC has just been configured to decode; 2-aligned by construction.
    unsafe { core::ptr::write_volatile((SDRAM_BASE + off) as *mut u16, v) }
}

#[inline]
fn r16(off: u32) -> u16 {
    // SAFETY: as `w16`.
    unsafe { core::ptr::read_volatile((SDRAM_BASE + off) as *const u16) }
}

#[inline]
fn w32(off: u32, v: u32) {
    // SAFETY: as `w16`, 4-aligned by construction.
    unsafe { core::ptr::write_volatile((SDRAM_BASE + off) as *mut u32, v) }
}

#[inline]
fn r32(off: u32) -> u32 {
    // SAFETY: as `w32`.
    unsafe { core::ptr::read_volatile((SDRAM_BASE + off) as *const u32) }
}

/// Address-derived value: a mis-decoded address reads back a DIFFERENT word,
/// so aliasing cannot hide behind a constant fill. Knuth's multiplicative
/// constant, chosen only because it spreads the low address bits over all 32.
#[inline]
fn pattern_for(off: u32) -> u32 {
    off.wrapping_mul(0x9E37_79B1) ^ 0x5A5A_A5A5
}

/// Prove the SDRAM before a single pixel is written into it.
///
/// Three tests, in increasing cost and decreasing likelihood of failure:
///
/// 1. **Data bus** — walking 1 and walking 0 through a single 16-bit word at
///    the base address. Catches a stuck, shorted or unconnected D0..D15.
/// 2. **Address bus** — a pattern at each power-of-two word offset and an
///    anti-pattern probe. Catches a stuck or shorted address line and, just
///    as importantly, catches an FMC configured with the wrong row/column/
///    bank split, which aliases addresses rather than losing them.
/// 3. **Full device** — write all 8 MiB with an address-derived value, then
///    read all 8 MiB back. The write and read passes are timed separately,
///    which also yields the sustained bus bandwidth the verdict needs.
///
/// The bulk pass reports its own cycle counts; those are the ONLY numbers
/// here that describe performance rather than correctness.
pub fn sdram_test() -> SdramReport {
    let mut r = SdramReport {
        data_bus_errors: 0,
        addr_bus_errors: 0,
        pattern_errors: 0,
        first_bad_off: u32::MAX,
        first_bad_want: 0,
        first_bad_got: 0,
        bytes: SDRAM_BYTES,
        write_cycles: 0,
        read_cycles: 0,
    };
    let mut note = |off: u32, want: u32, got: u32| {
        if off < r.first_bad_off {
            r.first_bad_off = off;
            r.first_bad_want = want;
            r.first_bad_got = got;
        }
    };

    // 1. data bus
    for i in 0..16 {
        let p: u16 = 1 << i;
        w16(0, p);
        if r16(0) != p {
            r.data_bus_errors += 1;
            note(0, p as u32, r16(0) as u32);
        }
        let q = !p;
        w16(0, q);
        if r16(0) != q {
            r.data_bus_errors += 1;
            note(0, q as u32, r16(0) as u32);
        }
    }

    // 2. address bus — byte offsets 2, 4, 8, ... < 8 MiB, plus offset 0.
    const PAT: u16 = 0xAAAA;
    const ANTI: u16 = 0x5555;
    let mut off = 2u32;
    while off < SDRAM_BYTES {
        w16(off, PAT);
        off <<= 1;
    }
    // A stuck-high address line makes some offset alias onto 0.
    w16(0, ANTI);
    let mut off = 2u32;
    while off < SDRAM_BYTES {
        if r16(off) != PAT {
            r.addr_bus_errors += 1;
            note(off, PAT as u32, r16(off) as u32);
        }
        off <<= 1;
    }
    // Shorted address lines: perturb one offset at a time and check that
    // nothing else moved.
    w16(0, PAT);
    let mut t = 2u32;
    while t < SDRAM_BYTES {
        w16(t, ANTI);
        if r16(0) != PAT {
            r.addr_bus_errors += 1;
            note(0, PAT as u32, r16(0) as u32);
        }
        let mut o = 2u32;
        while o < SDRAM_BYTES {
            if o != t && r16(o) != PAT {
                r.addr_bus_errors += 1;
                note(o, PAT as u32, r16(o) as u32);
            }
            o <<= 1;
        }
        w16(t, PAT);
        t <<= 1;
    }

    // 3. full device, 32 bits at a time.
    let t0 = cyc();
    let mut off = 0u32;
    while off < SDRAM_BYTES {
        w32(off, pattern_for(off));
        off += 4;
    }
    r.write_cycles = cyc().wrapping_sub(t0);

    let t1 = cyc();
    let mut off = 0u32;
    let mut bad = 0u32;
    while off < SDRAM_BYTES {
        if r32(off) != pattern_for(off) {
            bad += 1;
        }
        off += 4;
    }
    r.read_cycles = cyc().wrapping_sub(t1);
    r.pattern_errors = bad;
    if bad > 0 {
        // Re-walk only on failure, to name the first bad address without
        // paying for a branch-heavy inner loop in the timed pass above.
        let mut off = 0u32;
        while off < SDRAM_BYTES {
            let got = r32(off);
            if got != pattern_for(off) {
                note(off, pattern_for(off), got);
                break;
            }
            off += 4;
        }
    }
    r
}

// --- PLLSAI + LTDC ----------------------------------------------------------

/// Bring up the LTDC pixel clock. ST's dividers, and ST's arithmetic:
///
/// * PLLSAI VCO input  = PLL source / PLLM = 1 MHz (PLLM is shared with the
///   main PLL, which [`crate::m4::clock_init_180mhz`] set to 8 for the 8 MHz
///   crystal — or 16 on the HSI fallback, which also lands on 1 MHz, so this
///   divider chain is correct either way).
/// * PLLSAI VCO output = 1 MHz × PLLSAIN(192) = 192 MHz.
/// * PLLLCDCLK         = 192 / PLLSAIR(4)     = 48 MHz.
/// * LTDC pixel clock  = 48 / PLLSAIDIVR(8)   = **6 MHz**.
///
/// At 6 MHz the 280 × 328 total frame is 91,840 pixel clocks → 65.3 Hz, the
/// refresh the ILI9341's RGB interface is specified around.
///
/// PLLSAIQ is left at its reset value 4 rather than ST's 0: the SAI clock is
/// unused here, but 0 is outside the field's documented 2..15 range and
/// writing an out-of-range divider to a PLL that must lock is a needless
/// risk.
pub fn pllsai_init() -> bool {
    // The PLLSAI must be off while its configuration register is written.
    wr(RCC_CR, rd(RCC_CR) & !RCC_CR_PLLSAION);
    if !wait_ms(2, || rd(RCC_CR) & RCC_CR_PLLSAIRDY == 0) {
        return false;
    }
    wr(RCC_PLLSAICFGR, (192 << 6) | (4 << 24) | (4 << 28));
    // DCKCFGR PLLSAIDIVR[17:16]: 0b10 = /8.
    wr(
        RCC_DCKCFGR,
        (rd(RCC_DCKCFGR) & !(0b11 << 16)) | (0b10 << 16),
    );
    wr(RCC_CR, rd(RCC_CR) | RCC_CR_PLLSAION);
    // Budget 2 ms: the F429 datasheet gives PLL lock time as ~200 µs typical,
    // so this is an order of magnitude of headroom and still fails fast.
    wait_ms(2, || rd(RCC_CR) & RCC_CR_PLLSAIRDY != 0)
}

/// The LTDC pixel clock in Hz as the SILICON is configured — recomputed from
/// RCC_PLLCFGR/RCC_PLLSAICFGR/RCC_DCKCFGR rather than asserted, so a partly
/// failed clock set-up reports the frequency it reached.
pub fn pixclk_hz() -> u32 {
    let pllcfgr = rd(RCC_PLLCFGR);
    let saicfgr = rd(RCC_PLLSAICFGR);
    let dck = rd(RCC_DCKCFGR);
    let src = if pllcfgr & (1 << 22) != 0 {
        8_000_000
    } else {
        16_000_000
    };
    let m = pllcfgr & 0x3F;
    let n = (saicfgr >> 6) & 0x1FF;
    let r = (saicfgr >> 28) & 0x7;
    let divr = 2u32 << ((dck >> 16) & 0b11); // 00->2, 01->4, 10->8, 11->16
    if m == 0 || r == 0 {
        return 0;
    }
    src / m * n / r / divr
}

/// Snapshot of the LTDC, for the runner to assert against.
pub struct LtdcRegs {
    pub sscr: u32,
    pub bpcr: u32,
    pub awcr: u32,
    pub twcr: u32,
    pub gcr: u32,
    pub l1_cr: u32,
    pub l1_pfcr: u32,
    pub l1_cfbar: u32,
    pub l1_cfblr: u32,
    pub l1_cfblnr: u32,
}

/// ST's LTDC timing for the ILI9341 on this board, from `BSP_LCD_Init`, with
/// ST's own derivation of every number preserved:
///
/// ```text
/// HSYNC = 10 (9+1)      VSYNC = 2 (1+1)
/// HBP   = 20 (29-10+1)  VBP   = 2 (3-2+1)
/// ActiveW = 240         ActiveH = 320
/// HFP   = 10            VFP   = 4
/// total = 280 x 328
/// ```
const HSYNC: u32 = 9; // ILI9341_HSYNC
const VSYNC: u32 = 1; // ILI9341_VSYNC
const AHBP: u32 = 29; // ILI9341_HBP  (accumulated)
const AVBP: u32 = 3; // ILI9341_VBP  (accumulated)
const AAW: u32 = 269;
const AAH: u32 = 323;
const TOTALW: u32 = 279;
const TOTALH: u32 = 327;
/// Total pixel clocks per frame: (TOTALW+1) × (TOTALH+1).
const CLOCKS_PER_FRAME: u32 = (TOTALW + 1) * (TOTALH + 1);

/// Configure and start LTDC scanning `fb` as one RGB565 layer.
///
/// Order follows ST's: timings and the controller are enabled FIRST (so the
/// panel sees valid HSYNC/VSYNC/DE while it is being configured), the layer
/// is configured and enabled after. The window positions are ST's HAL
/// arithmetic, not a guess: horizontal start = `X0 + AHBP + 1`, stop =
/// `X1 + AHBP`; vertical the same against `AVBP`. The line-length field is
/// `bytes_per_line + 3` — a genuine `+3` in the silicon's definition, not a
/// typo.
pub fn ltdc_init(fb: u32) -> LtdcRegs {
    wr(RCC_APB2ENR, rd(RCC_APB2ENR) | RCC_APB2ENR_LTDCEN);
    let _ = rd(RCC_APB2ENR);
    pins_to_af(LTDC_PINS.iter().copied());

    wr(LTDC_SSCR, (HSYNC << 16) | VSYNC);
    wr(LTDC_BPCR, (AHBP << 16) | AVBP);
    wr(LTDC_AWCR, (AAW << 16) | AAH);
    wr(LTDC_TWCR, (TOTALW << 16) | TOTALH);
    // All four polarities are ST's active-low / input-pixel-clock == 0.
    wr(LTDC_GCR, rd(LTDC_GCR) & !LTDC_GCR_POL_MASK);
    wr(LTDC_BCCR, 0x0000_0000); // black behind the layer
    // Dithering is deliberately NOT enabled. The panel takes 6 bits per
    // channel and RGB565 supplies 5/6/5, so LTDC's dither would put pixel
    // values on the glass that the renderer never produced — the same reason
    // `lcd::rgb565` truncates rather than dithers.
    wr(LTDC_GCR, rd(LTDC_GCR) | LTDC_GCR_LTDCEN);

    // Layer 1: the whole active area, RGB565, opaque.
    wr(L1_WHPCR, ((PANEL_W + AHBP) << 16) | (AHBP + 1));
    wr(L1_WVPCR, ((PANEL_H + AVBP) << 16) | (AVBP + 1));
    wr(L1_PFCR, 2); // 0=ARGB8888 1=RGB888 2=RGB565
    wr(L1_CACR, 0xFF);
    wr(L1_DCCR, 0);
    // BF1 = PAxCA (0b110 << 8), BF2 = 1-PAxCA (0b111) — ST's choice. With one
    // opaque layer and constant alpha 255 this reduces to "the layer wins".
    wr(L1_BFCR, (0b110 << 8) | 0b111);
    wr(L1_CFBAR, fb);
    wr(L1_CFBLR, ((PANEL_W * 2) << 16) | (PANEL_W * 2 + 3));
    wr(L1_CFBLNR, PANEL_H);
    wr(L1_CR, rd(L1_CR) | LTDC_LX_CR_LEN);
    wr(LTDC_SRCR, LTDC_SRCR_IMR); // shadow registers -> active, now

    LtdcRegs {
        sscr: rd(LTDC_SSCR),
        bpcr: rd(LTDC_BPCR),
        awcr: rd(LTDC_AWCR),
        twcr: rd(LTDC_TWCR),
        gcr: rd(LTDC_GCR),
        l1_cr: rd(L1_CR),
        l1_pfcr: rd(L1_PFCR),
        l1_cfbar: rd(L1_CFBAR),
        l1_cfblr: rd(L1_CFBLR),
        l1_cfblnr: rd(L1_CFBLNR),
    }
}

/// MEASURE the refresh rate off the hardware instead of computing it.
///
/// `LTDC_CPSR` reports the pixel the controller is currently emitting
/// (CXPOS[31:16], CYPOS[15:0]). Watching CYPOS wrap from large to small twice
/// brackets exactly one frame; the DWT delta between the two wraps is the
/// frame period in real core cycles. If LTDC is not scanning at all, CYPOS
/// never moves, no wrap is ever seen and this returns `None` — which is the
/// point: it can fail, and a number it returns is therefore evidence.
///
/// Budget: 100 ms per wrap. The nominal frame period is 15.3 ms, so this is
/// ~6 frames of slack and still bails in a tenth of a second if the pixel
/// clock is dead.
pub fn measure_frame_period() -> Option<u32> {
    fn cy() -> u32 {
        rd(LTDC_CPSR) & 0xFFFF
    }
    fn wrap() -> bool {
        let mut prev = cy();
        wait_ms(100, || {
            let now = cy();
            let wrapped = now < prev;
            prev = now;
            wrapped
        })
    }
    if !wrap() {
        return None;
    }
    let t0 = cyc();
    if !wrap() {
        return None;
    }
    Some(cyc().wrapping_sub(t0))
}

/// LTDC's own account of what it is driving right now: `CDSR` bits
/// VDES(0)/HDES(1)/VSYNCS(2)/HSYNCS(3), and the current scan position.
pub fn ltdc_status() -> (u32, u32) {
    (rd(LTDC_CDSR), rd(LTDC_CPSR))
}

// --- framebuffer ------------------------------------------------------------

/// Fill the whole framebuffer with one RGB565 colour.
pub fn fb_fill(color: u16) {
    let two = (color as u32) | ((color as u32) << 16);
    let mut off = FB_BASE;
    while off < FB_END {
        // SAFETY: inside the framebuffer, which is inside the SDRAM aperture
        // proven by `sdram_test`; 4-aligned because FB_BASE is and the step
        // is 4.
        unsafe { core::ptr::write_volatile(off as *mut u32, two) };
        off += 4;
    }
}

/// RGB888 rect → RGB565 straight into the scan-out framebuffer.
///
/// No intermediate buffer: `band` is the exact bytes vyr-core just rendered.
/// Two pixels are packed into one 32-bit store whenever the destination run
/// is 4-aligned and even — which it always is for a full-width band, and for
/// any even-x/even-width dirty rect — halving the number of AHB transactions
/// the 16-bit SDRAM has to absorb. `write_volatile` keeps every store in the
/// program, so the measured cost is the cost actually paid.
///
/// Returns nothing and hashes nothing: the determinism proof is folded from
/// the RGB888 bytes BEFORE this runs, so the hash can never be an artefact of
/// the display path.
fn blit(x: u32, y: u32, w: u32, h: u32, band: &[u8]) {
    let stride = (w * 3) as usize;
    for row in 0..h {
        let src = &band[row as usize * stride..][..stride];
        let dst = FB_BASE as usize + ((((y + row) * PANEL_W) + x) as usize) * 2;
        // `chunks_exact` rather than indexed slicing on purpose: it hands the
        // body a slice of statically known length, so the per-pixel-pair
        // bounds checks an `src[i + 5]` form emits go away. MEASURED on this
        // board, 240x320 Draft, mean of 8 warmed frames: 2,854,717 -> 2,266,885
        // cycles, i.e. 37.2 -> 29.5 cycles per pixel, a 21 % saving that was
        // pure bounds checking and would otherwise have been charged to the
        // SDRAM.
        if dst.is_multiple_of(4) && w.is_multiple_of(2) {
            let mut p = dst as *mut u32;
            for px in src.chunks_exact(6) {
                let a = rgb565(px[0], px[1], px[2]) as u32;
                let b = rgb565(px[3], px[4], px[5]) as u32;
                // Little-endian: the first pixel occupies the low half-word,
                // which is the lower framebuffer address. Same bytes as two
                // u16 stores, one AHB transaction instead of two.
                // SAFETY: `dst` is 4-aligned and the run stays inside the
                // framebuffer (callers pass rects clipped to the panel).
                unsafe {
                    p.write_volatile(a | (b << 16));
                    p = p.add(1);
                }
            }
        } else {
            let mut p = dst as *mut u16;
            for px in src.chunks_exact(3) {
                // SAFETY: 2-aligned, inside the framebuffer.
                unsafe {
                    p.write_volatile(rgb565(px[0], px[1], px[2]));
                    p = p.add(1);
                }
            }
        }
    }
}

/// Fold the framebuffer back out of SDRAM.
///
/// This is the difference between "the firmware executed some stores" and
/// "the memory the display hardware reads contains those bytes". Compared
/// against a hash folded from the same RGB565 values at write time, a match
/// means the SDRAM round-tripped the whole frame.
fn fb_readback_hash() -> u64 {
    let mut h = lcd::FNV_OFFSET;
    let mut off = FB_BASE;
    while off < FB_END {
        // SAFETY: inside the framebuffer.
        let v = unsafe { core::ptr::read_volatile(off as *const u16) };
        h = fnv1a_fold(h, &v.to_le_bytes());
        off += 2;
    }
    h
}

// --- the render passes ------------------------------------------------------

/// Cost of one update, split so that drawing and displaying are never
/// conflated. All cycle counts are DWT_CYCCNT deltas at the achieved core
/// clock.
pub struct Cost {
    pub render_cycles: u32,
    pub blit_cycles: u32,
}

impl Cost {
    fn total(&self) -> u32 {
        self.render_cycles + self.blit_cycles
    }
    /// Frames per second the CPU can sustain, ignoring the refresh ceiling.
    fn fps(&self, hz: u32) -> u32 {
        hz.checked_div(self.total()).unwrap_or(0)
    }
}

/// Render `rect` (in scene coordinates) band by band and blit each band into
/// the framebuffer, returning the frame hash of the RGB888 bytes.
///
/// The band loop is the SAME shape as [`crate::workload::run`] and
/// `crate::lcd::show_panel_scene`: a caller-owned buffer, an arbitrary
/// sub-rect, an explicit stride — invariant I1, and the only render entry
/// point.
///
/// # Single buffered, deliberately
///
/// The bands land in the framebuffer LTDC is already scanning, so a partially
/// updated frame can be on the glass for one refresh: **tearing**. That is the
/// known and accepted cost of single buffering here, and it is the SAFE choice.
///
/// SDRAM has room for a second buffer — 8 MiB against a 153,600 B frame — and
/// adding one would remove the tearing. Do not do it casually. `docs/plan.md`
/// §"Presentation modes" records the trap: with two full framebuffers and a
/// pointer swap on vsync, **the back buffer is TWO FRAMES STALE**, so a
/// dirty-rect update must repaint the union of THIS frame's and the PREVIOUS
/// frame's dirty regions (or copy forward) — the TouchGFX SMOC lesson.
/// Repainting only the current frame's dirty rects into an alternating back
/// buffer leaves pixels from two frames ago on the glass.
///
/// What makes that trap genuinely dangerous HERE: it is invisible to every
/// check this leg performs. The frame hash is folded from the RGB888 the
/// renderer produced, and the `incremental == full` equivalence tests are all
/// single-buffer — so the hashes would keep matching while the display showed
/// trails. It manifests only as something a human sees. If a second buffer is
/// ever added, the union-of-two-frames rule must be implemented explicitly and
/// said out loud, and per the plan that bookkeeping belongs in this
/// flush/presentation layer, never in the painter and never in vyr-core.
#[allow(clippy::too_many_arguments)]
fn render_rect(
    req: &vyr_core::ir::Request,
    fonts: &mut Fonts,
    assets: &Assets,
    quality: Quality,
    band_buf: &mut [u8],
    rect: Rect,
    cost: &mut Cost,
    pixels: &mut u64,
) -> Result<u64, RenderError> {
    render_rect_from(
        req,
        fonts,
        assets,
        quality,
        band_buf,
        rect,
        cost,
        pixels,
        lcd::FNV_OFFSET,
    )
}

/// [`render_rect`] continuing an existing fold, so a MULTI-RECT update folds to
/// one hash. `#45` (the presentation-mode comparison) needs this to hash a
/// `dirty_rects` set the same way the direct path hashes it back out of SDRAM;
/// `render_rect` is this function with a fresh fold, and nothing else.
#[allow(clippy::too_many_arguments)]
pub(crate) fn render_rect_from(
    req: &vyr_core::ir::Request,
    fonts: &mut Fonts,
    assets: &Assets,
    quality: Quality,
    band_buf: &mut [u8],
    rect: Rect,
    cost: &mut Cost,
    pixels: &mut u64,
    hash_in: u64,
) -> Result<u64, RenderError> {
    let stride = (rect.w * 3) as usize;
    let mut hash = hash_in;
    let mut y = 0u32;
    while y < rect.h {
        let h = PANEL_BAND_H.min(rect.h - y);
        let band = Rect {
            x: rect.x,
            y: rect.y + y as i32,
            w: rect.w,
            h,
        };
        let len = stride * h as usize;

        let t0 = cyc();
        let stats =
            req.render_with_quality(fonts, assets, band, &mut band_buf[..len], stride, quality)?;
        cost.render_cycles = cost.render_cycles.wrapping_add(cyc().wrapping_sub(t0));

        *pixels += stats.pixels_written;
        // The determinism fold is over the RGB888 the renderer produced,
        // before any display-format conversion touches it.
        hash = fnv1a_fold(hash, &band_buf[..len]);

        let t1 = cyc();
        blit(
            rect.x as u32,
            rect.y as u32 + y,
            rect.w,
            h,
            &band_buf[..len],
        );
        cost.blit_cycles = cost.blit_cycles.wrapping_add(cyc().wrapping_sub(t1));
        y += h;
    }
    Ok(hash)
}

/// The dirty rect this leg prices: the `vy_lcd` readout ("1480") plus its
/// unit label, snapped out to even x/width so the packed 32-bit blit applies.
/// A real dashboard redraws exactly this kind of region many times a second
/// while the rest of the frame is static, which is the case LTDC has to win.
pub const DIRTY: Rect = Rect {
    x: 136,
    y: 96,
    w: 96,
    h: 32,
};

/// How many warmed repetitions each cost figure averages over. Small because
/// every repetition is deterministic — the renderer has no adaptive state, no
/// clock and no allocator randomness — so this is averaging away DWT sampling
/// granularity and refresh-phase bus contention, not run-to-run variance.
const REPS: u32 = 8;

// --- entry point ------------------------------------------------------------

/// Why the LTDC leg stopped.
///
/// Deliberately NOT folded into [`RenderError`]: "the external SDRAM does not
/// read back what it was written" is a hardware fault, and calling it a render
/// error would put a hardware diagnosis behind a renderer-shaped label. Honest
/// failure means the error names the thing that actually failed.
#[derive(Debug)]
pub enum LtdcError {
    /// [`sdram_test`] found a fault; no framebuffer was placed.
    Sdram,
    /// vyr-core refused to render. The payload is read via the derived
    /// `Debug` in `m4::main`'s failure line; rustc's dead-code pass ignores
    /// derived impls, hence the allow.
    Render(#[allow(dead_code)] RenderError),
}

impl From<RenderError> for LtdcError {
    fn from(e: RenderError) -> Self {
        LtdcError::Render(e)
    }
}

/// Bring up SDRAM, LTDC and the panel, draw [`PANEL_IR`] into the scan-out
/// framebuffer, and price it.
///
/// Ordering is the point, exactly as in [`crate::lcd::show_panel_scene`]:
///
/// 1. SDRAM up and PROVEN before anything is stored in it.
/// 2. Framebuffer cleared to a flat colour, LTDC started, panel switched to
///    RGB-interface mode. If the scene render later fails, a flat blue panel
///    still says "SDRAM, PLLSAI, LTDC, the 28 AF pins and the ILI9341's RGB
///    mode all work" and separates a display fault from a renderer fault by
///    looking.
/// 3. The scene, banded, with the per-band determinism fold.
/// 4. A framebuffer read-back hash — proof the memory holds what was written.
/// 5. Cost measurements in their own DWT window.
/// 6. The controller's own read-back, last, changing nothing on screen.
///
/// The 480×270 measurement's window in [`crate::workload::run`] is opened
/// later and is untouched by any of this.
pub fn show_scene(
    emit: &mut dyn FnMut(&str),
    band_buf: &mut [u8],
    quality: Quality,
) -> Result<u64, LtdcError> {
    let hz = crate::m4::sysclk_hz();

    // --- 1. SDRAM ----------------------------------------------------------
    let sr = sdram_init();
    emit(&alloc::format!(
        "INFO  [vyr-size] ltdc: FMC SDRAM bank2 @{SDRAM_BASE:#010x} {} MiB (16-bit, \
         CAS3, SDCLK=HCLK/2=90MHz, refresh 1386): SDCR1={:#010x} SDCR2={:#010x} \
         SDTR1={:#010x} SDTR2={:#010x} SDRTR={:#010x} SDSR={:#010x}",
        SDRAM_BYTES / (1024 * 1024),
        sr.sdcr1,
        sr.sdcr2,
        sr.sdtr1,
        sr.sdtr2,
        sr.sdrtr,
        sr.sdsr
    ));

    let t = sdram_test();
    let mbps = |cycles: u32| -> u32 {
        if cycles == 0 {
            return 0;
        }
        // bytes/cycle * hz / 1e6, ordered to stay in u32/u64 range.
        ((SDRAM_BYTES as u64 * hz as u64) / (cycles as u64 * 1_000_000)) as u32
    };
    emit(&alloc::format!(
        "INFO  [vyr-size] ltdc: sdram test data_bus_errors={} addr_bus_errors={} \
         pattern_errors={} bytes={} first_bad_off={:#x} want={:#010x} got={:#010x} \
         write={} c ({} MB/s) read={} c ({} MB/s) ok={}",
        t.data_bus_errors,
        t.addr_bus_errors,
        t.pattern_errors,
        t.bytes,
        t.first_bad_off,
        t.first_bad_want,
        t.first_bad_got,
        t.write_cycles,
        mbps(t.write_cycles),
        t.read_cycles,
        mbps(t.read_cycles),
        t.ok()
    ));
    if !t.ok() {
        // Honest failure: a framebuffer in memory that does not hold what it
        // was given produces a picture nobody can reason about. Stop before
        // pixels rather than after.
        emit(
            "ERROR [vyr-size] ltdc: SDRAM memory test FAILED — refusing to place a \
             framebuffer in memory that does not read back what it was written",
        );
        return Err(LtdcError::Sdram);
    }

    // --- 2. clocks, scan-out, panel ----------------------------------------
    fb_fill(rgb565(0x00, 0x10, 0x30)); // the same dark blue the SPI leg uses
    let sai = pllsai_init();
    emit(&alloc::format!(
        "INFO  [vyr-size] ltdc: PLLSAI locked={sai} (N=192 R=4 DIVR=8) pixclk_hz={} \
         RCC_CR={:#010x} RCC_PLLSAICFGR={:#010x} RCC_DCKCFGR={:#010x}",
        pixclk_hz(),
        rd(RCC_CR),
        rd(RCC_PLLSAICFGR),
        rd(RCC_DCKCFGR)
    ));

    // SPI5 is the command channel for BOTH display paths; only the ILI9341's
    // 0xF6/0xB0 differ. This is the same driver the `lcd` leg uses.
    lcd::spi_init();
    let lr = ltdc_init(FB_BASE);
    lcd::panel_init(true); // RGB interface: LTDC owns the pixels
    // The layer configuration was written before the panel accepted RGB mode;
    // re-arm the shadow registers so the layer is certainly live afterwards.
    wr(LTDC_SRCR, LTDC_SRCR_IMR);

    emit(&alloc::format!(
        "INFO  [vyr-size] ltdc: LTDC {PANEL_W}x{PANEL_H} RGB565 fb={FB_BASE:#010x} \
         ({FB_BYTES} B): SSCR={:#010x} BPCR={:#010x} AWCR={:#010x} TWCR={:#010x} \
         GCR={:#010x} L1CR={:#010x} L1PFCR={:#010x} L1CFBAR={:#010x} \
         L1CFBLR={:#010x} L1CFBLNR={:#010x}",
        lr.sscr,
        lr.bpcr,
        lr.awcr,
        lr.twcr,
        lr.gcr,
        lr.l1_cr,
        lr.l1_pfcr,
        lr.l1_cfbar,
        lr.l1_cfblr,
        lr.l1_cfblnr
    ));

    // Is it actually scanning? Measured, not assumed.
    let period = measure_frame_period();
    let (cdsr, cpsr) = ltdc_status();
    match period {
        Some(p) if p > 0 => emit(&alloc::format!(
            "INFO  [vyr-size] ltdc: scan-out LIVE — frame period {p} core cycles \
             = {} mHz refresh (nominal {CLOCKS_PER_FRAME} pixclk/frame at {} Hz \
             = {} mHz), CDSR={cdsr:#010x} (vde={} hde={} vsync={} hsync={}) \
             CPSR={cpsr:#010x}",
            (hz as u64 * 1000 / p as u64),
            pixclk_hz(),
            (pixclk_hz() as u64 * 1000 / CLOCKS_PER_FRAME as u64),
            cdsr & 1,
            (cdsr >> 1) & 1,
            (cdsr >> 2) & 1,
            (cdsr >> 3) & 1
        )),
        _ => emit(&alloc::format!(
            "ERROR [vyr-size] ltdc: scan-out NOT running — LTDC_CPSR never wrapped \
             within 100 ms. CDSR={cdsr:#010x} CPSR={cpsr:#010x}. Nothing will be \
             on the panel."
        )),
    }
    emit(
        "INFO  [vyr-size] ltdc: framebuffer cleared to dark blue — if the board is \
         showing a flat blue screen and nothing else, SDRAM + PLLSAI + LTDC + the \
         22 AF pins + the ILI9341's RGB mode all work and the scene render failed",
    );

    // --- 3. the scene ------------------------------------------------------
    let mut fonts = Fonts::new();
    fonts.register("roboto", opaque(crate::workload::SUBSET_FONT).to_vec())?;
    let mut assets = Assets::new();
    assets.register(
        vyr_core::demo::IMAGE_ASSET,
        vyr_core::RgbaImage::new(24, 24, opaque(crate::workload::CHECKER).to_vec())?,
    )?;
    let req = vyr_core::ir::Request::parse(opaque(PANEL_IR))?;

    let full = Rect {
        x: 0,
        y: 0,
        w: PANEL_W,
        h: PANEL_H,
    };
    let mut warm = Cost {
        render_cycles: 0,
        blit_cycles: 0,
    };
    let mut pixels = 0u64;
    let hash = render_rect(
        &req,
        &mut fonts,
        &assets,
        quality,
        band_buf,
        full,
        &mut warm,
        &mut pixels,
    )?;
    emit(&alloc::format!(
        "INFO  [vyr-size] ltdc: panel frame fnv1a={hash:#018x} bands={} \
         pixels_written={pixels} quality={quality:?}",
        PANEL_H.div_ceil(PANEL_BAND_H)
    ));

    // --- 4. does the memory hold it? ---------------------------------------
    // Fold the RGB565 the blit should have produced, straight from the RGB888
    // by re-rendering nothing — instead read the framebuffer back and report
    // its hash. Stable across runs == the SDRAM round-tripped the frame.
    let rb = fb_readback_hash();
    emit(&alloc::format!(
        "INFO  [vyr-size] ltdc: framebuffer readback fnv1a={rb:#018x} over \
         {FB_BYTES} B of SDRAM"
    ));

    // --- 5. cost -----------------------------------------------------------
    let mut fullc = Cost {
        render_cycles: 0,
        blit_cycles: 0,
    };
    let mut px = 0u64;
    let mut h2 = 0u64;
    for _ in 0..REPS {
        h2 = render_rect(
            &req, &mut fonts, &assets, quality, band_buf, full, &mut fullc, &mut px,
        )?;
    }
    fullc.render_cycles /= REPS;
    fullc.blit_cycles /= REPS;
    let stable = h2 == hash;
    emit(&alloc::format!(
        "INFO  [vyr-size] ltdc: full-frame cost render={} c blit={} c total={} c \
         over {REPS} warmed frames => {} fps cpu-bound (hash stable={stable})",
        fullc.render_cycles,
        fullc.blit_cycles,
        fullc.total(),
        fullc.fps(hz)
    ));

    let mut dirtyc = Cost {
        render_cycles: 0,
        blit_cycles: 0,
    };
    let mut dpx = 0u64;
    let mut dhash = 0u64;
    for _ in 0..REPS {
        dhash = render_rect(
            &req,
            &mut fonts,
            &assets,
            quality,
            band_buf,
            DIRTY,
            &mut dirtyc,
            &mut dpx,
        )?;
    }
    dirtyc.render_cycles /= REPS;
    dirtyc.blit_cycles /= REPS;
    emit(&alloc::format!(
        "INFO  [vyr-size] ltdc: dirty-rect ({},{} {}x{}) cost render={} c blit={} c \
         total={} c => {} fps cpu-bound, fnv1a={dhash:#018x}",
        DIRTY.x,
        DIRTY.y,
        DIRTY.w,
        DIRTY.h,
        dirtyc.render_cycles,
        dirtyc.blit_cycles,
        dirtyc.total(),
        dirtyc.fps(hz)
    ));

    let refresh_mhz = period.map_or(0, |p| (hz as u64 * 1000 / p as u64) as u32);
    emit(&alloc::format!(
        "INFO  [vyr-size] ltdc: achievable = min(cpu {} fps, refresh {} mHz); \
         scan-out itself costs the CPU 0 cycles and {} B/s of SDRAM read bandwidth",
        fullc.fps(hz),
        refresh_mhz,
        (FB_BYTES as u64 * refresh_mhz as u64 / 1000)
    ));
    emit(
        "ALERT [vyr-size] ltdc: frame is in SDRAM and LTDC is scanning it out over \
         the 16-bit parallel RGB bus — the image persists after the target halts \
         only while the core clock and PLLSAI keep running",
    );

    // --- 6. what the controller says ---------------------------------------
    lcd::panel_probe(emit);

    // --- 7. `--features present` (#45): the OTHER presentation mode ---------
    // Everything above priced ONE topology (partial + flush). This prices
    // rendering straight into the scan-out framebuffer against it, same scene,
    // same fonts, same assets, same clock — reusing the registries built here
    // rather than building a second set, because a second glyph cache would not
    // fit the 122,880 B arena. It runs LAST, so nothing it does to the LTDC
    // layer configuration can affect any number above.
    #[cfg(feature = "present")]
    crate::present::compare(emit, band_buf, &req, &mut fonts, &assets, hz)?;

    Ok(hash)
}

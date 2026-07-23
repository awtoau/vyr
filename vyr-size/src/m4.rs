//! The run-qemu M-profile runtime (#9 dynamic half): vector table, crt0,
//! semihosting I/O, and a COUNTED real heap. Everything the workload needs
//! to actually BOOT and report under `qemu-system-arm -machine
//! netduinoplus2` — measurement scaffolding only, never shipped renderer
//! code (`unsafe` is allowed in vyr-size alone; vyr-core stays
//! `forbid(unsafe_code)`).
//!
//! Boot (ARMv7-M architectural contract): the core reads word 0 of the
//! vector table at address 0 as the initial SP and word 1 as the reset
//! handler. qemu's stm32f405 SoC aliases flash (LMA 0x08000000) at 0x0, so
//! the `.vectors` section linked first into FLASH (link-qemu.ld) IS the
//! boot table. `reset` then does crt0 — copy `.data` LMA→VMA, zero `.bss`
//! — initializes the heap, and runs the workload.
//!
//! Output is semihosting SYS_WRITE0 (qemu `-semihosting-config
//! enable=on,target=native`); exit is SYS_EXIT, which terminates qemu with
//! rc 0/1 — the runner script's pass/fail. SYS_CLOCK gives centiseconds of
//! VIRTUAL time: under `-icount shift=0,sleep=off` qemu advances the
//! virtual clock exactly 1 ns per guest instruction, so SYS_CLOCK deltas
//! ARE deterministic instruction counts (×10⁷ insns per cs).
//!
//! With `--features board` the SAME runtime targets REAL silicon — an
//! STM32F429I-DISC1 flashed and run over an ST-LINK/V2-1 (`probe-rs run`,
//! which services the very same semihosting calls). Two things change:
//! [`clock_init_180mhz`] brings the part up to its rated clock with
//! production flash settings, and the timer becomes DWT_CYCCNT — real CPU
//! cycles rather than a host-wall-time proxy. Driver: `scripts/board-run.py`;
//! the halted-target register truth behind the clock choices lives in
//! `scripts/board-diag.py`.

use core::alloc::{GlobalAlloc, Layout};
use core::cell::UnsafeCell;
use core::ptr::NonNull;
use core::sync::atomic::{AtomicUsize, Ordering};

use linked_list_allocator::Heap;

/// netduinoplus2 (STM32F405) in qemu ≥ 8 models the CORRECTED F405 layout:
/// 128 KiB SRAM @ 0x20000000 + 64 KiB CCM @ 0x10000000 (measured: writes
/// just below 0x20030000 BusFault — there is no contiguous 192 KiB; the
/// F427 this emulates has 192 KiB SRAM + the same CCM, so this budget is
/// STRICTLY TIGHTER). Placement is the classic F4 discipline: stack + band
/// buffer in CCM (CPU-only, no DMA contact), heap arena in SRAM. Initial SP
/// = top of CCM, stack growing down toward the band buffer (link-qemu.ld
/// asserts ≥ 16 KiB headroom at link time).
pub const STACK_TOP: u32 = 0x1001_0000;

/// Heap arena: 120 KiB of `.bss` (SRAM is 128 KiB). The workload's measured
/// host peak is ~113 KB once the band buffer lives in CCM (subset font copy
/// ~8 KB + parsed IR + glyph cache + 63,488 B gutter pixmap + transients);
/// 120 KiB leaves first-fit fragmentation room and still fits SRAM with
/// ~8 KiB for other statics.
const ARENA_BYTES: usize = 120 * 1024;

/// The reused 480×16 RGB888 band buffer — a CCM resident (NOLOAD: crt0
/// never touches CCM; the renderer fully writes every band byte before the
/// hash reads it). On the F427 model this is exactly the buffer you would
/// NOT hand to DMA (CCM has no DMA path) — the flush-out copy is the
/// display driver's job, outside this measurement.
#[unsafe(link_section = ".ccm")]
static BAND_BUF: Cell8<{ crate::workload::BAND_BYTES }> =
    Cell8(UnsafeCell::new([0u8; crate::workload::BAND_BYTES]));

/// 8-aligned UnsafeCell byte array with a Sync story: single-threaded
/// target, no interrupts, each cell handed out exactly once.
#[repr(C, align(8))]
struct Cell8<const N: usize>(UnsafeCell<[u8; N]>);

// SAFETY: single-threaded target, no interrupts are ever enabled.
unsafe impl<const N: usize> Sync for Cell8<N> {}

/// The heap arena (SRAM `.bss`), handed to the allocator once at boot.
static ARENA: Cell8<ARENA_BYTES> = Cell8(UnsafeCell::new([0u8; ARENA_BYTES]));

/// Live/peak counters in the SAME semantics as vyr-cli's CountingAlloc
/// (Σ layout.size() of live allocations, high-water mark) so the table's
/// columns compare like for like.
static HEAP_LIVE: AtomicUsize = AtomicUsize::new(0);
static HEAP_PEAK: AtomicUsize = AtomicUsize::new(0);

/// A real alloc/dealloc heap (the bump arena of the size vehicle cannot run
/// a banded frame: 17 gutter pixmaps would never fit any F4-class SRAM
/// without freeing) wrapped with the counters.
struct CountedHeap(UnsafeCell<Heap>);

// SAFETY: single-threaded, no heap-touching interrupts — the UnsafeCell is
// never aliased concurrently.
unsafe impl Sync for CountedHeap {}

#[global_allocator]
static HEAP: CountedHeap = CountedHeap(UnsafeCell::new(Heap::empty()));

unsafe impl GlobalAlloc for CountedHeap {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        // SAFETY: sole accessor (single-threaded, see Sync impl above).
        let heap = unsafe { &mut *self.0.get() };
        match heap.allocate_first_fit(layout) {
            Ok(p) => {
                let live = HEAP_LIVE.fetch_add(layout.size(), Ordering::Relaxed) + layout.size();
                HEAP_PEAK.fetch_max(live, Ordering::Relaxed);
                p.as_ptr()
            }
            // Exhaustion → null → alloc error → panic handler below prints
            // and exits 1 (honest failure, never a silent wrap).
            Err(()) => core::ptr::null_mut(),
        }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        // SAFETY: ptr came from allocate_first_fit with this layout; sole
        // accessor as above.
        unsafe {
            (*self.0.get()).deallocate(NonNull::new_unchecked(ptr), layout);
        }
        HEAP_LIVE.fetch_sub(layout.size(), Ordering::Relaxed);
    }
}

/// (live, peak) — the workload's heap probe.
pub fn heap_now() -> (usize, usize) {
    (
        HEAP_LIVE.load(Ordering::Relaxed),
        HEAP_PEAK.load(Ordering::Relaxed),
    )
}

// --- semihosting (ARM "Semihosting for AArch32/AArch64", via bkpt 0xAB) ----

const SYS_WRITE0: u32 = 0x04; // r1 = NUL-terminated string
#[cfg(not(feature = "board"))]
const SYS_CLOCK: u32 = 0x10; // → centiseconds since start (virtual time)
const SYS_EXIT: u32 = 0x18; // r1 = ADP reason code
const ADP_STOPPED_APPLICATION_EXIT: u32 = 0x20026; // qemu exits rc 0
const ADP_STOPPED_RUNTIME_ERROR: u32 = 0x20023; // any other reason → rc 1

fn semihost(op: u32, param: u32) -> u32 {
    let ret: u32;
    // SAFETY: bkpt 0xAB is the ARMv7-M semihosting trap; qemu services it
    // and returns in r0. No memory is clobbered beyond what r1 points at.
    unsafe {
        core::arch::asm!(
            "bkpt 0xab",
            inout("r0") op => ret,
            in("r1") param,
            options(nostack),
        );
    }
    ret
}

/// Print one line via SYS_WRITE0 (allocates the NUL-terminated copy — fine,
/// the heap probe lines are read BEFORE the print of each phase).
pub fn write_line(line: &str) {
    let mut s = alloc::string::String::with_capacity(line.len() + 2);
    s.push_str(line);
    s.push('\n');
    s.push('\0');
    semihost(SYS_WRITE0, s.as_ptr() as u32);
}

/// Heap-free print for contexts where the heap may be the casualty (faults,
/// panic preamble). `msg` MUST be NUL-terminated.
fn write_raw(msg: &'static str) {
    debug_assert!(msg.ends_with('\0'));
    semihost(SYS_WRITE0, msg.as_ptr() as u32);
}

/// Centiseconds of qemu VIRTUAL time.
///
/// NOTE (measured 2026-07-23): this is only a deterministic instruction count
/// when qemu can drive the virtual clock from icount. On a qemu built WITHOUT
/// TCG plugins — including Fedora's stock `qemu-system-arm` 10.2.2 — SYS_CLOCK
/// tracks HOST WALL TIME: the identical workload read 39 cs idle and 58 cs with
/// the host CPU loaded, a 49 % swing an instruction count cannot have. Treat
/// this as indicative only; the `board` feature's `clock_cycles()` is the
/// trustworthy timer.
#[cfg(not(feature = "board"))]
pub fn clock_cs() -> i32 {
    semihost(SYS_CLOCK, 0) as i32
}

// --- RCC / PWR / FLASH (STM32F429, RM0090) ---------------------------------
#[cfg(feature = "board")]
mod rcc {
    pub const RCC_CR: *mut u32 = 0x4002_3800 as *mut u32;
    pub const RCC_PLLCFGR: *mut u32 = 0x4002_3804 as *mut u32;
    pub const RCC_CFGR: *mut u32 = 0x4002_3808 as *mut u32;
    pub const RCC_APB1ENR: *mut u32 = 0x4002_3840 as *mut u32;
    pub const FLASH_ACR: *mut u32 = 0x4002_3C00 as *mut u32;
    pub const PWR_CR: *mut u32 = 0x4000_7000 as *mut u32;
    pub const PWR_CSR: *mut u32 = 0x4000_7004 as *mut u32;

    pub const HSION: u32 = 1;
    pub const HSIRDY: u32 = 1 << 1;
    pub const HSEON: u32 = 1 << 16;
    pub const HSERDY: u32 = 1 << 17;
    pub const HSEBYP: u32 = 1 << 18;
    pub const PLLON: u32 = 1 << 24;
    pub const PLLRDY: u32 = 1 << 25;

    /// HSI is a 16 MHz on-die RC on every F4 — NOMINALLY. Measured on THIS
    /// part (`scripts/board-diag.py`: debugger-driven bring-up, DWT_CYCCNT
    /// gated against host wall time over a 10 s aperture): an HSI-sourced PLL
    /// configured for 180 MHz ran at 182.18–182.71 MHz, i.e. an HSI of
    /// 16.19–16.24 MHz — about 1.3 % fast, which would put the "180 MHz" over
    /// the part's rated maximum. That is why HSE is the first choice and this
    /// is only the fallback, and why a cycle count taken on the fallback must
    /// not be converted to milliseconds using this constant.
    pub const HSI_HZ: u32 = 16_000_000;
    /// The F429I-DISC1's HSE crystal. MEASURED, not assumed: the same gate
    /// with the PLL on HSE (M=8 N=360 P=2) bracketed the core at
    /// 179.49–180.01 MHz, i.e. a 7.977–8.000 MHz input — 8 MHz to within the
    /// measurement's own resolution.
    pub const HSE_HZ: u32 = 8_000_000;
}

/// Bring the STM32F429 to its rated 180 MHz with production flash settings.
///
/// The reset state (HSI 16 MHz, 0 flash wait states, ART off) is not a shipping
/// configuration and would understate flash-fetch cost, so the board leg
/// configures the part the way real firmware would:
///
/// * **HSE crystal** as the PLL source, with HSI as the fallback. Which HSE
///   mode this board wants was MEASURED over the debugger, not inferred from
///   the board name: with `HSEBYP=0` (crystal oscillator) `HSERDY` asserts in
///   0.5–0.7 ms; with `HSEBYP=1` (external clock, the ST-LINK-MCO assumption)
///   it never asserts, polled for 500 ms — 250x the datasheet's typical
///   start-up. An earlier revision of this function assumed bypass and spun on
///   `HSERDY` forever — a silent boot hang with no output, which is exactly
///   what the bounded spins below exist to prevent.
/// * **PLL → 180 MHz**: M chosen for a 1 MHz VCO input (HSE 8 MHz → M=8,
///   HSI 16 MHz → M=16), N=360 (360 MHz VCO), P=2 (180 MHz SYSCLK), Q=7
///   (unused; USB absent). HSE gives a true 180.0 MHz; the HSI fallback lands
///   wherever the RC actually is (182.4 MHz measured here — over the part's
///   rated maximum, hence fallback and not first choice).
/// * **Overdrive** — above 168 MHz the F429 requires voltage scale 1 plus the
///   overdrive handshake (ODEN → ODRDY, ODSWEN → ODSWRDY). Skipping it does not
///   fail loudly; it just runs out of spec.
/// * **5 flash wait states** — the 3.3 V / 150–180 MHz row of the latency
///   table. Too few silently corrupts fetches.
/// * **ART accelerator** (prefetch + I-cache + D-cache) — the mechanism that
///   makes 5 WS tolerable, and what every real F4 application enables.
/// * **Bus prescalers** AHB/1 (180), APB1/4 (45 MHz, max 45), APB2/2 (90 MHz,
///   max 90) — exceeding either APB ceiling is out of spec.
///
/// Ordering matters: flash latency and ART must be set BEFORE the switch to a
/// faster SYSCLK, never after. Nothing here is trusted on faith — [`sysclk_hz`]
/// recomputes the achieved clock from the registers afterwards and the boot
/// line prints it.
#[cfg(feature = "board")]
fn clock_init_180mhz() {
    use rcc::*;

    // Every hardware handshake below is bounded. A plain `while !ready {}` on a
    // flag that never asserts is an unrecoverable silent hang with no output —
    // measured here when the HSE was started in the wrong mode. On timeout we
    // leave the clock wherever it got to; `sysclk_hz()` then reports the truth
    // instead of the intent, so the run still produces numbers and says what
    // clock they were taken at.
    //
    // 1e6 iterations ≈ 0.3 s at the 16 MHz reset clock (the loop is a handful
    // of cycles per pass): two orders past the slowest thing waited on here
    // (HSE crystal start-up, ~2 ms typical per the datasheet), short enough
    // that a dead oscillator fails fast instead of wedging the run.
    const SPIN_LIMIT: u32 = 1_000_000;
    fn spin_until(f: impl Fn() -> bool) -> bool {
        let mut n = 0u32;
        while !f() {
            n += 1;
            if n >= SPIN_LIMIT {
                return false;
            }
            core::hint::spin_loop();
        }
        true
    }

    // SAFETY: fixed STM32F429 peripheral addresses, single-threaded boot path
    // with interrupts still disabled; every spin is bounded (above).
    unsafe {
        // HSI (16 MHz, on-die) is the reset clock and the fallback; assert it.
        RCC_CR.write_volatile(RCC_CR.read_volatile() | HSION);
        if !spin_until(|| RCC_CR.read_volatile() & HSIRDY != 0) {
            return; // HSIRDY never set — stay on whatever is running
        }

        // HSE, crystal mode. HSEBYP is only writable while HSEON is clear
        // (RM0090 §6.3.1), so drop both first, then start the oscillator.
        let cr = RCC_CR.read_volatile() & !(HSEON | HSEBYP);
        RCC_CR.write_volatile(cr);
        RCC_CR.write_volatile(cr | HSEON);
        let hse = spin_until(|| RCC_CR.read_volatile() & HSERDY != 0);
        if !hse {
            // Crystal did not start: turn HSE back off and fall back to HSI,
            // rather than leaving a half-enabled oscillator behind.
            RCC_CR.write_volatile(RCC_CR.read_volatile() & !HSEON);
        }

        // PWR clock on, voltage scale 1 (VOS = 0b11), then overdrive.
        RCC_APB1ENR.write_volatile(RCC_APB1ENR.read_volatile() | (1 << 28));
        PWR_CR.write_volatile(PWR_CR.read_volatile() | (0b11 << 14));
        PWR_CR.write_volatile(PWR_CR.read_volatile() | (1 << 16)); // ODEN
        if !spin_until(|| PWR_CSR.read_volatile() & (1 << 16) != 0) {
            return;
        }
        PWR_CR.write_volatile(PWR_CR.read_volatile() | (1 << 17)); // ODSWEN
        if !spin_until(|| PWR_CSR.read_volatile() & (1 << 17) != 0) {
            return;
        }

        // PLL: (HSE 8 / M=8) or (HSI 16 / M=16) = 1 MHz ref, ×N=360 = 360 MHz
        // VCO, /P=2 → 180 MHz. PLLP encodes 2 as 0b00; PLLSRC (bit 22) picks
        // HSE. Q=7 keeps the unused 48 MHz domain in range.
        let m: u32 = if hse { 8 } else { 16 };
        let src: u32 = if hse { 1 << 22 } else { 0 };
        // PLLP (bits 17:16) encodes /2 as 0b00, so the P field contributes no
        // set bits and is deliberately absent from this OR.
        RCC_PLLCFGR.write_volatile(m | (360 << 6) | src | (7 << 24));
        RCC_CR.write_volatile(RCC_CR.read_volatile() | PLLON);
        if !spin_until(|| RCC_CR.read_volatile() & PLLRDY != 0) {
            return; // PLL never locked — stay on HSI, still a valid cycle count
        }

        // Flash: 5 WS + prefetch + I-cache + D-cache, BEFORE the SYSCLK switch.
        FLASH_ACR.write_volatile((1 << 8) | (1 << 9) | (1 << 10) | 5);

        // Prescalers first (so no bus overspeeds the instant SYSCLK jumps),
        // then select the PLL and wait for the switch to be acknowledged.
        // AHB/1 = 0b0000, APB1/4 = 0b101 << 10, APB2/2 = 0b100 << 13.
        RCC_CFGR.write_volatile((0b101 << 10) | (0b100 << 13));
        RCC_CFGR.write_volatile(RCC_CFGR.read_volatile() | 0b10); // SW = PLL
        spin_until(|| RCC_CFGR.read_volatile() & (0b11 << 2) == (0b10 << 2)); // SWS
    }
}

/// True once SYSCLK is actually driven by the PLL — reported at boot so a
/// cycle count is never silently attributed to the wrong clock.
#[cfg(feature = "board")]
pub fn on_pll() -> bool {
    // SAFETY: fixed RCC address, read-only.
    unsafe { core::ptr::read_volatile(rcc::RCC_CFGR) & (0b11 << 2) == (0b10 << 2) }
}

/// The core clock as the SILICON is actually configured, Hz — recomputed from
/// RCC_CFGR/RCC_PLLCFGR rather than asserted by a constant, so a clock set-up
/// that partly failed reports the clock it reached instead of the one it
/// wanted. Nominal (the crystal/RC is what it is; see the measured figures on
/// [`rcc::HSE_HZ`] / [`rcc::HSI_HZ`]).
#[cfg(feature = "board")]
pub fn sysclk_hz() -> u32 {
    use rcc::*;
    // SAFETY: fixed RCC addresses, read-only.
    let (cfgr, pllcfgr) = unsafe {
        (
            core::ptr::read_volatile(RCC_CFGR),
            core::ptr::read_volatile(RCC_PLLCFGR),
        )
    };
    let sysclk = match (cfgr >> 2) & 0b11 {
        0b00 => HSI_HZ,
        0b01 => HSE_HZ,
        0b10 => {
            let src = if pllcfgr & (1 << 22) != 0 {
                HSE_HZ
            } else {
                HSI_HZ
            };
            let m = pllcfgr & 0x3F;
            let n = (pllcfgr >> 6) & 0x1FF;
            let p = (((pllcfgr >> 16) & 0b11) + 1) * 2;
            if m == 0 || p == 0 {
                return 0;
            }
            src / m * n / p
        }
        _ => return 0,
    };
    // AHB prescaler: HPRE < 8 = /1, else 2^(HPRE-7) with 5 skipped (÷32 is
    // encoded 0b1100, not 0b1011) — the RM0090 table, not a naive shift.
    let hpre = (cfgr >> 4) & 0xF;
    let shift = match hpre {
        0..=7 => 0,
        8 => 1,
        9 => 2,
        10 => 3,
        11 => 4,
        12 => 6,
        13 => 7,
        14 => 8,
        _ => 9,
    };
    sysclk >> shift
}

/// Which oscillator the PLL (or SYSCLK directly) is running from — printed at
/// boot next to the frequency so the reader can tell a 180.0 MHz crystal run
/// from a fallback.
#[cfg(feature = "board")]
pub fn clock_source() -> &'static str {
    // SAFETY: fixed RCC addresses, read-only.
    let (cfgr, pllcfgr) = unsafe {
        (
            core::ptr::read_volatile(rcc::RCC_CFGR),
            core::ptr::read_volatile(rcc::RCC_PLLCFGR),
        )
    };
    match (cfgr >> 2) & 0b11 {
        0b00 => "HSI direct (PLL NOT engaged)",
        0b01 => "HSE direct (PLL NOT engaged)",
        0b10 if pllcfgr & (1 << 22) != 0 => "HSE crystal -> PLL",
        0b10 => "HSI -> PLL (HSE FALLBACK: crystal did not start)",
        _ => "unknown",
    }
}

/// Snapshot of the clock/flash registers, for the runner to assert against —
/// the boot line is the only place these leave the chip, and a number nobody
/// can check is a number nobody should trust.
#[cfg(feature = "board")]
pub fn clock_regs() -> (u32, u32, u32, u32, u32) {
    use rcc::*;
    // SAFETY: fixed RCC/FLASH/PWR addresses, read-only.
    unsafe {
        (
            core::ptr::read_volatile(RCC_CR),
            core::ptr::read_volatile(RCC_PLLCFGR),
            core::ptr::read_volatile(RCC_CFGR),
            core::ptr::read_volatile(FLASH_ACR),
            core::ptr::read_volatile(PWR_CSR),
        )
    }
}

/// Real CPU cycles from the Cortex-M4 DWT cycle counter (`board` only).
///
/// DWT_CYCCNT increments once per core clock and is immune to host load — it
/// is the honest per-frame cost, and unlike emulation it includes flash wait
/// states, the ART accelerator and bus contention. Free-running 32-bit, so it
/// wraps every 2^32 cycles (~23.9 s at 180 MHz); the workload's timed window
/// is milliseconds, and the subtraction is done in wrapping arithmetic, so a
/// wrap across the window still yields the correct delta.
#[cfg(feature = "board")]
pub fn clock_cycles() -> i32 {
    const DEMCR: *mut u32 = 0xE000_EDFC as *mut u32; // Debug Exception & Monitor Control
    const DWT_CTRL: *mut u32 = 0xE000_1000 as *mut u32;
    const DWT_CYCCNT: *mut u32 = 0xE000_1004 as *mut u32;
    const TRCENA: u32 = 1 << 24; // DEMCR: enable the trace/debug block (DWT)
    const CYCCNTENA: u32 = 1 << 0; // DWT_CTRL: run the cycle counter

    // SAFETY: fixed ARMv7-M debug-block addresses, always present on a
    // Cortex-M4. Single-threaded, no interrupts touch these.
    unsafe {
        if core::ptr::read_volatile(DEMCR) & TRCENA == 0 {
            core::ptr::write_volatile(DEMCR, core::ptr::read_volatile(DEMCR) | TRCENA);
            core::ptr::write_volatile(DWT_CYCCNT, 0);
            core::ptr::write_volatile(DWT_CTRL, core::ptr::read_volatile(DWT_CTRL) | CYCCNTENA);
        }
        core::ptr::read_volatile(DWT_CYCCNT) as i32
    }
}

/// Terminate qemu: rc 0 on ok, rc 1 otherwise.
pub fn exit(ok: bool) -> ! {
    semihost(
        SYS_EXIT,
        if ok {
            ADP_STOPPED_APPLICATION_EXIT
        } else {
            ADP_STOPPED_RUNTIME_ERROR
        },
    );
    // SYS_EXIT does not return under qemu; satisfy the type honestly.
    #[allow(clippy::empty_loop)] // nothing left to wait for — qemu is gone
    loop {}
}

// --- vector table + reset ---------------------------------------------------

type Handler = unsafe extern "C" fn() -> !;

/// The 16 ARMv7-M system-exception slots: SP, Reset, then NMI/HardFault/
/// MemManage/BusFault/UsageFault, 4 reserved, SVCall, Debug, reserved,
/// PendSV, SysTick — everything but Reset lands in [`fault`] (loud exit 1,
/// never a wild jump into whatever bytes follow the table in flash).
#[repr(C)]
pub struct VectorTable {
    sp: u32,
    reset: Handler,
    exceptions: [Handler; 14],
}

// SAFETY: plain constant data, read by the core at boot.
unsafe impl Sync for VectorTable {}

#[unsafe(link_section = ".vectors")]
#[unsafe(no_mangle)]
pub static VECTORS: VectorTable = VectorTable {
    sp: STACK_TOP,
    reset,
    exceptions: [fault; 14],
};

/// Any exception = a bug in this vehicle (no interrupts are ever enabled):
/// report heap-free and exit 1 — honest failure, qemu terminates.
unsafe extern "C" fn fault() -> ! {
    write_raw("FATAL [vyr-size] cpu exception (NMI/HardFault/...) — exiting 1\n\0");
    exit(false)
}

/// crt0 + main. First code the core runs; until `.bss` is zeroed and
/// `.data` copied, NO statics may be touched — raw-pointer loops only.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn reset() -> ! {
    unsafe extern "C" {
        static mut __sbss: u32;
        static mut __ebss: u32;
        static mut __sdata: u32;
        static mut __edata: u32;
        static __sidata: u32;
    }
    // SAFETY: the symbols delimit the linker-laid .bss/.data ranges
    // (link-qemu.ld, 4-aligned); nothing else runs yet.
    unsafe {
        // FIRST: enable CP10/CP11 (the M4F FPU) via CPACR. eabihf codegen
        // uses VFP instructions freely, and the FPU is architecturally
        // DISABLED at reset — without this the first FP instruction is a
        // NOCP UsageFault escalating straight to a boot lockup (measured:
        // qemu "Lockup: can't escalate 3 to HardFault"). The barriers make
        // the enable visible before any subsequent FP issue.
        const CPACR: *mut u32 = 0xE000_ED88 as *mut u32;
        CPACR.write_volatile(CPACR.read_volatile() | (0xF << 20));
        core::arch::asm!("dsb", "isb", options(nostack));

        // Real silicon: bring the clock tree up to the part's rated maximum
        // BEFORE measuring anything. At reset the F429 runs on HSI at 16 MHz
        // with zero flash wait states and the ART accelerator off, which is
        // not a configuration anyone ships — and it flatters flash-heavy code,
        // because at 0 WS instruction fetch is free. Measuring there would
        // understate the per-frame cycle cost.
        #[cfg(feature = "board")]
        clock_init_180mhz();

        let mut b = core::ptr::addr_of_mut!(__sbss);
        let be = core::ptr::addr_of_mut!(__ebss);
        while b < be {
            b.write_volatile(0);
            b = b.add(1);
        }
        let mut d = core::ptr::addr_of_mut!(__sdata);
        let de = core::ptr::addr_of_mut!(__edata);
        let mut s = core::ptr::addr_of!(__sidata);
        while d < de {
            d.write_volatile(s.read_volatile());
            d = d.add(1);
            s = s.add(1);
        }
        // Statics are now live. Hand the arena to the allocator.
        (*HEAP.0.get()).init(ARENA.0.get() as *mut u8, ARENA_BYTES);
    }
    main()
}

fn main() -> ! {
    #[cfg(feature = "board")]
    {
        let hz = sysclk_hz();
        let (cr, pllcfgr, cfgr, acr, pwr_csr) = clock_regs();
        write_line(&alloc::format!(
            "INFO  [vyr-size] boot: STM32F429I-DISC1 (F429ZI/M4F) REAL SILICON, \
             sysclk_hz={hz} src={}, crt0 + FPU done; \
             heap arena 122880 B in SRAM, band buffer 23040 B + stack in CCM",
            clock_source()
        ));
        // The registers themselves, so the runner (and a reader) can verify
        // 5 wait states + ART + PLL rather than take the prose on trust.
        write_line(&alloc::format!(
            "INFO  [vyr-size] clock regs: RCC_CR={cr:#010x} RCC_PLLCFGR={pllcfgr:#010x} \
             RCC_CFGR={cfgr:#010x} FLASH_ACR={acr:#010x} PWR_CSR={pwr_csr:#010x} \
             (latency={} prften={} icen={} dcen={} on_pll={})",
            acr & 0xF,
            (acr >> 8) & 1,
            (acr >> 9) & 1,
            (acr >> 10) & 1,
            on_pll()
        ));
    }
    #[cfg(not(feature = "board"))]
    write_line(
        "INFO  [vyr-size] boot: netduinoplus2 (STM32F405/M4F), crt0 + FPU done; \
         heap arena 122880 B in SRAM, band buffer 23040 B + stack in CCM",
    );
    let mut emit = |line: &str| write_line(line);
    let heap = || heap_now();
    // The board leg counts REAL CPU cycles (DWT_CYCCNT); the qemu leg reads
    // SYS_CLOCK, which on a plugin-less qemu is host wall time, not insns.
    #[cfg(feature = "board")]
    let clock = || clock_cycles();
    #[cfg(not(feature = "board"))]
    let clock = || clock_cs();
    // SAFETY: sole reference ever taken to the CCM band buffer (single
    // pass through main on a single-threaded target).
    let band_buf = unsafe { &mut *BAND_BUF.0.get() };

    // #28, `--features lcd` only: draw the panel-native scene on the board's
    // own ILI9341 FIRST, so pixels appear seconds after flash rather than
    // after the ~13 s timed loop. It borrows a 240x16 prefix of the same CCM
    // band buffer (11,520 of 23,040 B) — no second buffer, no framebuffer at
    // all, because the controller holds the frame memory. This block is
    // entirely outside the DWT window opened later inside `workload::run`,
    // and the frame hash / cycle count that follow are unchanged by it.
    #[cfg(feature = "lcd")]
    {
        let n = crate::lcd::PANEL_BAND_BYTES;
        match crate::lcd::show_panel_scene(
            &mut emit,
            &mut band_buf[..n],
            crate::workload::WORKLOAD_QUALITY,
        ) {
            Ok(_) => {}
            // Honest failure, but NOT fatal: the measurement half of this
            // vehicle is the load-bearing part and still has to report.
            Err(e) => write_line(&alloc::format!("ERROR [vyr-size] lcd scene failed: {e:?}")),
        }
    }

    match crate::workload::run(
        &mut emit,
        &heap,
        Some(&clock),
        band_buf,
        crate::workload::WORKLOAD_QUALITY,
    ) {
        Ok(_) => exit(true),
        Err(e) => {
            write_line(&alloc::format!("ERROR [vyr-size] workload failed: {e:?}"));
            exit(false)
        }
    }
}

/// Panic = exit 1, loudly: a heap-free preamble first (the heap may be what
/// broke), then a best-effort formatted message.
#[panic_handler]
fn panic(info: &core::panic::PanicInfo) -> ! {
    write_raw("FATAL [vyr-size] panic:\n\0");
    write_line(&alloc::format!("FATAL [vyr-size] {info}"));
    exit(false)
}

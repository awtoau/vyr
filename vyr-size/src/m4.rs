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

/// Centiseconds of qemu VIRTUAL time (deterministic under icount).
pub fn clock_cs() -> i32 {
    semihost(SYS_CLOCK, 0) as i32
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
    write_line(
        "INFO  [vyr-size] boot: netduinoplus2 (STM32F405/M4F), crt0 + FPU done; \
         heap arena 122880 B in SRAM, band buffer 23040 B + stack in CCM",
    );
    let mut emit = |line: &str| write_line(line);
    let heap = || heap_now();
    let clock = || clock_cs();
    // SAFETY: sole reference ever taken to the CCM band buffer (single
    // pass through main on a single-threaded target).
    let band_buf = unsafe { &mut *BAND_BUF.0.get() };
    match crate::workload::run(&mut emit, &heap, Some(&clock), band_buf) {
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

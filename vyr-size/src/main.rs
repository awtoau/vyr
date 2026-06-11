//! vyr-size — the F9 STATIC-size measurement vehicle (#9).
//!
//! A real LINKED ELF is the only honest size number: an rlib/staticlib
//! overcounts because dead-code elimination has not run. This bin links
//! vyr-core's REAL render path (parse → tree → layout → paint → pixels) for
//! `thumbv7em-none-eabihf` (STM32F427-class M4F) so `arm-none-eabi-size`
//! reports what the renderer actually costs in flash and static RAM.
//! `./dev.py size-mcu` builds the feature/profile matrix and prints the
//! table; `docs/measurements/f9-static.md` records the numbers.
//!
//! **NOT runnable firmware** in its default/size-matrix configs. No
//! cortex-m-rt, no vector table, no `.data` copy-up or `.bss` zeroing, no
//! clocks. `_start` exists so the linker has a live root reaching the real
//! render path and `--gc-sections` keeps exactly what a real firmware would
//! keep. Flashing this ELF would not boot.
//!
//! **EXCEPT `--features run-qemu`** (#9 dynamic half): that config IS
//! runnable — [`m4`] adds a vector table + crt0 + semihosting + a counted
//! real heap, [`workload`] renders a 480×270 fixture in 480×16 bands with
//! the ASCII subset font, and `./dev.py qemu-m4` boots it under
//! `qemu-system-arm -machine netduinoplus2` (its own linker script:
//! link-qemu.ld). The same feature on a host runs the same workload under a
//! counting allocator — the cross-ISA hash reference.
//!
//! `unsafe` is allowed HERE ONLY (the bump allocator in [`mcu`]); vyr-core
//! itself stays `#![forbid(unsafe_code)]` — this crate is measurement
//! scaffolding, never shipped renderer code.
//!
//! Features select which ASSETS get baked into `.rodata`, not which code is
//! linked: every widget arm of the IR interpreter is statically reachable
//! from `render()` (JSON content is runtime data — LTO/GC can never prove
//! text or images away), so the text and image CODE is all-in even in the
//! code-only build. `font` bakes `fonts/roboto.ttf` + renders the F5 text
//! fixture; `image` bakes the 24×24 RGBA checker + renders the F6 image
//! fixture.
//!
//! On hosts (`not(target_os = "none")`) this is a plain std bin whose `main`
//! RUNS the same exercise the MCU stub links — the workspace gate therefore
//! proves the stub's renders actually succeed (an ELF that can never run
//! would otherwise hide a broken stub).

#![cfg_attr(target_os = "none", no_std)]
#![cfg_attr(target_os = "none", no_main)]

extern crate alloc;

#[cfg(all(target_os = "none", feature = "run-qemu"))]
mod m4;
#[cfg(feature = "run-qemu")]
mod workload;

#[cfg(not(feature = "run-qemu"))]
use alloc::vec;

#[cfg(not(feature = "run-qemu"))]
use vyr_core::{Assets, Fonts, Rect, RenderError};

/// Make a baked-in byte/str constant opaque to the optimizer — the moral
/// equivalent of "these bytes arrive from a flash address at run time".
/// Load-bearing: WITHOUT this, LLVM inlines the bump allocator, constant-
/// folds the 160 KB TTF `to_vec` against the 64 KiB arena, proves the
/// allocation must fail, and dead-codes the ENTIRE render path behind it
/// (first build of this vehicle measured 2.8 KB — the panic machinery and
/// nothing else). Opaque inputs keep both branches, i.e. the real path.
fn opaque<T>(v: T) -> T {
    core::hint::black_box(v)
}

/// Render one 120×16 band of a 120×120 fixture into a heap working buffer —
/// the MCU working mode (invariant I1): small band, caller-provided RGB888
/// buffer. `y = 16` crosses real content in all three fixtures. Returns a
/// fold of every output byte so the optimizer cannot discard the render.
#[cfg(not(feature = "run-qemu"))]
fn render_band(ir: &str, fonts: &mut Fonts, assets: &Assets) -> Result<u8, RenderError> {
    const W: usize = 120;
    const H: usize = 16;
    let band = Rect {
        x: 0,
        y: 16,
        w: W as u32,
        h: H as u32,
    };
    let mut buf = vec![0u8; W * H * 3];
    let stats = vyr_core::render_with(ir, fonts, assets, band, &mut buf, W * 3)?;
    let mut acc = stats.pixels_written as u8;
    // black_box defeats value tracking: the buffer must be materialized.
    for b in core::hint::black_box(&buf[..]) {
        acc = acc.wrapping_add(*b);
    }
    Ok(acc)
}

#[cfg(all(feature = "font", not(feature = "run-qemu")))]
fn make_fonts() -> Result<Fonts, RenderError> {
    let mut fonts = Fonts::new();
    // The MCU font model (plan §F5): the TTF lives in flash. include_bytes!
    // puts the real Roboto in .rodata — the size table's font line item.
    // (Fonts::register then COPIES it to the heap — a real F9 finding, see
    // docs/measurements/f9-static.md; irrelevant to the static numbers.)
    fonts.register(
        "roboto",
        opaque(include_bytes!("../../fonts/roboto.ttf").as_slice()).to_vec(),
    )?;
    Ok(fonts)
}

#[cfg(all(not(feature = "font"), not(feature = "run-qemu")))]
fn make_fonts() -> Result<Fonts, RenderError> {
    Ok(Fonts::new())
}

#[cfg(all(feature = "image", not(feature = "run-qemu")))]
fn make_assets() -> Result<Assets, RenderError> {
    let mut assets = Assets::new();
    // The F15 bake-to-flash model: decode happened at build time
    // (scripts/make-test-asset.py emits the raw straight-RGBA twin of the
    // committed checker PNG); the device blits flash-resident pixels.
    assets.register(
        vyr_core::demo::IMAGE_ASSET,
        vyr_core::RgbaImage::new(
            24,
            24,
            opaque(include_bytes!("../assets/checker-24.rgba").as_slice()).to_vec(),
        )?,
    )?;
    Ok(assets)
}

#[cfg(all(not(feature = "image"), not(feature = "run-qemu")))]
fn make_assets() -> Result<Assets, RenderError> {
    Ok(Assets::new())
}

/// Exercise the real render path once per enabled fixture, folding all
/// output into one observable byte (what `_start` publishes to [`mcu::SINK`]
/// and the host `main` prints).
#[cfg(not(feature = "run-qemu"))]
fn exercise() -> Result<u8, RenderError> {
    let mut fonts = make_fonts()?;
    let assets = make_assets()?;

    let mut acc: u8 = 0;
    acc = acc.wrapping_add(render_band(
        opaque(vyr_core::demo::DEMO_IR),
        &mut fonts,
        &assets,
    )?);
    #[cfg(feature = "font")]
    {
        acc = acc.wrapping_add(render_band(
            opaque(vyr_core::demo::TEXT_IR),
            &mut fonts,
            &assets,
        )?);
    }
    #[cfg(feature = "image")]
    {
        acc = acc.wrapping_add(render_band(
            opaque(vyr_core::demo::IMAGE_IR),
            &mut fonts,
            &assets,
        )?);
    }
    Ok(acc)
}

/// Everything the none-target needs beyond [`exercise`]: a global allocator
/// over a fixed static arena, a panic handler, and the `_start` link root.
/// (The run-qemu config replaces all of this with [`m4`] — a REAL boot.)
#[cfg(all(target_os = "none", not(feature = "run-qemu")))]
mod mcu {
    use core::alloc::{GlobalAlloc, Layout};
    use core::cell::UnsafeCell;
    use core::sync::atomic::{AtomicU8, AtomicUsize, Ordering};

    /// Where the render result lands — one observable byte, so the whole
    /// frame is a live computation to the optimizer.
    #[unsafe(no_mangle)]
    static SINK: AtomicU8 = AtomicU8::new(0);

    /// Bump-arena size: 64 KiB of `.bss` — measurement scaffolding, called
    /// out separately in the size table (`./dev.py size-mcu` nets it out).
    /// Sized for ONE 120×16 band working set (pixmap ≈ 17.4 KB + band buffer
    /// ≈ 5.8 KB + parse); the font config's runtime working set is larger
    /// (the TTF heap copy) — irrelevant here because this ELF never runs,
    /// only links. The real working-RAM model lives in
    /// docs/measurements/f9-static.md.
    const ARENA_BYTES: usize = 64 * 1024;

    #[repr(C, align(8))]
    struct Arena(UnsafeCell<[u8; ARENA_BYTES]>);

    // SAFETY: the allocator below hands out disjoint ranges via the atomic
    // cursor CAS; the cell itself is never accessed elsewhere.
    unsafe impl Sync for Arena {}

    static ARENA: Arena = Arena(UnsafeCell::new([0u8; ARENA_BYTES]));
    static CURSOR: AtomicUsize = AtomicUsize::new(0);

    /// The tiniest legitimate `#[global_allocator]`: bump-allocate from
    /// [`ARENA`], never free. Exhaustion returns null → `alloc` error →
    /// panic → park (honest failure, no silent wrap).
    struct Bump;

    unsafe impl GlobalAlloc for Bump {
        unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
            let mut cur = CURSOR.load(Ordering::Relaxed);
            loop {
                let aligned = (cur + layout.align() - 1) & !(layout.align() - 1);
                let end = match aligned.checked_add(layout.size()) {
                    Some(e) if e <= ARENA_BYTES => e,
                    _ => return core::ptr::null_mut(), // arena exhausted
                };
                match CURSOR.compare_exchange_weak(cur, end, Ordering::Relaxed, Ordering::Relaxed) {
                    // SAFETY: [aligned, end) is in-bounds of ARENA and was
                    // exclusively claimed by the CAS above; alignment ≤ 8 is
                    // honoured by the arena's own alignment, larger ones by
                    // the cursor rounding (arena base is 8-aligned and only
                    // over-aligned requests could misalign — vyr-core
                    // allocates nothing over-aligned, and a miss would only
                    // misrender a stub that never runs).
                    Ok(_) => return unsafe { (ARENA.0.get() as *mut u8).add(aligned) },
                    Err(seen) => cur = seen,
                }
            }
        }

        unsafe fn dealloc(&self, _ptr: *mut u8, _layout: Layout) {
            // Bump allocators never free — fine for a size vehicle.
        }
    }

    #[global_allocator]
    static BUMP: Bump = Bump;

    /// No UART, no semihosting — a size vehicle has nowhere to report; park.
    #[panic_handler]
    fn panic(_: &core::panic::PanicInfo) -> ! {
        loop {}
    }

    /// The link root (`ENTRY(_start)` + `KEEP` in link.ld): run the real
    /// render path, publish the result byte, park forever.
    #[unsafe(no_mangle)]
    extern "C" fn _start() -> ! {
        let byte = match super::exercise() {
            Ok(b) => b,
            // Distinct, observable failure marker — never Debug-format the
            // error here (that would add fmt machinery the REAL path does
            // not need at this point).
            Err(_) => 0xEE,
        };
        SINK.store(core::hint::black_box(byte), Ordering::Relaxed);
        loop {}
    }
}

/// Host leg: run the exact stub the MCU ELF links, loudly.
#[cfg(all(not(target_os = "none"), not(feature = "run-qemu")))]
fn main() {
    match exercise() {
        Ok(byte) => println!("vyr-size: exercise ok (sink byte {byte:#04x})"),
        Err(e) => {
            eprintln!("vyr-size: exercise FAILED: {e:?}");
            std::process::exit(1);
        }
    }
}

/// run-qemu host leg: the SAME banded workload the M4 boots, under a
/// counting allocator (vyr-cli's pattern) — prints the same structured
/// lines, PLUS a band-vs-full-frame hash self-check the MCU cannot afford
/// (the 567,424 B full-frame pixmap). Its `frame fnv1a=` line is the x86
/// reference the qemu-m4 runner compares the M4's hash against.
#[cfg(all(not(target_os = "none"), feature = "run-qemu"))]
mod host_runq {
    use std::alloc::{GlobalAlloc, Layout, System};
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct CountingAlloc;

    static HEAP_LIVE: AtomicUsize = AtomicUsize::new(0);
    static HEAP_PEAK: AtomicUsize = AtomicUsize::new(0);

    unsafe impl GlobalAlloc for CountingAlloc {
        unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
            let p = unsafe { System.alloc(layout) };
            if !p.is_null() {
                let live = HEAP_LIVE.fetch_add(layout.size(), Ordering::Relaxed) + layout.size();
                HEAP_PEAK.fetch_max(live, Ordering::Relaxed);
            }
            p
        }
        unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
            unsafe { System.dealloc(ptr, layout) };
            HEAP_LIVE.fetch_sub(layout.size(), Ordering::Relaxed);
        }
    }

    #[global_allocator]
    static ALLOC: CountingAlloc = CountingAlloc;

    pub fn heap_now() -> (usize, usize) {
        (
            HEAP_LIVE.load(Ordering::Relaxed),
            HEAP_PEAK.load(Ordering::Relaxed),
        )
    }
}

#[cfg(all(not(target_os = "none"), feature = "run-qemu"))]
fn main() {
    let mut emit = |line: &str| println!("{line}");
    let heap = || host_runq::heap_now();
    // Heap-allocated on the host (vyr-cli's shape); the M4 leg places it in
    // CCM instead — see workload::BAND_BYTES for why both are honest.
    let mut band_buf = alloc::vec![0u8; workload::BAND_BYTES];
    let banded = match workload::run(&mut emit, &heap, None, &mut band_buf) {
        Ok(h) => h,
        Err(e) => {
            eprintln!("ERROR [vyr-size] host run-qemu workload FAILED: {e:?}");
            std::process::exit(1);
        }
    };
    // Band-equivalence self-check, host-only (full frame needs 567 KB):
    match workload::full_frame_hash() {
        Ok(full) if full == banded => {
            println!("INFO  [vyr-size] full-frame fnv1a={full:#018x} == banded (band equivalence)");
        }
        Ok(full) => {
            eprintln!(
                "ERROR [vyr-size] full-frame fnv1a={full:#018x} != banded {banded:#018x} — \
                 band equivalence BROKE"
            );
            std::process::exit(1);
        }
        Err(e) => {
            eprintln!("ERROR [vyr-size] full-frame cross-check FAILED: {e:?}");
            std::process::exit(1);
        }
    }
}

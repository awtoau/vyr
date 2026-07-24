//! Link wiring for the thumbv7em builds — and ONLY then. Host builds of this
//! crate are a plain std bin (the workspace gate runs them), so the custom
//! memory maps must never leak into a host link.
//!
//! Three scripts, selected by feature (most specific first):
//! - default (the F9 STATIC size matrix): `link.ld` — F427 map, NOT runnable.
//! - `run-qemu` (the F9 RUNNABLE vehicle): `link-qemu.ld` — netduinoplus2
//!   (STM32F405) map with vector table + crt0 symbols.
//! - `board` (the F9 BOARD half): `link-board.ld` — STM32F429I-DISC1 map
//!   (2 M flash / 192 K SRAM). `board` implies `run-qemu`, so it MUST be
//!   tested before it.

fn main() {
    println!("cargo:rerun-if-changed=link.ld");
    println!("cargo:rerun-if-changed=link-qemu.ld");
    println!("cargo:rerun-if-changed=link-board.ld");
    // The panel bring-up sweep's compile-time parameters (`src/lcd.rs` MADCTL
    // and SPI_BR, `src/present.rs` RB_SWAP). rustc already records `option_env!`
    // as a dependency, so this is belt-and-braces — but a stale image flashed
    // to a board is a wrong ANSWER, not a slow build, and the runner
    // additionally verifies the value the running target reports.
    println!("cargo:rerun-if-env-changed=VYR_MADCTL");
    println!("cargo:rerun-if-env-changed=VYR_RB_SWAP");
    println!("cargo:rerun-if-env-changed=VYR_SPI_BR");
    // TARGET is the triple cargo is building FOR (host builds see the host
    // triple here): gate every link arg on the MCU target.
    let target = std::env::var("TARGET").unwrap_or_default();
    if target.starts_with("thumbv7em") {
        let dir = std::env::var("CARGO_MANIFEST_DIR").expect("cargo sets CARGO_MANIFEST_DIR");
        // `board` implies `run-qemu`, so it must be checked FIRST or the
        // board build would silently link the 128 KiB qemu map.
        let script = if std::env::var("CARGO_FEATURE_BOARD").is_ok() {
            "link-board.ld"
        } else if std::env::var("CARGO_FEATURE_RUN_QEMU").is_ok() {
            "link-qemu.ld"
        } else {
            "link.ld"
        };
        // Absolute path so the linker needs no search-path setup.
        println!("cargo:rustc-link-arg-bins=-T{dir}/{script}");
        // Section-level dead-code elimination — the honest half of "a linked
        // ELF is the only honest size number" (an rlib overcounts).
        println!("cargo:rustc-link-arg-bins=--gc-sections");
    }
}

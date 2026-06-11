//! Link wiring for the thumbv7em builds — and ONLY then. Host builds of this
//! crate are a plain std bin (the workspace gate runs them), so the custom
//! memory maps must never leak into a host link.
//!
//! Two scripts, selected by feature:
//! - default (the F9 STATIC size matrix): `link.ld` — F427 map, NOT runnable.
//! - `run-qemu` (the F9 RUNNABLE vehicle): `link-qemu.ld` — netduinoplus2
//!   (STM32F405) map with vector table + crt0 symbols.

fn main() {
    println!("cargo:rerun-if-changed=link.ld");
    println!("cargo:rerun-if-changed=link-qemu.ld");
    // TARGET is the triple cargo is building FOR (host builds see the host
    // triple here): gate every link arg on the MCU target.
    let target = std::env::var("TARGET").unwrap_or_default();
    if target.starts_with("thumbv7em") {
        let dir = std::env::var("CARGO_MANIFEST_DIR").expect("cargo sets CARGO_MANIFEST_DIR");
        let script = if std::env::var("CARGO_FEATURE_RUN_QEMU").is_ok() {
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

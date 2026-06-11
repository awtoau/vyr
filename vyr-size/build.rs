//! Link wiring for the thumbv7em size measurement — and ONLY then. Host
//! builds of this crate are a plain std bin (the workspace gate runs them),
//! so the custom memory map must never leak into a host link.

fn main() {
    println!("cargo:rerun-if-changed=link.ld");
    // TARGET is the triple cargo is building FOR (host builds see the host
    // triple here): gate every link arg on the MCU target.
    let target = std::env::var("TARGET").unwrap_or_default();
    if target.starts_with("thumbv7em") {
        let dir = std::env::var("CARGO_MANIFEST_DIR").expect("cargo sets CARGO_MANIFEST_DIR");
        // The F427 memory map + ENTRY(_start); absolute path so the linker
        // needs no search-path setup.
        println!("cargo:rustc-link-arg-bins=-T{dir}/link.ld");
        // Section-level dead-code elimination — the honest half of "a linked
        // ELF is the only honest size number" (an rlib overcounts).
        println!("cargo:rustc-link-arg-bins=--gc-sections");
    }
}

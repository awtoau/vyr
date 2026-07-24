/* vyr-size/link-qemu.ld — the RUNNABLE layout for the F9 dynamic half (#9),
 * `--features run-qemu` only (build.rs selects; the static-size matrix keeps
 * link.ld and its F427 2 MiB map untouched).
 *
 * Machine: qemu-system-arm `netduinoplus2` (STM32F405 SoC — Cortex-M4F).
 * qemu ≥ 8 models the CORRECTED F405 memory layout (MEASURED here: writes
 * just below 0x20030000 BusFault at boot — there is no contiguous 192 KiB):
 *
 *   FLASH 1 MiB   @ 0x08000000   (aliased at 0x00000000, where the
 *                                 M-profile core fetches the boot vectors)
 *   SRAM  128 KiB @ 0x20000000   (SRAM1+SRAM2)
 *   CCM    64 KiB @ 0x10000000   (core-coupled: CPU-only, no DMA)
 *
 * The STM32F427 target this emulates has 192 KiB SRAM + the same 64 KiB
 * CCM, so this machine is STRICTLY TIGHTER than the F427 budget — numbers
 * that fit here fit the F427 with 64 KiB SRAM to spare. Placement follows
 * the classic F4 discipline (the f9-static doc's "CCM is a placement
 * decision for the on-target half" — decided here): heap arena in SRAM;
 * STACK and the DMA-free band buffer in CCM.
 *
 * Boot contract (ARMv7-M): word 0 of the vector table = initial SP, word 1
 * = reset handler (thumb bit set). vyr-size/src/m4.rs provides the table in
 * `.vectors`, KEPT and placed first in FLASH below. crt0 (`reset`) copies
 * .data LMA→VMA and zeroes .bss using the __sdata/__edata/__sidata and
 * __sbss/__ebss symbols defined here. The FPU must be enabled before any FP
 * instruction (m4.rs does, first thing — boot-lockup lesson).
 */

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 1M
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
  CCM   (rw)  : ORIGIN = 0x10000000, LENGTH = 64K
}

ENTRY(reset);

SECTIONS
{
  .vectors :
  {
    /* MUST be the first bytes of FLASH — the core reads SP/PC from here. */
    KEEP(*(.vectors));
  } > FLASH

  .text :
  {
    *(.text .text.*);
  } > FLASH

  .rodata :
  {
    *(.rodata .rodata.*);
  } > FLASH

  /* Unwind index tables: panic = abort, but rustc still emits them — keep
   * them COUNTED in flash, same as link.ld. */
  .ARM.exidx :
  {
    *(.ARM.exidx .ARM.exidx.*);
    *(.ARM.extab .ARM.extab.*);
  } > FLASH

  /* Initialized statics: VMA in RAM, load image in FLASH; crt0 copies up. */
  .data : ALIGN(4)
  {
    __sdata = .;
    *(.data .data.*);
    . = ALIGN(4);
    __edata = .;
  } > RAM AT > FLASH

  __sidata = LOADADDR(.data);

  .bss (NOLOAD) : ALIGN(4)
  {
    __sbss = .;
    *(.bss .bss.*);
    *(COMMON);
    . = ALIGN(4);
    __ebss = .;
  } > RAM

  /* CCM residents (NOLOAD — crt0 does not touch CCM; everything placed
   * here is fully written before any read): the reused band buffer. */
  .ccm (NOLOAD) : ALIGN(8)
  {
    *(.ccm .ccm.*);
    . = ALIGN(4);
    __eccm = .;
  } > CCM
}

/* The stack runs down from the top of CCM (initial SP = m4::STACK_TOP,
 * baked into the vector table). Assert the link leaves it headroom over
 * the CCM residents, because a silent collision with the band buffer would
 * corrupt rendered bytes (caught at LINK time, not as a flaky runtime
 * fault). lld wants ASSERT at top level.
 *
 * 24 KiB floor, MEASURED not guessed (#33, 2026-07-24): the
 * `--features stack-probe` build paints the dead stack at boot and scans it
 * after the workload, and this vehicle's real high-water mark is
 * **18,748-19,068 B** across every tier and every opt-level (z/s/2/3 — the
 * whole spread is 320 B; the flattening/JSON-parse depth is what it is
 * regardless of inlining). The previous 16 KiB floor was BELOW actual usage:
 * a band buffer grown to exactly 16 KiB of headroom would have linked
 * cleanly and overflowed at runtime. 24 KiB = measured + ~26 % margin, and
 * today's headroom is 42,496 B. Re-measure with:
 *   python3 scripts/optlevel-matrix.py --phases stack */
ASSERT(ORIGIN(CCM) + LENGTH(CCM) - __eccm >= 24576,
       "run-qemu: less than 24 KiB stack headroom above .ccm; measured high-water is ~19 KiB (#33) — shrink the band buffer")

/* And the heap arena + statics must fit SRAM outright (the linker errors on
 * overflow anyway; this names the failure). */
ASSERT(__ebss <= ORIGIN(RAM) + LENGTH(RAM),
       "run-qemu: .bss (heap arena) overflows the 128 KiB SRAM")

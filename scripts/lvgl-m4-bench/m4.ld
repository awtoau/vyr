/* lvgl-m4-bench/m4.ld — bare-metal STM32F405 (qemu netduinoplus2) layout for
 * the LVGL M4 benchmark. Mirrors vyr-size/link-qemu.ld so the two builds run
 * on the SAME machine + memory map and the flash/RAM numbers compare.
 *
 * Machine: qemu-system-arm `netduinoplus2` (STM32F405 SoC — Cortex-M4F).
 * qemu >= 8 models the CORRECTED F405 layout (vyr measured this the hard way):
 *
 *   FLASH 1 MiB   @ 0x08000000   (aliased at 0x00000000 — boot vectors here)
 *   SRAM  128 KiB @ 0x20000000   (SRAM1+SRAM2)
 *   CCM    64 KiB @ 0x10000000   (core-coupled: CPU-only, no DMA)
 *
 * Boot contract (ARMv7-M): word 0 of the vector table = initial SP, word 1 =
 * reset handler (thumb bit set). startup.c provides the table in `.isr_vector`,
 * KEPT first in FLASH. _reset copies .data LMA->VMA and zeroes .bss using the
 * symbols defined here. The FPU is enabled before any FP instruction (the
 * boot-lockup lesson vyr learned).
 *
 * Placement, classic F4 discipline (same as vyr): the LVGL heap pool + .bss in
 * SRAM; stack in CCM (CPU-only). Initial SP = top of CCM. We do NOT pin the
 * draw buffer to CCM the way vyr pins its band buffer — LVGL's draw buffer is a
 * normal C static, so it lands in .bss (SRAM) with the pool. Noted as a
 * divergence in compare.md: vyr's 23 KB band buffer is CCM-resident (outside
 * its heap), LVGL's draw buffer is counted inside its working RAM here.
 */

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 1M
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
  CCM   (rw)  : ORIGIN = 0x10000000, LENGTH = 64K
}

ENTRY(_reset)

/* Initial SP = top of CCM; stack grows down. */
_estack = ORIGIN(CCM) + LENGTH(CCM);

SECTIONS
{
  .isr_vector :
  {
    /* MUST be the first bytes of FLASH — the core reads SP/PC from here. */
    KEEP(*(.isr_vector))
  } > FLASH

  .text :
  {
    *(.text .text.*)
    *(.glue_7) *(.glue_7t)
    *(.eh_frame)
    KEEP(*(.init))
    KEEP(*(.fini))
  } > FLASH

  .rodata :
  {
    *(.rodata .rodata.*)
  } > FLASH

  /* C++/unwind tables (LVGL is C, but ThorVG etc. are off; keep COUNTED). */
  .ARM.extab :
  {
    *(.ARM.extab* .gnu.linkonce.armextab.*)
  } > FLASH

  .ARM.exidx :
  {
    __exidx_start = .;
    *(.ARM.exidx* .gnu.linkonce.armexidx.*)
    __exidx_end = .;
  } > FLASH

  /* Initialized statics: VMA in RAM, load image in FLASH; crt0 copies up. */
  .data : ALIGN(4)
  {
    _sdata = .;
    *(.data .data.*)
    . = ALIGN(4);
    _edata = .;
  } > RAM AT > FLASH

  _sidata = LOADADDR(.data);

  .bss (NOLOAD) : ALIGN(4)
  {
    _sbss = .;
    *(.bss .bss.*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
  } > RAM

  /* End of used SRAM (newlib _sbrk uses this if any libc malloc is pulled —
   * but the LVGL builtin pool is a .bss array, so the heap-end is moot). */
  _end = .;
  PROVIDE(end = .);

  /DISCARD/ : { *(.note.GNU-stack) }
}

/* The bss (LVGL pool lives here) must fit SRAM outright. */
ASSERT(_ebss <= ORIGIN(RAM) + LENGTH(RAM),
       "LVGL M4: .bss (incl. LV_MEM pool + draw buffer) overflows the 128 KiB SRAM")

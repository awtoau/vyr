/* vyr-size/link.ld — minimal hand-written layout for the F9 STATIC size
 * measurement (#9). STM32F427-class memory map:
 *
 *   FLASH 2 MiB   @ 0x08000000
 *   RAM   192 KiB @ 0x20000000   (SRAM1+2+3; the F427's extra 64 KiB CCM at
 *                                 0x10000000 is deliberately NOT modelled —
 *                                 core-coupled, no DMA access, a placement
 *                                 decision for the on-target half of F9)
 *
 * NOT runnable firmware (vyr-size/src/main.rs says the same from the Rust
 * side): no vector table, no .data copy-up, no .bss zeroing, no clock init.
 * This script exists so the linker lays the renderer out against the real
 * memory map and `arm-none-eabi-size` reports honest text/data/bss.
 */

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 2M
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 192K
}

ENTRY(_start);

SECTIONS
{
  .text :
  {
    /* The one live root — everything else earns its place by reachability
     * (--gc-sections). */
    KEEP(*(.text._start));
    *(.text .text.*);
  } > FLASH

  .rodata :
  {
    *(.rodata .rodata.*);
  } > FLASH

  /* Unwind index tables: panic = abort everywhere here, but rustc still
   * emits the sections — keep them in FLASH so they are COUNTED, never
   * silently discarded. */
  .ARM.exidx :
  {
    *(.ARM.exidx .ARM.exidx.*);
    *(.ARM.extab .ARM.extab.*);
  } > FLASH

  /* Initialized statics: RAM at run time (VMA), load image in FLASH (LMA) —
   * real firmware copies this up at boot. size(1) counts .data into both the
   * flash total and the RAM total, which is exactly the honest accounting. */
  .data : ALIGN(4)
  {
    *(.data .data.*);
  } > RAM AT > FLASH

  .bss (NOLOAD) : ALIGN(4)
  {
    *(.bss .bss.*);
    *(COMMON);
  } > RAM
}

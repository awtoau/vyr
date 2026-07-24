/* vyr-size/link-board.ld — the REAL-SILICON layout for the F9 board half (#9),
 * `--features board` only.
 *
 * Board: STM32F429I-DISC1 — STM32F429ZI, Cortex-M4F, ART accelerator.
 *
 *   FLASH 2 MiB   @ 0x08000000
 *   SRAM  192 KiB @ 0x20000000   (SRAM1 112K + SRAM2 16K + SRAM3 64K,
 *                                 contiguous on the F429 — unlike the F405
 *                                 qemu machine, which stops at 128 KiB)
 *   CCM    64 KiB @ 0x10000000   (core-coupled: CPU-only, no DMA)
 *
 * Same placement discipline as link-qemu.ld — heap arena in SRAM, stack and
 * the DMA-free band buffer in CCM — so the two legs differ ONLY in capacity
 * and in the clock source. This map is strictly ROOMIER than the qemu
 * machine's, so anything that linked for qemu links here.
 *
 * Why a board leg exists at all: qemu's SYS_CLOCK, on a build without TCG
 * plugins, returns host wall time rather than a guest instruction count
 * (measured: the same workload moved 39 -> 58 cs purely from host CPU load).
 * DWT_CYCCNT on real silicon counts real cycles and cannot be perturbed by
 * what the workstation is doing.
 *
 * Boot contract (ARMv7-M): word 0 of the vector table = initial SP, word 1 =
 * reset handler. On the real F429 with BOOT0 low the core boots from flash
 * aliased at 0x00000000, exactly as the qemu machine does.
 */

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 2M
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 192K
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
 * 24 KiB floor, MEASURED on the emulated twin of this vehicle (#33,
 * 2026-07-24, `--features stack-probe`): high-water 18,748-19,068 B across
 * every tier and every opt-level. The previous 16 KiB floor was BELOW actual
 * usage — it would have linked a firmware whose stack overflows. Kept in
 * step with link-qemu.ld; the board leg runs the same render path. */
ASSERT(ORIGIN(CCM) + LENGTH(CCM) - __eccm >= 24576,
       "board: less than 24 KiB stack headroom above .ccm; measured high-water is ~19 KiB (#33) — shrink the band buffer")

/* And the heap arena + statics must fit SRAM outright (the linker errors on
 * overflow anyway; this names the failure). */
ASSERT(__ebss <= ORIGIN(RAM) + LENGTH(RAM),
       "board: .bss (heap arena) overflows the 192 KiB SRAM")

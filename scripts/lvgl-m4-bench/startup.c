/* lvgl-m4-bench/startup.c — bare-metal ARMv7-M boot scaffolding for the LVGL
 * M4 benchmark on qemu-system-arm -machine netduinoplus2 (STM32F405, M4F).
 *
 * This is GENERIC ARM-architecture + ST-public-datasheet material, written
 * fresh in C: the 16 system-exception vector slots, crt0 (FPU enable, .data
 * copy-up, .bss zero), and ARM semihosting (SYS_WRITE0 / SYS_EXIT / SYS_CLOCK
 * via `bkpt 0xAB`). It mirrors the clean pattern in vyr-size/src/m4.rs but
 * shares no code — vyr stays clean-room. No vendor SDK, no CMSIS.
 *
 * Boot (ARMv7-M contract): the core reads word 0 of the vector table as the
 * initial SP and word 1 as the reset handler. qemu aliases flash (LMA
 * 0x08000000) at 0x0, so the `.isr_vector` section linked first into FLASH IS
 * the boot table. _reset enables the FPU (CPACR), runs crt0, then main().
 */

#include <stdint.h>
#include <stddef.h>

/* ---- semihosting (ARM "Semihosting for AArch32/AArch64", via bkpt 0xAB) --- */

#define SYS_WRITE0 0x04u /* r1 = NUL-terminated string                       */
#define SYS_CLOCK  0x10u /* -> centiseconds since start (virtual time)       */
#define SYS_EXIT   0x18u /* r1 = ADP reason code                             */

#define ADP_STOPPED_APPLICATION_EXIT 0x20026u /* qemu exits rc 0             */
#define ADP_STOPPED_RUNTIME_ERROR    0x20023u /* any other reason -> rc 1    */

static inline uint32_t semihost(uint32_t op, void *param)
{
    register uint32_t r0 __asm__("r0") = op;
    register void    *r1 __asm__("r1") = param;
    __asm__ volatile("bkpt 0xab"
                     : "+r"(r0)
                     : "r"(r1)
                     : "memory");
    return r0;
}

/* Print a NUL-terminated string (no newline added). */
void sh_write0(const char *s)
{
    semihost(SYS_WRITE0, (void *)s);
}

/* Centiseconds of qemu VIRTUAL time (deterministic under -icount shift=0). */
int32_t sh_clock_cs(void)
{
    return (int32_t)semihost(SYS_CLOCK, NULL);
}

/* Terminate qemu: rc 0 on ok, rc 1 otherwise. SYS_EXIT does not return. */
void sh_exit(int ok)
{
    uint32_t reason = ok ? ADP_STOPPED_APPLICATION_EXIT
                         : ADP_STOPPED_RUNTIME_ERROR;
    semihost(SYS_EXIT, (void *)(uintptr_t)reason);
    for (;;) {
    }
}

/* ---- vector table + reset ------------------------------------------------- */

extern uint32_t _estack; /* top of CCM (linker)                              */
extern uint32_t _sdata, _edata, _sidata, _sbss, _ebss;

void _reset(void);
int  main(void);

/* Any unexpected exception = a bug (no interrupts are ever enabled): report
 * heap-free and exit 1 — honest failure, qemu terminates. */
static void fault_handler(void)
{
    sh_write0("FATAL [lvgl-m4] cpu exception (NMI/HardFault/...) — exiting 1\n");
    sh_exit(0);
}

/* The 16 ARMv7-M system-exception slots: SP, Reset, then NMI/HardFault/
 * MemManage/BusFault/UsageFault, 4 reserved, SVCall, Debug, reserved, PendSV,
 * SysTick. Everything but Reset lands in fault_handler (loud exit 1). */
__attribute__((section(".isr_vector"), used))
void (*const g_isr_vector[16])(void) = {
    (void (*)(void))(&_estack), /* 0: initial SP   */
    _reset,                     /* 1: reset        */
    fault_handler,              /* 2: NMI          */
    fault_handler,              /* 3: HardFault    */
    fault_handler,              /* 4: MemManage    */
    fault_handler,              /* 5: BusFault     */
    fault_handler,              /* 6: UsageFault   */
    fault_handler,              /* 7: reserved     */
    fault_handler,              /* 8: reserved     */
    fault_handler,              /* 9: reserved     */
    fault_handler,              /* 10: reserved    */
    fault_handler,              /* 11: SVCall      */
    fault_handler,              /* 12: Debug       */
    fault_handler,              /* 13: reserved    */
    fault_handler,              /* 14: PendSV      */
    fault_handler,              /* 15: SysTick     */
};

/* crt0 + main. First code the core runs. Until .bss is zeroed and .data is
 * copied, nothing may touch initialized statics. */
__attribute__((naked, noreturn)) void _reset(void)
{
    /* Set SP explicitly: qemu loads it from the vector table, but a naked
     * function should not assume the compiler left it intact. */
    __asm__ volatile("ldr sp, =_estack");

    /* FIRST: enable CP10/CP11 (the M4F FPU) via CPACR. eabihf / -mfloat-abi=hard
     * codegen uses VFP freely, and the FPU is architecturally DISABLED at reset
     * — without this the first FP instruction is a NOCP UsageFault escalating
     * to a boot lockup ("Lockup: can't escalate 3 to HardFault"). The barriers
     * make the enable visible before any subsequent FP issue. */
    volatile uint32_t *const cpacr = (volatile uint32_t *)0xE000ED88u;
    *cpacr |= (0xFu << 20);
    __asm__ volatile("dsb \n isb" ::: "memory");

    /* .bss = 0 */
    for (uint32_t *b = &_sbss; b < &_ebss; b++) {
        *b = 0u;
    }
    /* .data <- flash LMA */
    const uint32_t *s = &_sidata;
    for (uint32_t *d = &_sdata; d < &_edata; d++, s++) {
        *d = *s;
    }

    main();
    sh_exit(0);
}

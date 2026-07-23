#!/usr/bin/env python3
"""board-diag.py — halted-target register TRUTH for the STM32F429I-DISC1.

Everything here is a MEASUREMENT taken over the ST-LINK, never an inference
from the source. openocd runs once as a server and is driven from Python over
its TCL RPC port (6666), so every wait is a Python-side condition loop with a
deadline — there is no shell `sleep` and no openocd `sleep` anywhere.

Stages
------
 1. state of the RUNNING image  — halt (no reset), dump RCC/FLASH/PWR + PC/LR.
 2. silicon defaults            — `reset halt`, dump the same registers.
 3. HSE population probe        — start HSE with HSEBYP=0 (fitted crystal) and
                                  with HSEBYP=1 (external clock) and see which
                                  one, if either, asserts HSERDY. This decides
                                  empirically what is populated on the board.
 4. debugger-driven bring-up    — drive the whole 180 MHz sequence from the
                                  debugger for both HSI and HSE sources, then
                                  MEASURE the resulting core clock: park the
                                  core on a `b .` in SRAM, zero DWT_CYCCNT, run
                                  for a Python-timed aperture, halt, read the
                                  counter. SWS only says "PLL selected"; this
                                  says what the PLL actually multiplied.
 5. firmware verification       — flash the real board ELF, let it boot with
                                  ITS OWN clock code, wait until it is inside
                                  the timed render loop, then read the clock
                                  registers back out of silicon and gate
                                  DWT_CYCCNT again. This is the check that the
                                  numbers scripts/board-run.py reports were
                                  taken at 180 MHz / 5 WS / ART on.

Usage:
  python3 scripts/board-diag.py                 # stages 1-4
  python3 scripts/board-diag.py --verify-fw     # stages 1-5
  python3 scripts/board-diag.py --no-hse        # stages 1-2 only (read-only)

Log: tmp/board-diag.log   JSON: tmp/board-diag.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
LOG = TMP / "board-diag.log"
OOCD_LOG = TMP / "board-diag-openocd.log"

# THIS probe only. A second F42x board (STLINK-V3, 005600343431511837393330)
# shares the workstation and must never be driven by this repo's tooling.
STLINK_SERIAL = "0671FF484971754867174427"
BOARD_CFG = "board/stm32f429disc1.cfg"
TCL_PORT = 6666

# --- register map (RM0090) --------------------------------------------------
RCC_CR = 0x40023800
RCC_PLLCFGR = 0x40023804
RCC_CFGR = 0x40023808
RCC_APB1ENR = 0x40023840
FLASH_ACR = 0x40023C00
PWR_CR = 0x40007000
PWR_CSR = 0x40007004
DBGMCU_IDCODE = 0xE0042000
DEMCR = 0xE000EDFC
DWT_CTRL = 0xE0001000
DWT_CYCCNT = 0xE0001004

REGS = [
    ("RCC_CR", RCC_CR),
    ("RCC_PLLCFGR", RCC_PLLCFGR),
    ("RCC_CFGR", RCC_CFGR),
    ("FLASH_ACR", FLASH_ACR),
    ("PWR_CR", PWR_CR),
    ("PWR_CSR", PWR_CSR),
    ("DBGMCU_IDCODE", DBGMCU_IDCODE),
]

# Deadlines. Each is a bound on a *condition*, not a fixed wait: the loop exits
# the instant the condition holds.
#   HSE start-up: RM0090/DS8597 quote ~2 ms typical for the 8 MHz crystal.
#   PLL lock:     hundreds of us. Overdrive handshake: tens of us.
# 0.5 s is 250x the slowest of those, so a negative result is conclusive.
RDY_DEADLINE_S = 0.5
# Frequency gate apertures. DWT_CYCCNT counts ONLY while the core runs, so the
# figure is bracketed, never a point estimate: the core starts somewhere inside
# the `resume` round-trip and stops somewhere inside the `halt` round-trip, so
# the true run time lies in [h0-r1, h1-r0] and the true frequency in
# [cycles/(h1-r0), cycles/(h0-r1)]. The adapter round-trip is ~14 ms per edge,
# so the aperture has to be long for the bracket to be tight.
#   10 s parked: bracket ~+/-0.3 %, and at 180 MHz CYCCNT wraps at 23.9 s, so
#     10 s cannot alias.
#   5 s firmware: it must fit INSIDE the Exact tier's ~12.5 s timed render loop
#     (20 frames x ~0.62 s), which is the only stretch where the firmware
#     executes no semihosting and therefore is never halted under us.
GATE_PARKED_S = 10.0
GATE_FIRMWARE_S = 5.0
# openocd must publish its TCL port within this long or the adapter is gone.
CONNECT_DEADLINE_S = 20.0
# A wedged firmware must not hold the script forever.
FW_DEADLINE_S = 90.0


def now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class Log:
    def __init__(self, path: Path):
        self.fh = path.open("a")

    def __call__(self, msg: str) -> None:
        line = f"[{now()}] {msg}"
        print(line, flush=True)
        self.fh.write(line + "\n")
        self.fh.flush()


# --- openocd TCL RPC --------------------------------------------------------


class OpenOCD:
    """openocd as a server, driven over its TCL RPC socket.

    The RPC framing is: send `<command>\\x1a`, read until `\\x1a`. Commands run
    in openocd's TCL interpreter, so `read_memory`/`write_memory` return real
    TCL lists rather than the human-formatted `mdw` text.
    """

    SEP = b"\x1a"

    def __init__(self, log: Log):
        self.log = log
        OOCD_LOG.write_text("")
        self.proc = subprocess.Popen(
            [
                "openocd",
                "-c", f"adapter serial {STLINK_SERIAL}",
                "-f", BOARD_CFG,
                "-c", f"tcl_port {TCL_PORT}",
                "-c", "init",
            ],
            stdout=OOCD_LOG.open("ab"),
            stderr=subprocess.STDOUT,
            cwd=str(REPO),
        )
        self.sock = None
        t0 = time.monotonic()
        while time.monotonic() - t0 < CONNECT_DEADLINE_S:
            if self.proc.poll() is not None:
                raise SystemExit(
                    f"openocd exited rc={self.proc.returncode} before serving; "
                    f"see {OOCD_LOG}")
            try:
                s = socket.create_connection(("127.0.0.1", TCL_PORT), timeout=5)
                self.sock = s
                break
            except OSError:
                continue
        if self.sock is None:
            self.proc.kill()
            raise SystemExit(f"openocd never opened TCL port {TCL_PORT}; see {OOCD_LOG}")
        self.log(f"openocd up (pid {self.proc.pid}), TCL RPC on {TCL_PORT}")

    def cmd(self, command: str, timeout_s: float = 30.0) -> str:
        # Per-command deadline: a register read answers in ~1 ms, but
        # `flash write_image` of the ~200 KiB image takes ~15 s over an
        # ST-LINK/V2-1, so one global socket timeout cannot serve both.
        self.sock.settimeout(timeout_s)
        self.sock.sendall(command.encode() + self.SEP)
        buf = b""
        while not buf.endswith(self.SEP):
            chunk = self.sock.recv(65536)
            if not chunk:
                raise SystemExit("openocd closed the TCL connection")
            buf += chunk
        return buf[:-1].decode(errors="replace")

    # -- memory helpers ------------------------------------------------------
    def rd(self, addr: int) -> int:
        out = self.cmd(f"read_memory {addr:#x} 32 1")
        m = re.search(r"(0x[0-9a-fA-F]+|\d+)", out)
        if not m:
            raise SystemExit(f"read_memory {addr:#x} -> unparsable {out!r}")
        return int(m.group(1), 0) & 0xFFFFFFFF

    def wr(self, addr: int, val: int) -> None:
        self.cmd(f"write_memory {addr:#x} 32 {{{val:#x}}}")

    def wait_bit(self, addr: int, bit: int, deadline_s: float = RDY_DEADLINE_S):
        """Poll until (mem[addr] >> bit) & 1, or the deadline. Returns
        (ok, elapsed_s, last_value). No sleep: each poll is an adapter
        round-trip (~1 ms), so this samples ~500x across the deadline."""
        t0 = time.monotonic()
        v = 0
        while True:
            v = self.rd(addr)
            if (v >> bit) & 1:
                return True, time.monotonic() - t0, v
            if time.monotonic() - t0 >= deadline_s:
                return False, time.monotonic() - t0, v

    def close(self):
        try:
            self.cmd("shutdown")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        t0 = time.monotonic()
        while self.proc.poll() is None and time.monotonic() - t0 < 10:
            pass
        if self.proc.poll() is None:
            self.proc.kill()
        self.log(f"openocd down (rc={self.proc.returncode})")


# --- decoders ---------------------------------------------------------------


def dec_cr(v):
    return (f"HSION={v & 1} HSIRDY={(v >> 1) & 1} HSEON={(v >> 16) & 1} "
            f"HSERDY={(v >> 17) & 1} HSEBYP={(v >> 18) & 1} "
            f"PLLON={(v >> 24) & 1} PLLRDY={(v >> 25) & 1}")


def dec_cfgr(v):
    names = {0: "HSI", 1: "HSE", 2: "PLL", 3: "?"}
    return (f"SW={v & 3}({names[v & 3]}) SWS={(v >> 2) & 3}({names[(v >> 2) & 3]}) "
            f"HPRE={(v >> 4) & 0xF:#x} PPRE1={(v >> 10) & 7:#05b} "
            f"PPRE2={(v >> 13) & 7:#05b}")


def dec_pllcfgr(v):
    return (f"M={v & 0x3F} N={(v >> 6) & 0x1FF} P={((v >> 16) & 3) * 2 + 2} "
            f"Q={(v >> 24) & 0xF} SRC={'HSE' if (v >> 22) & 1 else 'HSI'}")


def dec_acr(v):
    return (f"LATENCY={v & 0xF}(WS) PRFTEN={(v >> 8) & 1} ICEN={(v >> 9) & 1} "
            f"DCEN={(v >> 10) & 1}")


def dec_pwr_cr(v):
    return f"VOS={(v >> 14) & 3} ODEN={(v >> 16) & 1} ODSWEN={(v >> 17) & 1}"


def dec_pwr_csr(v):
    return f"VOSRDY={(v >> 14) & 1} ODRDY={(v >> 16) & 1} ODSWRDY={(v >> 17) & 1}"


DEC = {
    "RCC_CR": dec_cr, "RCC_CFGR": dec_cfgr, "RCC_PLLCFGR": dec_pllcfgr,
    "FLASH_ACR": dec_acr, "PWR_CR": dec_pwr_cr, "PWR_CSR": dec_pwr_csr,
}


def dump(ocd: OpenOCD, log: Log, label: str) -> dict:
    got = {}
    log(f"  [{label}] registers:")
    for name, addr in REGS:
        v = ocd.rd(addr)
        got[name] = f"{v:#010x}"
        d = DEC.get(name)
        log(f"    {name:<14} @{addr:#010x} = {v:#010x}" + (f"   {d(v)}" if d else ""))
    return got


def where(ocd: OpenOCD, log: Log) -> dict:
    out = ocd.cmd("reg pc") + ocd.cmd("reg lr") + ocd.cmd("reg sp") + ocd.cmd("reg xPSR")
    regs = dict(re.findall(r"(pc|lr|sp|xPSR)\s*\(/32\)\s*:\s*(0x[0-9a-fA-F]+)", out))
    log(f"  core: pc={regs.get('pc')} lr={regs.get('lr')} sp={regs.get('sp')} "
        f"xPSR={regs.get('xPSR')}")
    return regs


# --- stage 3: HSE population ------------------------------------------------


def probe_hse(ocd: OpenOCD, log: Log, bypass: bool) -> dict:
    mode = "BYPASS (external clock on OSC_IN)" if bypass else "CRYSTAL (oscillator)"
    ocd.cmd("reset halt")
    # HSEBYP is writable only while HSEON is clear (RM0090 6.3.1): clear both,
    # then set the mode and start the oscillator in one write.
    ocd.wr(RCC_CR, 0x00000001)                       # HSION only
    ocd.wr(RCC_CR, 0x00050001 if bypass else 0x00010001)
    ok, secs, v = ocd.wait_bit(RCC_CR, 17)           # HSERDY
    log(f"  HSE {mode}: RCC_CR={v:#010x} -> HSERDY "
        f"{'ASSERTED after %.1f ms' % (secs * 1e3) if ok else 'NEVER asserted (%.0f ms polled)' % (secs * 1e3)}")
    return {"mode": "bypass" if bypass else "crystal", "hserdy": ok,
            "seconds": round(secs, 4), "rcc_cr": f"{v:#010x}"}


# --- stage 4: debugger-driven bring-up + measured clock ---------------------


def gate_clock(ocd: OpenOCD, log: Log, park: bool, gate_s: float) -> dict:
    """Measure the core clock by gating DWT_CYCCNT over a Python-timed aperture.

    `park=True` plants a `b .` at 0x20000000 and points PC at it, so nothing but
    the loop executes (used for the debugger-driven bring-up, where no firmware
    is running). `park=False` measures whatever the target is already doing —
    used to verify the firmware's own clock while it renders.

    Returns a BRACKET, because the exact instants the core started and stopped
    are only known to within the adapter round-trip (see GATE_PARKED_S).
    """
    ocd.cmd("halt")
    if park:
        ocd.wr(0x20000000, 0xE7FEE7FE)          # `b .` twice
        ocd.wr(DEMCR, ocd.rd(DEMCR) | (1 << 24))  # TRCENA
        ocd.wr(DWT_CYCCNT, 0)
        ocd.wr(DWT_CTRL, ocd.rd(DWT_CTRL) | 1)  # CYCCNTENA
        ocd.cmd("reg pc 0x20000001")            # thumb bit
    c0 = ocd.rd(DWT_CYCCNT)
    r0 = time.monotonic()
    ocd.cmd("resume")
    r1 = time.monotonic()
    # The aperture itself. This is the one place a fixed duration is correct:
    # it IS the measurement's time base.
    time.sleep(gate_s)
    h0 = time.monotonic()
    ocd.cmd("halt")
    h1 = time.monotonic()
    c1 = ocd.rd(DWT_CYCCNT)
    cycles = (c1 - c0) & 0xFFFFFFFF
    lo = cycles / (h1 - r0)          # longest possible run time -> lowest f
    hi = cycles / (h0 - r1)          # shortest possible run time -> highest f
    log(f"    gate: {cycles} cycles over an aperture bracketed to "
        f"[{(h0 - r1) * 1e3:.1f}, {(h1 - r0) * 1e3:.1f}] ms "
        f"-> core clock in [{lo / 1e6:.3f}, {hi / 1e6:.3f}] MHz")
    return {"cycles": cycles, "hz_low": lo, "hz_high": hi,
            "mhz_low": round(lo / 1e6, 3), "mhz_high": round(hi / 1e6, 3),
            "mhz_mid": round((lo + hi) / 2e6, 3), "gate_s": gate_s}


def bringup(ocd: OpenOCD, log: Log, src: str) -> dict:
    """Drive the full 180 MHz sequence from the debugger and measure the result."""
    log(f"  bring-up from {src.upper()}:")
    ocd.cmd("reset halt")
    hse_ok = None
    if src == "hse":
        ocd.wr(RCC_CR, 0x00000001)
        ocd.wr(RCC_CR, 0x00010001)              # HSEON, HSEBYP=0
        hse_ok, secs, _ = ocd.wait_bit(RCC_CR, 17)
        log(f"    HSERDY={hse_ok} after {secs * 1e3:.1f} ms")
        if not hse_ok:
            return {"src": src, "error": "HSERDY never asserted"}
        m, pllsrc = 8, 1 << 22
    else:
        m, pllsrc = 16, 0

    ocd.wr(RCC_APB1ENR, ocd.rd(RCC_APB1ENR) | (1 << 28))   # PWREN
    ocd.wr(PWR_CR, ocd.rd(PWR_CR) | (0b11 << 14))          # VOS = scale 1
    ocd.wr(PWR_CR, ocd.rd(PWR_CR) | (1 << 16))             # ODEN
    od, ods, _ = ocd.wait_bit(PWR_CSR, 16)                 # ODRDY
    ocd.wr(PWR_CR, ocd.rd(PWR_CR) | (1 << 17))             # ODSWEN
    odsw, odsws, _ = ocd.wait_bit(PWR_CSR, 17)             # ODSWRDY
    log(f"    overdrive: ODRDY={od} ({ods * 1e3:.1f} ms) ODSWRDY={odsw} ({odsws * 1e3:.1f} ms)")

    # M for a 1 MHz VCO ref, N=360 -> 360 MHz VCO, P=2 -> 180 MHz. PLLP encodes
    # /2 as 0b00, so it contributes no set bits.
    ocd.wr(RCC_PLLCFGR, m | (360 << 6) | pllsrc | (7 << 24))
    ocd.wr(RCC_CR, ocd.rd(RCC_CR) | (1 << 24))             # PLLON
    lock, locks, _ = ocd.wait_bit(RCC_CR, 25)              # PLLRDY
    log(f"    PLLRDY={lock} after {locks * 1e3:.1f} ms")
    if not lock:
        return {"src": src, "error": "PLL never locked"}

    # Flash latency + ART BEFORE the SYSCLK switch, never after.
    ocd.wr(FLASH_ACR, (1 << 10) | (1 << 9) | (1 << 8) | 5)
    ocd.wr(RCC_CFGR, (0b101 << 10) | (0b100 << 13))        # APB1/4, APB2/2, AHB/1
    ocd.wr(RCC_CFGR, ocd.rd(RCC_CFGR) | 0b10)              # SW = PLL
    t0 = time.monotonic()
    while ((ocd.rd(RCC_CFGR) >> 2) & 3) != 2:
        if time.monotonic() - t0 > RDY_DEADLINE_S:
            return {"src": src, "error": "SWS never became PLL"}

    gate = gate_clock(ocd, log, park=True, gate_s=GATE_PARKED_S)
    regs = dump(ocd, log, f"after {src} bring-up")
    out = {"src": src, "gate": gate, "regs": regs, "m": m, "n": 360, "p": 2}
    # The PLL input implied by the measurement — a wrong assumption about the
    # oscillator shows up here as a wrong input frequency, not a silent lie.
    out["implied_pll_input_mhz"] = [
        round(gate["hz_low"] * m * 2 / 360 / 1e6, 4),
        round(gate["hz_high"] * m * 2 / 360 / 1e6, 4),
    ]
    log(f"    -> core clock in [{gate['mhz_low']}, {gate['mhz_high']}] MHz, "
        f"implied PLL input {out['implied_pll_input_mhz']} MHz (M={m} N=360 P=2)")
    return out


# --- stage 5: verify the FIRMWARE's own clock -------------------------------


def verify_firmware(ocd: OpenOCD, log: Log, elf: Path) -> dict:
    """Flash the real board image, let its own clock_init run, then read the
    clock registers back out of silicon while it is inside the timed loop and
    gate DWT_CYCCNT to confirm the core really is at 180 MHz."""
    log(f"  flashing {elf} via openocd")
    ocd.cmd("reset halt")
    out = ocd.cmd(f"flash write_image erase {elf.as_posix()}", timeout_s=180.0)
    for ln in out.strip().splitlines():
        log(f"    {ln}")
    ocd.cmd("reset halt")
    ocd.cmd("arm semihosting enable")
    mark = OOCD_LOG.stat().st_size
    ocd.cmd("resume")

    # Wait until the firmware is INSIDE the timed render loop. The workload
    # prints the glyph-cache line immediately before that loop and emits no
    # semihosting at all inside it, so once that text appears the core is
    # running free for the whole 12 s Exact window — the only safe place to
    # take a 1 s gate without the debugger's own halts stealing cycles.
    t0 = time.monotonic()
    seen = ""
    while "glyph cache" not in seen:
        if time.monotonic() - t0 > FW_DEADLINE_S:
            log(f"    ERROR: firmware never reached the timed loop in "
                f"{FW_DEADLINE_S}s — see {OOCD_LOG}")
            return {"error": "firmware never reached the timed loop",
                    "captured": seen}
        with OOCD_LOG.open("r", errors="replace") as fh:
            fh.seek(mark)
            seen = fh.read()
    log(f"    firmware entered the timed loop after {time.monotonic() - t0:.1f} s")

    gate = gate_clock(ocd, log, park=False, gate_s=GATE_FIRMWARE_S)
    regs = dump(ocd, log, "firmware's own clock config, read from silicon")
    ocd.cmd("resume")

    # Let it finish so the semihosting report (hash, cycles/frame) is captured.
    t0 = time.monotonic()
    while "workload ok" not in seen:
        if time.monotonic() - t0 > FW_DEADLINE_S:
            log("    firmware did not report 'workload ok' before the guard")
            break
        with OOCD_LOG.open("r", errors="replace") as fh:
            fh.seek(mark)
            seen = fh.read()

    acr = int(regs["FLASH_ACR"], 16)
    cfgr = int(regs["RCC_CFGR"], 16)
    res = {
        "elf": str(elf),
        "gate": gate,
        "regs": regs,
        "on_pll": ((cfgr >> 2) & 3) == 2,
        "flash_wait_states": acr & 0xF,
        "art_prefetch": (acr >> 8) & 1,
        "art_icache": (acr >> 9) & 1,
        "art_dcache": (acr >> 10) & 1,
        "semihosting": [ln for ln in seen.splitlines() if "vyr-size" in ln],
    }
    for ln in res["semihosting"]:
        log(f"    fw| {ln.strip()}")
    return res


# --- main -------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-hse", action="store_true",
                    help="dump registers only; never write RCC")
    ap.add_argument("--verify-fw", action="store_true",
                    help="also flash the board ELF and verify ITS clock in silicon")
    ap.add_argument("--elf", default=str(TMP / "board-exact.elf"))
    a = ap.parse_args()

    TMP.mkdir(exist_ok=True)
    log = Log(LOG)
    log("=" * 70)
    log(f"board-diag: STM32F429I-DISC1 via ST-LINK {STLINK_SERIAL}")
    result = {"when": now(), "probe_serial": STLINK_SERIAL, "stages": {}}

    ocd = OpenOCD(log)
    try:
        log("--- stage 1: state of the RUNNING image (halt, no reset) ---")
        ocd.cmd("halt")
        result["stages"]["running"] = {"regs": dump(ocd, log, "running"),
                                       "core": where(ocd, log)}

        log("--- stage 2: silicon defaults (reset halt) ---")
        ocd.cmd("reset halt")
        result["stages"]["reset"] = {"regs": dump(ocd, log, "reset"),
                                     "core": where(ocd, log)}

        if not a.no_hse:
            log("--- stage 3: HSE population probe (empirical) ---")
            xtal = probe_hse(ocd, log, bypass=False)
            byp = probe_hse(ocd, log, bypass=True)
            # Re-test crystal AFTER bypass: the two writes must be independent,
            # and a one-shot result that does not reproduce is not a result.
            xtal2 = probe_hse(ocd, log, bypass=False)
            verdict = ("FITTED CRYSTAL (HSEBYP must be 0)"
                       if xtal["hserdy"] and xtal2["hserdy"] and not byp["hserdy"]
                       else "EXTERNAL CLOCK (HSEBYP must be 1)"
                       if byp["hserdy"] and not xtal["hserdy"]
                       else "both modes ready — unusual" if byp["hserdy"] and xtal["hserdy"]
                       else "HSE DOES NOT START IN EITHER MODE — HSI only")
            log(f"  VERDICT: {verdict}")
            result["stages"]["hse_probe"] = {"crystal": xtal, "bypass": byp,
                                             "crystal_repeat": xtal2,
                                             "verdict": verdict}

            log("--- stage 4: 180 MHz bring-up driven from the debugger ---")
            result["stages"]["bringup"] = {
                "hsi": bringup(ocd, log, "hsi"),
                "hse": bringup(ocd, log, "hse"),
            }

        if a.verify_fw:
            log("--- stage 5: verify the FIRMWARE's own clock in silicon ---")
            elf = Path(a.elf)
            if not elf.exists():
                log(f"  ERROR: {elf} does not exist — run scripts/board-run.py first")
                result["stages"]["firmware"] = {"error": f"missing {elf}"}
            else:
                result["stages"]["firmware"] = verify_firmware(ocd, log, elf)

        # Leave the part halted in its reset state, not mid-experiment.
        ocd.cmd("reset halt")
    finally:
        ocd.close()

    dest = TMP / "board-diag.json"
    dest.write_text(json.dumps(result, indent=2) + "\n")
    log(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

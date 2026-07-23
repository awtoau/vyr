#!/usr/bin/env python3
"""lcd-docs.py — establish the STM32F429I-DISC1 panel facts from ST's own
board-support sources, not from memory.

Issue #28 asks for the controller + interface + pin mapping to be CONFIRMED
for this board revision before any register code is written. The authoritative
machine-readable statement of that mapping is ST's own BSP for the board
(STM32CubeF4 `Drivers/BSP/STM32F429I-Discovery/`), which names every LCD pin
and the exact ILI9341 command list ST ships for this panel.

Fetches (read-only, public):
  * stm32f429i_discovery.h      — SPI5 + LCD control pin definitions
  * stm32f429i_discovery_lcd.h  — LCD layer/BSP declarations
  * ili9341.h                   — the controller's register map + ID
  * ili9341.c                   — ST's init sequence for THIS panel

Then greps out the load-bearing lines and writes them to tmp/lcd-docs.log so
the pin numbers in vyr-size/src/lcd.rs have a citable origin.
"""
import datetime
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
TMP = os.path.join(REPO, "tmp")
CACHE = os.path.join(TMP, "lcd-docs")

# NOTE: inside STM32CubeF4 these BSP directories are git SUBMODULES (the tree
# lists them as bare entries with no children), so the raw.githubusercontent
# path under STM32CubeF4 404s. The real content lives in the standalone
# component repos below — same files, same vendor.
RAW = "https://raw.githubusercontent.com"
BSP = f"{RAW}/STMicroelectronics/32f429idiscovery-bsp/main"
ILI = f"{RAW}/STMicroelectronics/stm32-ili9341/main"
SOURCES = {
    "stm32f429i_discovery.h": f"{BSP}/stm32f429i_discovery.h",
    "stm32f429i_discovery_lcd.h": f"{BSP}/stm32f429i_discovery_lcd.h",
    "stm32f429i_discovery_lcd.c": f"{BSP}/stm32f429i_discovery_lcd.c",
    "stm32f429i_discovery_sdram.h": f"{BSP}/stm32f429i_discovery_sdram.h",
    "stm32f429i_discovery_sdram.c": f"{BSP}/stm32f429i_discovery_sdram.c",
    "ili9341.h": f"{ILI}/ili9341.h",
    "ili9341.c": f"{ILI}/ili9341.c",
}

# What we need out of each file, as (file, label, regex) triples.
WANTED = [
    ("stm32f429i_discovery.h", "SPI5 / LCD pin map",
     r"^\s*#define\s+(DISCOVERY_SPI\w*|LCD_(NCS|WRX|RDX|RESET|BL)\w*|"
     r"LCD_\w*_(PIN|PORT|GPIO_CLK\w*))\b.*$"),
    ("stm32f429i_discovery.h", "SPI5 peripheral + AF",
     r"^\s*#define\s+DISCOVERY_SPIx\w*\s+.*$"),
    ("ili9341.h", "controller identity / geometry",
     r"^\s*#define\s+(ILI9341_ID|ILI9341_LCD_PIXEL_(WIDTH|HEIGHT)|"
     r"LCD_REG_\d+)\b.*$"),
    ("ili9341.c", "ST's init sequence for this panel",
     r"^\s*(ili9341_Write(Reg|Data)|LCD_IO_Write\w*)\s*\(.*$"),
    ("stm32f429i_discovery_lcd.c", "LCD IO / interface selection",
     r"^\s*(LCD_IO_\w+|.*RGB.*interface.*)\s*[\(;].*$"),
]


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def fetch(name, url, logf):
    path = os.path.join(CACHE, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        logf.write(f"[{now()}] cached {path}\n")
        return path
    r = subprocess.run(["curl", "-sSL", "-o", path, url],
                       capture_output=True, text=True)
    ok = r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 0
    logf.write(f"[{now()}] fetch {url} -> {'OK' if ok else 'FAIL'} "
               f"{r.stderr.strip()}\n")
    return path if ok else None


def main():
    os.makedirs(CACHE, exist_ok=True)
    with open(os.path.join(TMP, "lcd-docs.log"), "w") as logf:
        logf.write(f"[{now()}] lcd-docs: STM32F429I-DISC1 panel provenance\n")
        got = {}
        for name, url in SOURCES.items():
            p = fetch(name, url, logf)
            if p:
                got[name] = open(p, errors="replace").read()
        logf.write("\n" + "=" * 72 + "\n")
        for fname, label, pat in WANTED:
            text = got.get(fname)
            logf.write(f"\n--- {fname}: {label} ---\n")
            if not text:
                logf.write("  (file not fetched)\n")
                continue
            rx = re.compile(pat, re.M)
            hits = [m.group(0).rstrip() for m in rx.finditer(text)]
            if not hits:
                logf.write("  (no match)\n")
            for h in hits[:200]:
                logf.write("  " + h.strip() + "\n")
        logf.flush()
    print(open(os.path.join(TMP, "lcd-docs.log")).read())
    return 0


if __name__ == "__main__":
    sys.exit(main())

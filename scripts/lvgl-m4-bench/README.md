# lvgl-m4-bench — bare-metal LVGL benchmark on the emulated Cortex-M4

A self-contained bare-metal LVGL benchmark that runs the SAME 480×270 scene as
vyr's `FIXTURE_IR` on the SAME emulated silicon vyr uses
(`qemu-system-arm -machine netduinoplus2`, STM32F405/Cortex-M4F), so the
instruction-count and memory numbers compare apples-to-apples. See
[`compare.md`](compare.md) for the result table and every caveat.

## Files

| file | what |
|---|---|
| `startup.c` | fresh-written generic ARMv7-M boot: vector table, crt0 (FPU enable, `.data` copy, `.bss` zero), ARM semihosting (SYS_WRITE0 / SYS_EXIT / SYS_CLOCK). Mirrors the clean pattern in `vyr-size/src/m4.rs` but shares no code. |
| `m4.ld` | F405 memory map (FLASH 1M, SRAM 128K, CCM 64K) — mirrors `vyr-size/link-qemu.ld`. |
| `lv_conf.h` | the HONEST embedded LVGL v9.6 config: builtin malloc pool, RGB888, software renderer, partial 480×16 draw buffer, all heavyweight 3rd-party libs OFF. NOT the runner's 64 MB desktop pool. |
| `main.c` | builds the scene via the LVGL C API, renders one frame as 480×16 partial bands, a flush_cb folds each band into an FNV-1a hash, reports `lv_mem_monitor` + insns/frame + dims via semihosting. |
| `run.py` | builds (arm-none-eabi-gcc + the upstream LVGL mirror) and runs under qemu with a Python wall-clock guard; parses the numbers; writes `tmp/lvgl-m4-result.json`. |

## Where LVGL comes from

**The read-only upstream mirror**, never a fork and never a nested submodule of
a port project:

```
/mnt/2tb/git_mirror/lvgl        →  github.com/lvgl/lvgl
```

**Not pinned** — the anchor should track current upstream, so just pull the
mirror before a run. What matters is that the version is *recorded*, not fixed:
every run stamps the commit, `git describe`, date, remote, and the semantic
version into both the log and `tmp/lvgl-m4-result.json`. If the tree is dirty
or ahead of `origin/master`, the run prints a loud `*** NOT STOCK ***` line —
an anchor whose provenance is not recorded is not an anchor.

Refresh and run:

```
git -C /mnt/2tb/git_mirror/lvgl pull --ff-only     # take latest upstream
python3 scripts/lvgl-m4-bench/run.py               # default 64 KiB pool
python3 scripts/lvgl-m4-bench/run.py --mem-kb 16   # right-sized pool sweep
python3 scripts/lvgl-m4-bench/run.py --lvgl-root /some/other/lvgl   # one-off
```

Logs: `tmp/lvgl-m4-bench.log` (timestamped build/run), `tmp/lvgl-m4.log`
(captured semihosting console), `tmp/lvgl-m4-result.json` (machine-readable,
stamped with the LVGL provenance). Build objects land in `tmp/lvgl-m4-obj/`
(regenerable). Needs `arm-none-eabi-gcc` (Fedora distro toolchain) and
`qemu-system-arm` on PATH.

## IP boundary

Every file here is awto-written; **no LVGL source lives in this repo.** LVGL is
consumed the way any dependency is — headers included from the mirror at build
time, sources compiled from that external path — the same relationship vyr has
with tiny-skia. Nothing is vendored, and `.gitignore` keeps build objects out.

`lv_conf.h` is the one file to be aware of: a config header necessarily
enumerates LVGL's own `LV_*` macro names (244 of its 265 defines match LVGL's
`lv_conf_template.h`, because that IS the integration surface). The values,
comments, and structure are ours. LVGL is MIT, so this is compatible with vyr's
GPL-3.0-only + commercial licensing.

**TouchGFX is out entirely** (proprietary; no TGX code built, linked, or
derived). See `compare.md` for the honest TGX position.

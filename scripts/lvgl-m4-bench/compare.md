# vyr vs LVGL on the SAME emulated Cortex-M4 — the first apples-to-apples

**Measured 2026-06-12.** Both renderers run the SAME 480×270 scene on the SAME
emulated silicon — `qemu-system-arm -machine netduinoplus2` (STM32F405, Cortex-
M4F) under `-icount shift=0,sleep=off` (1 virtual ns = 1 guest instruction, so
SYS_CLOCK centisecond deltas ARE deterministic instruction counts, 1 cs = 10⁷
insns). Same machine, same memory map (FLASH 1 MiB @ 0x08000000, SRAM 128 KiB
@ 0x20000000, CCM 64 KiB @ 0x10000000), same banding (480×16 horizontal bands),
same semihosting methodology.

- **vyr** column: `python3 /home/dan/git/vyr/dev.py qemu-m4`
  (`vyr/tmp/qemu-m4.log`) — vyr-size `--features run-qemu`, the F9 dynamic half.
- **LVGL** column: `python3 scripts/lvgl-m4-bench/run.py`
  (`tmp/lvgl-m4.log`) — LVGL v9.6.0-dev (the vendored vyvanse-runner tree),
  built bare-metal for thumbv7em with a fresh C harness (vector table, crt0,
  FPU enable, semihosting) and an M4-tuned `lv_conf.h` (builtin malloc pool,
  RGB888, software renderer, partial 480×16 draw buffer, all heavyweight
  3rd-party libs OFF). The flush_cb folds each band into an FNV-1a hash; there
  is no real display.
- **TouchGFX** column: see "The honest TGX position" below.

## The table

| metric | vyr (M4 banded) | LVGL (M4 bare-metal) | TouchGFX (M4) |
|---|--:|--:|---|
| **instructions / frame** | **75.0 M** (579 insn/px) | **~10.0–10.25 M** (77–79 insn/px) | not benchmarkable in-repo¹ |
| **ms/frame @180 MHz (ESTIMATE²)** | ~417 ms | ~56 ms | — |
| **heap / pool PEAK** | 106,409 B (counting-alloc) | **8,152 B** (lv_mem high-water) | — |
| **draw / band buffer** | 23,040 B (CCM static, off-heap) | 23,040 B (SRAM .bss) | — |
| **flash (.text+.rodata)** | 448,269 B | **192,672 B** | ST publishes per-board figures¹ |
| **static RAM (.bss)** | 145,952 B³ | 91,692 B⁴ | — |
| **color depth** | RGB888 (24bpp) | RGB888 (24bpp) | typically RGB565 on F4 |
| **frame hash (own)** | `0x6b0c51567a991741` | `0xe7a75d89a00badec` | — |
| **what the frame contains** | panel, label×3, checker image, gauge (tiny-skia arc), LCD text, slider×2, progress, toggle, line — Roboto-ASCII subset font, unhinted AA | panel, label×3, checker image, scale+arc gauge, slider×2, bar, switch, line — Montserrat built-in font, LVGL AA | (proprietary; not built) |

¹ ² ³ ⁴ — see notes below.

### The headline reading

- **LVGL is ~7.3–7.5× fewer instructions per frame** on this scene (10.0 M vs
  75.0 M). That is the expected shape: LVGL is a mature, hand-tuned fixed-point
  C renderer with a simpler AA model; vyr renders through **tiny-skia**'s
  general-purpose floating-point coverage pipeline (the F9 static doc already
  flagged tiny-skia + skrifa generality as the flash cost; here it shows up as
  the per-frame instruction cost too). This is a SYSTEM comparison of two very
  different renderer architectures, not a like-for-like of the same algorithm.
- **LVGL's dynamic pool peak is tiny (8.2 KB)** because LVGL's fonts are
  compile-time `.rodata` bitmaps (no heap font copy) and its widgets are small
  structs; the heavy per-band scratch is the draw buffer, priced separately.
  vyr's 106 KB heap peak includes its 8 KB heap font copy + image asset + parse
  tree + the **63 KB in-band premultiplied-RGBA gutter pixmap** (tiny-skia's
  internal scratch). Different memory architectures: vyr's working set is
  heap-dominated and transient; LVGL's is a small pool + a static draw buffer +
  static font ROM. **Both fit the F405's 128 KiB SRAM with room to spare.**
- **LVGL's flash is ~2.3× smaller** (193 KB vs 448 KB) for this build. vyr's
  448 KB carries tiny-skia + skrifa + read-fonts + serde_json + the 8 KB subset
  font; LVGL's 193 KB is the v9 core + the handful of enabled widgets + the
  Montserrat 12/14/20 bitmaps. Note both numbers are honest LINKED ELFs with
  `--gc-sections`/dead-code elimination, not rlib overcounts.

## Notes & caveats (read these — the numbers are honest only with them)

¹ **TouchGFX is NOT benchmarkable in-repo.** TouchGFX is proprietary
  (ST/Draupner). We do **not** build, link, or derive any TGX code, and derive
  nothing from any disassembly — that is a hard IP boundary for this repo (vyr
  stays clean-room; the benchmark lives in awto-vyvanse which vendors only
  MIT-licensed LVGL). A clean M4 TGX number cannot be produced here. The only
  clean public reference is that **ST publishes per-board "MCU load" / FPS
  figures for TouchGFX** in its board datasheets and AN5179-class application
  notes (e.g. the STM32F4/F7/H7 TouchGFX benchmarks ST distributes with the
  X-CUBE-TOUCHGFX package). We cite that such figures exist; we do **not**
  fabricate a value, and ST's figures are not measured on this qemu machine or
  this scene, so they would not be comparable even if quoted.

² **Every ms/frame is an ESTIMATE labelled hard.** icount counts
  *instructions*, not M4 *cycles*. The ms numbers assume CPI = 1.0, which a real
  M4 does not have (flash wait states, write buffers, no dual-issue). The
  instruction COUNTS are deterministic (icount); the ms translation is not.
  Real-silicon CPI calibration is exactly the open BOARD half of vyr's F9.
  There is also a ±1 cs quantization on each reading (LVGL measured 40–41 cs
  over 40 frames across runs ⇒ 10.0–10.25 M insns/frame).

³ **vyr's 145,952 B .bss is measurement scaffolding, not renderer RAM.** It is
  dominated by the run-qemu vehicle's **120 KiB counting-heap arena** + the
  23 KB CCM band buffer + statics. vyr-core itself owns ~12 B of static RAM by
  design (all working memory is heap or caller-provided). The honest "RAM the
  renderer needs" for vyr is the **heap peak (106 KB) + band buffer (23 KB)**.

⁴ **LVGL's 91,692 B .bss is the reserved 64 KiB pool + 23 KB draw buffer + the
  2.3 KB checker image + small statics.** The pool is RESERVED at 64 KiB but
  only **8,152 B is ever used** (high-water): a 16 KiB pool runs the identical
  scene with the identical hash and instruction count (verified). So LVGL's
  honest minimum working RAM for this scene is **~8 KB pool + 23 KB draw buffer
  + 2.3 KB image ≈ 34 KB** — versus vyr's ~129 KB. Much of that gap is the
  tiny-skia gutter-pixmap architecture vs LVGL's direct-to-band blitter.

### Divergences (this is NOT a pixel-identical comparison)

The two frame hashes **deliberately differ** — they are per-renderer
determinism anchors, not a cross-check. Each is byte-stable across re-renders
on its own side. The scenes diverge in ways the two vocabularies force:

- **Font face differs.** vyr renders a Roboto-ASCII subset (unhinted, vyr's
  own AA). LVGL renders its built-in **Montserrat** bitmaps (12/14/20 px). The
  glyph shapes, metrics, and AA are entirely different faces — text pixels
  cannot match.
- **Widget defaults / AA differ.** vyr's `vy_gauge` is a tiny-skia arc; the
  LVGL analogue is `lv_scale` (round ticks) + an `lv_arc` indicator — a
  different visual. Slider/switch/bar geometry, corner radii, border AA, and
  default theme styling are LVGL's, not vyr's. The toggle is an `lv_switch`;
  the progress is an `lv_bar`.
- **Image content differs.** Both blit a 24×24 checker, but vyr's is the F6
  test checker RGBA; LVGL's is a synthesized 6×6 grey checker built in the
  harness (no PNG decoder linked — LODEPNG is OFF on bare metal).
- **Color depth matched on purpose.** Both render RGB888 (24bpp) so the
  per-pixel cost compares like-for-like. Real F4 panels are usually RGB565;
  an RGB565 LVGL build would be cheaper still (noted, not measured here).

## Reproduce

```
# vyr column (run in the vyr repo):
python3 /home/dan/git/vyr/dev.py qemu-m4          # -> vyr/tmp/qemu-m4.log

# LVGL column (run in awto-vyvanse):
python3 scripts/lvgl-m4-bench/run.py              # -> tmp/lvgl-m4.log
python3 scripts/lvgl-m4-bench/run.py --mem-kb 16  # prove the 8 KB high-water
```

Both use the identical qemu line:
`qemu-system-arm -machine netduinoplus2 -nographic -semihosting-config
enable=on,target=native -icount shift=0,sleep=off -kernel <elf>`.

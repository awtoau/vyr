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
| **instructions / frame, RENDER ONLY⁵** | Exact **48.24 M** (372 insn/px) · Fast **33.51 M** (259) · Draft **5.51 M** (42.5) | **3.22 M** (24.9 insn/px) | not benchmarkable in-repo¹ |
| **instructions / frame, with the harness fold⁵** | Exact 51.35 M · Fast 36.62 M · Draft 8.62 M | 7.11 M | — |
| **ms/frame @192 MHz (ESTIMATE²)** | Exact ~251 ms · Draft ~29 ms | ~17 ms | — |
| **heap / pool PEAK** | 106,409 B (counting-alloc) | **8,152 B** (lv_mem high-water) | — |
| **draw / band buffer** | 23,040 B (CCM static, off-heap) | 23,040 B (SRAM .bss) | — |
| **flash (.text+.rodata)** | 448,269 B | **192,672 B** | ST publishes per-board figures¹ |
| **static RAM (.bss)** | 145,952 B³ | 91,692 B⁴ | — |
| **color depth** | RGB888 (24bpp) | RGB888 (24bpp) | typically RGB565 on F4 |
| **frame hash (own)** | `0x6b0c51567a991741` | `0xe7a75d89a00badec` | — |
| **what the frame contains** | panel, label×3, checker image, gauge (tiny-skia arc), LCD text, slider×2, progress, toggle, line — Roboto-ASCII subset font, unhinted AA | panel, label×3, checker image, scale+arc gauge, slider×2, bar, switch, line — Montserrat built-in font, LVGL AA | (proprietary; not built) |

⁵ **Render-only is measured, not subtracted (#44).** Since 889543f both
harnesses render without folding inside the timed window — the fold moved to an
untimed verification pass that must reproduce the reference hash first — and each
runs a *second* timed pass with the fold so `total` and `fold` are measured in
the same cell. Instrument: qemu + the `libinsn` TCG plugin (exact instruction
counts) on both sides. Reproduce: `python3 scripts/fold-split-check.py`.

¹ ² ³ ⁴ — see notes below.

### The headline reading

- **LVGL is 15.0× fewer instructions per frame than vyr Exact, and vyr's own
  Draft tier still costs 1.71× LVGL** (3.22 M render-only against 5.51 M) while
  drawing *less* — no anti-aliasing, square corners. Read the render-only row,
  not the with-fold one: the FNV hash both harnesses use to prove the frame was
  materialised is a fixed ~3–3.9 M/frame, so it inflates a cheap frame far more
  than an expensive one and a "total" comparison is substantially a comparison
  of two FNV loops (#31, #44). That is the expected shape: LVGL is a mature,
  hand-tuned fixed-point C renderer with a simpler AA model; vyr renders
  through **tiny-skia**'s
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

### Divergences — the audited list (2026-07-23, #27 Task B)

The two frame hashes **deliberately differ** — they are per-renderer
determinism anchors, not a cross-check. What follows is the *audit*: every
content difference found by rendering both frames and diffing them per widget
rect (`python3 scripts/fidelity-compare.py`, `docs/quality-tiers/`), ranked by
how many pixels it moves. **9.6 % of the frame (12,459 / 129,600 px) still
differs between vyr Exact and LVGL, and this list is why.** Until the entries
marked OPEN are closed, no cross-renderer ratio taken from these frames is
publishable as a like-for-like.

**CLOSED — the two that were distorting the measurement**

| what | was | now |
|---|---|---|
| **checker image** | the harness synthesised its OWN 6x6 checkerboard of two greys (0xC8/0x40) while vyr blitted the real coloured asset — a content difference sitting inside a renderer comparison | `checker-24.inc`, generated on every build from **`vyr-size/assets/checker-24.rgba`**, the same bytes vyr blits, in LVGL's ARGB8888 order. The widget now differs by **117 px with max channel delta 1** — LSB-level blend rounding, nothing else |
| **gauge** | `lv_scale` (ROUND_INNER tick marks + 0/50/100 numeric labels) stacked with a value `lv_arc` (65 % sweep + drag knob), against vyr's plain full ring. This is the region every #27 quality number is taken in, and the extra elements inflated LVGL's distinct-colour count without any extra edge quality — the exact trap the issue's first reading fell into | one `lv_arc`, full 0-360 background ring, indicator and knob set to `LV_OPA_TRANSP`. Removing that content made LVGL **23 % cheaper** (9,220,422 → 7,112,541 insns/frame), so the previously published "vyr Draft beats LVGL by 8.05 %" had been scored against an LVGL doing more work |

**OPEN — known, quantified, not fixed**

| what | vyr | LVGL | px moved |
|---|---|---|--:|
| **slider / progress track colour** | `#E6E6E6` (vyr's own `TRACK` constant) | `#213C52` — its default dark theme's blended track | 3,302 + 2,042 + 660 |
| **slider knob** | white disc + 1 px `#B0B0B0` ring | a filled accent-blue circle, no ring | (in the above) |
| **frame border placement** | `stroke_rrect` is **centred** on the contour, so a 1 px border straddles the edge at half coverage each side | drawn fully **inside** the rect | 3,098 |
| **font face + size** | Roboto ASCII subset, 14 px default / 20 px LCD; the footer label has no size attr so it renders at 14 | Montserrat 12/14/20 — the footer is explicitly 12 px, and lands 2 rows higher | 1,155 + 999 + 467 + 373 |
| **arc radius** | ring centre radius 50, covering r ∈ [44.5, 55.5]; row 131 spans x 23-134 | `(min(w,h) - arc_width)/2` = 49.5 — **half a pixel inward**; row 131 spans x 24-133 | 932 |
| **toggle knob** | disc + ring, as the slider | `lv_switch` knob, theme-styled | 262 |

`vy_line` matches **exactly** (0 differing pixels), which is the useful control:
where the two vocabularies agree on geometry and colour, the renderers agree on
pixels.

**Not closeable in this harness**

- **Color depth matched on purpose.** Both render RGB888 (24bpp) so per-pixel
  cost compares like-for-like. Real F4 panels are usually RGB565; an RGB565
  LVGL build would be cheaper still (noted, not measured here).
- **Text.** Matching the faces means porting Roboto into LVGL's bitmap font
  format at three sizes. Worth doing before any text-heavy claim; it is the
  single largest remaining px-mover after the theme colours.

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

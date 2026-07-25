/* lvgl-m4-bench/main.c — the LVGL frame, built via the LVGL v9 C API directly,
 * on bare-metal qemu netduinoplus2 (STM32F405/M4F). The SAME 480x270 scene as
 * vyr's FIXTURE_IR (vyr-size/src/workload.rs), rendered as 480x16 partial bands
 * (matching vyr's banding) into a draw buffer; a flush_cb folds every band's
 * bytes into a streaming FNV-1a hash (no real display).
 *
 * Reports via semihosting: lv_mem_monitor (used/peak/frag), draw-buffer bytes,
 * instruction count per frame (SYS_CLOCK delta under -icount), frame dims, and
 * the frame hash.
 *
 * Divergences from vyr (documented in compare.md): Montserrat font not the
 * Roboto subset; LVGL widget defaults/anti-aliasing differ; this is a SYSTEM
 * comparison, not pixel-identical. The hash is LVGL's own — it is NOT expected
 * to match vyr's; it is a determinism/regression anchor for the LVGL side.
 */

#include "lvgl/lvgl.h"
#include <stdint.h>
#include <stddef.h>

/* ---- semihosting bridge (defined in startup.c) ---- */
void    sh_write0(const char *s);
int32_t sh_clock_cs(void);
void    sh_exit(int ok);

#ifdef DUMP_FRAME
/* Host-file semihosting, compiled ONLY into the --dump-frame build (see
 * startup.c). Lets the fidelity comparison show a REAL LVGL image of this
 * scene instead of asserting what it would look like. The measurement ELF is
 * built without -DDUMP_FRAME and contains none of it. */
int sh_open_wb(const char *name);
int sh_write(int handle, const void *buf, uint32_t len);
int sh_close(int handle);
#ifndef DUMP_FRAME_PATH
#define DUMP_FRAME_PATH "lvgl-frame.rgb888"
#endif
/* -1 = not dumping. Set only around the dedicated dump render, so the timed
 * loop never pays for file I/O even in this build. */
static int g_dump_fd = -1;
#endif

/* LVGL assert handler hook (referenced by lv_conf.h LV_ASSERT_HANDLER). */
void lvm4_assert_fail(void)
{
    sh_write0("FATAL [lvgl-m4] LV_ASSERT failed — exiting 1\n");
    sh_exit(0);
}

/* ---- tiny no-libc print helpers (semihosting SYS_WRITE0 wants a C string) -- */

static char g_line[256];

static char *put_str(char *p, const char *s)
{
    while (*s) {
        *p++ = *s++;
    }
    return p;
}

static char *put_u32(char *p, uint32_t v)
{
    char tmp[12];
    int  n = 0;
    if (v == 0) {
        *p++ = '0';
        return p;
    }
    while (v) {
        tmp[n++] = (char)('0' + (v % 10));
        v /= 10;
    }
    while (n) {
        *p++ = tmp[--n];
    }
    return p;
}

/* Used only by the -DVERIFY build's hash lines (#45); harmless in the perf
 * build, where it is simply not referenced. */
__attribute__((unused))
static char *put_hex64(char *p, uint64_t v)
{
    static const char hexd[] = "0123456789abcdef";
    *p++ = '0';
    *p++ = 'x';
    for (int i = 60; i >= 0; i -= 4) {
        *p++ = hexd[(v >> i) & 0xF];
    }
    return p;
}

/* ---- scene geometry: the SAME 480x270 frame, 480x16 bands ---- */

#define FRAME_W 480
#define FRAME_H 270
#define BAND_H  16
/* RGB888 draw buffer: LVGL XRGB/RGB888 stores 3 bytes/px at depth 24. */
#define BAND_PX_BYTES 3
#define DRAW_BUF_BYTES (FRAME_W * BAND_H * BAND_PX_BYTES)

/* The draw buffer is a normal .bss static -> SRAM (NOT CCM like vyr's band
 * buffer; documented divergence). 4-aligned for LV_DRAW_BUF_ALIGN. */
static uint8_t g_draw_buf[DRAW_BUF_BYTES] __attribute__((aligned(4)));

/* ---- 24x24 checker image: vyr's OWN asset, byte for byte (#27) ----
 *
 * This used to be a locally-invented 6x6 checkerboard of two greys, which
 * meant the published side-by-side had LVGL drawing a grey mock-up where vyr
 * drew the coloured 24x24 test asset — a CONTENT difference sitting inside a
 * RENDERER comparison. checker-24.inc is generated on every build by
 * run.py::gen_checker_header() straight from vyr-size/assets/checker-24.rgba,
 * the same bytes vyr's FIXTURE_IR blits, converted to LVGL's ARGB8888 memory
 * order. The asset carries real alpha (a semi-transparent quadrant and a fully
 * transparent centre), so the format is ARGB8888, not XRGB8888 — otherwise
 * LVGL would draw the transparent pixels opaque and the divergence would just
 * move. */
#include "checker-24.inc"
static lv_image_dsc_t g_checker_dsc;

static void checker_init(void)
{
    g_checker_dsc.header.magic  = LV_IMAGE_HEADER_MAGIC;
    g_checker_dsc.header.cf     = LV_COLOR_FORMAT_ARGB8888;
    g_checker_dsc.header.w      = CK_W;
    g_checker_dsc.header.h      = CK_H;
    g_checker_dsc.header.stride = CK_W * 4;
    g_checker_dsc.data          = g_checker_px;
    g_checker_dsc.data_size     = sizeof(g_checker_px);
}

/* ---- FNV-1a streaming hash over every flushed band byte ---- */

static uint32_t       g_bands_flushed;
static uint32_t       g_px_flushed;
/* #45: the hash is VERIFICATION, compiled ONLY under -DVERIFY. The perf build
 * (no -DVERIFY) has no FNV constants, no fold, no hash line — the counterpart of
 * vyr's `verify` feature. #44 kept the fold out of the timed window; that was
 * procedural. This keeps it out of the binary, so the perf number cannot contain
 * the benchmark's own hash by construction. run.py builds both: verify to prove
 * the frame bytes, perf to measure. */
#ifdef VERIFY
static uint64_t       g_hash;
static const uint64_t FNV_OFFSET = 0xcbf29ce484222325ULL;
static const uint64_t FNV_PRIME  = 0x100000001b3ULL;
#endif

static void flush_cb(lv_display_t *disp, const lv_area_t *area, uint8_t *px_map)
{
    int32_t w = lv_area_get_width(area);
    int32_t h = lv_area_get_height(area);
    uint32_t n = (uint32_t)w * (uint32_t)h * BAND_PX_BYTES;
    /* The barrier is UNCONDITIONAL and load-bearing — the C counterpart of
     * vyr's `core::hint::black_box`. It consumes the band pointer and clobbers
     * memory, so LVGL's writes into px_map must be committed before this point
     * even in the perf build where nothing reads them afterwards. The FNV fold
     * is verify-only (#45). */
    __asm__ volatile("" : : "r"(px_map), "r"(n) : "memory");
#ifdef VERIFY
    /* Fold the exact bytes LVGL produced for this band into the hash. The
     * draw buffer is tight (stride == w*3) for our partial bands. */
    for (uint32_t i = 0; i < n; i++) {
        g_hash ^= px_map[i];
        g_hash *= FNV_PRIME;
    }
#endif
#ifdef DUMP_FRAME
    /* Bands arrive top-to-bottom and full-width in LV_DISPLAY_RENDER_MODE_
     * PARTIAL with this buffer size, so appending each band's tight bytes
     * yields the whole frame in raster order. run.py asserts the byte count
     * is exactly FRAME_W*FRAME_H*3 rather than trusting that. */
    if (g_dump_fd >= 0) {
        (void)sh_write(g_dump_fd, px_map, n);
    }
#endif
    g_bands_flushed++;
    g_px_flushed += (uint32_t)w * (uint32_t)h;
    lv_display_flush_ready(disp);
}

/* ---- build the scene: the SAME widgets as vyr FIXTURE_IR ----
 *
 * vyr FIXTURE_IR (480x270, bg #22262B):
 *   vy_frame  12,10  456x44   panel  bg #2E3440 r8 border #4C566A
 *   vy_label  28,22          "Compressor 2 - line B"  #ECEFF4
 *   vy_image  428,20  24x24   checker
 *   vy_gauge  24,76  110x110           -> lv_arc, FULL RING, no knob/ticks
 *   vy_lcd    44,196  90x24  "1480"  #A3BE8C (20px)  -> styled label
 *   vy_slider 180,92  260x18  value 62
 *   vy_slider 180,128 260x18  value 35
 *   vy_progress 180,164 260x12 value 80  -> lv_bar
 *   vy_toggle 180,196  56x28  value 1   -> lv_switch (on)
 *   vy_label  248,202  "bypass" #D8DEE9
 *   vy_line   12,236  456x2  #4C566A
 *   vy_label  16,246  "awto / vyr on emulated M4"  #7A869A
 */

static lv_obj_t *mk_label(lv_obj_t *parent, int x, int y, const char *txt,
                          uint32_t color, const lv_font_t *font)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_label_set_text(l, txt);
    lv_obj_set_pos(l, x, y);
    lv_obj_set_style_text_color(l, lv_color_hex(color), 0);
    if (font) {
        lv_obj_set_style_text_font(l, font, 0);
    }
    return l;
}

static void build_scene(void)
{
    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x22262B), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);

    /* Header panel (vy_frame) */
    lv_obj_t *panel = lv_obj_create(scr);
    lv_obj_set_pos(panel, 12, 10);
    lv_obj_set_size(panel, 456, 44);
    lv_obj_set_style_bg_color(panel, lv_color_hex(0x2E3440), 0);
    lv_obj_set_style_radius(panel, 8, 0);
    lv_obj_set_style_border_width(panel, 1, 0);
    lv_obj_set_style_border_color(panel, lv_color_hex(0x4C566A), 0);
    lv_obj_set_style_pad_all(panel, 0, 0);

    mk_label(scr, 28, 22, "Compressor 2 - line B", 0xECEFF4, NULL);

    /* Checker image (vy_image) */
    lv_obj_t *img = lv_image_create(scr);
    lv_image_set_src(img, &g_checker_dsc);
    lv_obj_set_pos(img, 428, 20);

    /* Gauge (vy_gauge) -> a PLAIN FULL RING, because that is what vyr draws.
     *
     * #27 Task B: this was an lv_scale (ROUND_INNER: tick marks and 0/50/100
     * numeric labels) stacked with a value lv_arc (a 65% sweep plus a drag
     * knob). vyr's vy_gauge lowers to ONE full circular ring — no ticks, no
     * labels, no knob, no partial sweep. The gauge region is where every
     * quality measurement in #27 is taken, so a content mismatch THERE
     * contaminated the whole comparison: extra elements inflate a colour
     * count without any extra edge quality, which is precisely how the
     * original "LVGL has 116 distinct colours" reading went wrong.
     *
     * vyr's geometry (ir.rs, vy_gauge): d = min(w,h) = 110, stroke = d/10 = 11,
     * radius = d/2 - stroke/2 = 50, centred at (79, 131) — so the ring covers
     * r in [44.5, 55.5]. LVGL places its arc at radius (min(w,h) - arc_width)/2
     * = 49.5, i.e. HALF A PIXEL inward; that residual is documented, not
     * fixable through the public style API. */
    lv_obj_t *arc = lv_arc_create(scr);
    lv_obj_set_pos(arc, 24, 76);
    lv_obj_set_size(arc, 110, 110);
    lv_obj_remove_flag(arc, LV_OBJ_FLAG_CLICKABLE);
    lv_arc_set_bg_angles(arc, 0, 360);
    lv_obj_set_style_arc_color(arc, lv_color_hex(0x88C0D0), LV_PART_MAIN);
    lv_obj_set_style_arc_width(arc, 11, LV_PART_MAIN);
    lv_obj_set_style_arc_opa(arc, LV_OPA_COVER, LV_PART_MAIN);
    /* The value indicator and the knob are vyr-absent: switch them off rather
     * than restyle them, so nothing of them can reach the pixels. */
    lv_obj_set_style_arc_opa(arc, LV_OPA_TRANSP, LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(arc, LV_OPA_TRANSP, LV_PART_KNOB);
    lv_obj_set_style_pad_all(arc, 0, LV_PART_KNOB);

    /* LCD-ish value (vy_lcd) — styled 20px label */
    mk_label(scr, 44, 196, "1480", 0xA3BE8C, &lv_font_montserrat_20);

    /* Two sliders (vy_slider) */
    lv_obj_t *s1 = lv_slider_create(scr);
    lv_obj_set_pos(s1, 180, 92);
    lv_obj_set_size(s1, 260, 18);
    lv_slider_set_range(s1, 0, 100);
    lv_slider_set_value(s1, 62, LV_ANIM_OFF);

    lv_obj_t *s2 = lv_slider_create(scr);
    lv_obj_set_pos(s2, 180, 128);
    lv_obj_set_size(s2, 260, 18);
    lv_slider_set_range(s2, 0, 100);
    lv_slider_set_value(s2, 35, LV_ANIM_OFF);

    /* Progress bar (vy_progress) */
    lv_obj_t *bar = lv_bar_create(scr);
    lv_obj_set_pos(bar, 180, 164);
    lv_obj_set_size(bar, 260, 12);
    lv_bar_set_range(bar, 0, 100);
    lv_bar_set_value(bar, 80, LV_ANIM_OFF);

    /* Toggle (vy_toggle) -> switch, ON */
    lv_obj_t *sw = lv_switch_create(scr);
    lv_obj_set_pos(sw, 180, 196);
    lv_obj_set_size(sw, 56, 28);
    lv_obj_add_state(sw, LV_STATE_CHECKED);

    mk_label(scr, 248, 202, "bypass", 0xD8DEE9, NULL);

    /* Separator line (vy_line) */
    static lv_point_precise_t line_pts[2];
    line_pts[0].x = 12;  line_pts[0].y = 237;
    line_pts[1].x = 468; line_pts[1].y = 237;
    lv_obj_t *line = lv_line_create(scr);
    lv_line_set_points(line, line_pts, 2);
    lv_obj_set_style_line_color(line, lv_color_hex(0x4C566A), 0);
    lv_obj_set_style_line_width(line, 2, 0);

    mk_label(scr, 16, 246, "awto / vyr on emulated M4", 0x7A869A,
             &lv_font_montserrat_12);
}

/* Monotonic ms for LVGL, derived from SYS_CLOCK (centiseconds of virtual
 * time). The scene is static so the exact value never affects the render;
 * LVGL only needs a monotonic source for its timer bookkeeping. */
static uint32_t lvm4_tick_ms(void)
{
    return (uint32_t)sh_clock_cs() * 10u;
}

/* ---- report a single "key=val" structured line ---- */
static void emit_kv(const char *prefix, const char *k, uint32_t v)
{
    char *p = g_line;
    p = put_str(p, prefix);
    p = put_str(p, k);
    p = put_u32(p, v);
    p = put_str(p, "\n");
    *p = '\0';
    sh_write0(g_line);
}

int main(void)
{
    sh_write0("INFO  [lvgl-m4] boot: netduinoplus2 (STM32F405/M4F), crt0 + FPU done\n");

    checker_init();

    lv_init();
    lv_tick_set_cb(lvm4_tick_ms);

    lv_display_t *disp = lv_display_create(FRAME_W, FRAME_H);
    lv_display_set_color_format(disp, LV_COLOR_FORMAT_RGB888);
    lv_display_set_buffers(disp, g_draw_buf, NULL, DRAW_BUF_BYTES,
                           LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(disp, flush_cb);
    lv_display_set_default(disp);

    build_scene();

    sh_write0("INFO  [lvgl-m4] scene built (480x270, 480x16 partial bands, RGB888)\n");

    /* --- frame 1: full render, capture hash (verify) + bands + peak --- */
#ifdef VERIFY
    g_hash          = FNV_OFFSET;
#endif
    g_bands_flushed = 0;
    g_px_flushed    = 0;
    lv_refr_now(disp);

    lv_mem_monitor_t mon;
    lv_mem_monitor(&mon);

    emit_kv("INFO  [lvgl-m4] ", "draw_buf_bytes=", DRAW_BUF_BYTES);
    emit_kv("INFO  [lvgl-m4] ", "frame_w=", FRAME_W);
    emit_kv("INFO  [lvgl-m4] ", "frame_h=", FRAME_H);
    emit_kv("INFO  [lvgl-m4] ", "band_h=", BAND_H);
    emit_kv("INFO  [lvgl-m4] ", "bands_flushed=", g_bands_flushed);
    emit_kv("INFO  [lvgl-m4] ", "pixels_flushed=", g_px_flushed);
    emit_kv("INFO  [lvgl-m4] ", "lv_mem_total=", (uint32_t)mon.total_size);
    emit_kv("INFO  [lvgl-m4] ", "lv_mem_used_now=",
            (uint32_t)(mon.total_size - mon.free_size));
    emit_kv("ALERT [lvgl-m4] ", "lv_mem_peak=", (uint32_t)mon.max_used);
    emit_kv("INFO  [lvgl-m4] ", "lv_mem_frag_pct=", (uint32_t)mon.frag_pct);

#ifdef VERIFY
    {
        char *p = g_line;
        p = put_str(p, "INFO  [lvgl-m4] frame fnv1a=");
        p = put_hex64(p, g_hash);
        p = put_str(p, "\n");
        *p = '\0';
        sh_write0(g_line);
    }
    /* Captured HERE, not at the timed section: the optional DUMP_FRAME render
     * below folds another frame into g_hash without resetting it. */
    const uint64_t first_hash = g_hash;
#endif

#ifdef DUMP_FRAME
    /* --- fidelity dump: one extra full render, bands appended to a host file.
     * Done AFTER the reported frame-1 numbers and BEFORE the timed loop, so it
     * perturbs neither. #27 side-by-side. */
    {
        int fd = sh_open_wb(DUMP_FRAME_PATH);
        if (fd < 0) {
            sh_write0("FATAL [lvgl-m4] dump: semihosting SYS_OPEN failed\n");
            sh_exit(0);
        }
        g_dump_fd       = fd;
        g_px_flushed    = 0;
        g_bands_flushed = 0;
        lv_obj_invalidate(lv_screen_active());
        lv_refr_now(disp);
        g_dump_fd = -1;
        sh_close(fd);
        emit_kv("INFO  [lvgl-m4] ", "dumped_pixels=", g_px_flushed);
        emit_kv("INFO  [lvgl-m4] ", "dumped_bands=", g_bands_flushed);
        sh_write0("ALERT [lvgl-m4] frame dumped to " DUMP_FRAME_PATH "\n");
    }
#endif

    /* --- timed: re-render N frames, force a full redraw each time ---
     * Static scene means LVGL would otherwise flush nothing (no dirty area), so
     * we invalidate the whole screen before each refresh to measure a full
     * frame's instruction cost — the apples-to-apples with vyr's warmed frames
     * (vyr re-renders the whole banded frame each timed iteration). */
    /* 40 frames: each full-frame LVGL render is ~1 cs, so a handful would
     * quantize hard against the 1 cs SYS_CLOCK resolution. 40 puts the reading
     * in the tens-of-cs range for a stable insns/frame. */
    /* Overridable at build time (-DTIMED_FRAMES=N) so the measurement window
     * can be lengthened. The SYS_CLOCK counter has 1 cs granularity, so the
     * quantisation error is 1/N_cs — at the 40-frame default that is ~2.5%,
     * which is the same order as the vyr-vs-LVGL gap being measured. Longer
     * runs shrink it proportionally. */
#ifndef TIMED_FRAMES
#define TIMED_FRAMES 40
#endif
    const uint32_t TIMED_FRAMES_N = TIMED_FRAMES;

#ifdef VERIFY
    /* --- the VERIFY build: prove the frame, do not time it (#45). A warmed
     * re-render WITH the fold must reproduce the first frame's hash; a mismatch
     * is fatal. The perf build below contains none of this. */
    (void)TIMED_FRAMES_N;
    g_hash          = FNV_OFFSET;
    g_bands_flushed = 0;
    lv_obj_invalidate(lv_screen_active());
    lv_refr_now(disp);
    {
        char *p = g_line;
        p = put_str(p, "INFO  [lvgl-m4] warmed frame fnv1a=");
        p = put_hex64(p, g_hash);
        p = put_str(p, "\n");
        *p = '\0';
        sh_write0(g_line);
    }
    if (g_hash != first_hash) {
        sh_write0("ERROR [lvgl-m4] warmed frame hash != first frame hash — "
                  "non-deterministic re-render\n");
        sh_exit(0);
    }
    sh_write0("INFO  [lvgl-m4] verify: warmed frame hash reproduced (-DVERIFY)\n");
#else
    /* --- the PERF build: a SINGLE render-only timed pass (#45). There is no
     * fold in this binary, so render_only is the whole timed cost, structurally,
     * not a difference of two passes. */
    int32_t t0 = sh_clock_cs();
    for (uint32_t i = 0; i < TIMED_FRAMES_N; i++) {
        g_bands_flushed = 0;
        lv_obj_invalidate(lv_screen_active());
        lv_refr_now(disp);
    }
    int32_t t1 = sh_clock_cs();
    uint32_t render_only = (uint32_t)(t1 - t0);

    emit_kv("INFO  [lvgl-m4] timed_frames=", "", TIMED_FRAMES_N);
    emit_kv("INFO  [lvgl-m4] timed_cs=", "", render_only);
    emit_kv("INFO  [lvgl-m4] render_only_cs=", "", render_only);
    sh_write0("INFO  [lvgl-m4] fold=absent-by-build (#45)\n");
#endif

    sh_write0("ALERT [lvgl-m4] workload ok\n");
    sh_exit(1);
    return 0;
}

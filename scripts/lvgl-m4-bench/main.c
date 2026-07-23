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

/* ---- 24x24 checker image (the vyr scene's checker-24 analogue) ---- */
/* Built at compile time as an XRGB8888 (LVGL's native true-color) descriptor:
 * a 6x6 checker of two greys, matching the spirit of vyr's 24x24 checker. */
#define CK_W 24
#define CK_H 24
static uint8_t g_checker_px[CK_W * CK_H * 4];
static lv_image_dsc_t g_checker_dsc;

static void checker_init(void)
{
    for (int y = 0; y < CK_H; y++) {
        for (int x = 0; x < CK_W; x++) {
            int      cell = ((x / 4) + (y / 4)) & 1;
            uint8_t  g    = cell ? 0xC8 : 0x40;
            uint8_t *p    = &g_checker_px[(y * CK_W + x) * 4];
            p[0] = g;     /* B */
            p[1] = g;     /* G */
            p[2] = g;     /* R */
            p[3] = 0xFF;  /* A/X */
        }
    }
    g_checker_dsc.header.magic  = LV_IMAGE_HEADER_MAGIC;
    g_checker_dsc.header.cf     = LV_COLOR_FORMAT_XRGB8888;
    g_checker_dsc.header.w      = CK_W;
    g_checker_dsc.header.h      = CK_H;
    g_checker_dsc.header.stride = CK_W * 4;
    g_checker_dsc.data          = g_checker_px;
    g_checker_dsc.data_size     = sizeof(g_checker_px);
}

/* ---- FNV-1a streaming hash over every flushed band byte ---- */

static uint64_t       g_hash;
static uint32_t       g_bands_flushed;
static uint32_t       g_px_flushed;
static const uint64_t FNV_OFFSET = 0xcbf29ce484222325ULL;
static const uint64_t FNV_PRIME  = 0x100000001b3ULL;

static void flush_cb(lv_display_t *disp, const lv_area_t *area, uint8_t *px_map)
{
    int32_t w = lv_area_get_width(area);
    int32_t h = lv_area_get_height(area);
    /* Fold the exact bytes LVGL produced for this band into the hash. The
     * draw buffer is tight (stride == w*3) for our partial bands. */
    uint32_t n = (uint32_t)w * (uint32_t)h * BAND_PX_BYTES;
    for (uint32_t i = 0; i < n; i++) {
        g_hash ^= px_map[i];
        g_hash *= FNV_PRIME;
    }
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
 *   vy_gauge  24,76  110x110  value 65  -> lv_scale (round) + needle
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

    /* Gauge (vy_gauge) -> a round scale + an arc to suggest the value */
    lv_obj_t *scale = lv_scale_create(scr);
    lv_obj_set_pos(scale, 24, 76);
    lv_obj_set_size(scale, 110, 110);
    lv_scale_set_mode(scale, LV_SCALE_MODE_ROUND_INNER);
    lv_scale_set_range(scale, 0, 100);
    lv_obj_t *arc = lv_arc_create(scr);
    lv_obj_set_pos(arc, 24, 76);
    lv_obj_set_size(arc, 110, 110);
    lv_arc_set_range(arc, 0, 100);
    lv_arc_set_value(arc, 65);
    lv_obj_set_style_arc_color(arc, lv_color_hex(0x88C0D0), LV_PART_INDICATOR);

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

    /* --- frame 1: full render, capture hash + bands + peak --- */
    g_hash          = FNV_OFFSET;
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

    {
        char *p = g_line;
        p = put_str(p, "INFO  [lvgl-m4] frame fnv1a=");
        p = put_hex64(p, g_hash);
        p = put_str(p, "\n");
        *p = '\0';
        sh_write0(g_line);
    }

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
    int32_t t0 = sh_clock_cs();
    for (uint32_t i = 0; i < TIMED_FRAMES_N; i++) {
        g_hash          = FNV_OFFSET;
        g_bands_flushed = 0;
        lv_obj_invalidate(lv_screen_active());
        lv_refr_now(disp);
    }
    int32_t t1 = sh_clock_cs();

    emit_kv("INFO  [lvgl-m4] timed_frames=", "", TIMED_FRAMES_N);
    emit_kv("INFO  [lvgl-m4] timed_cs=", "", (uint32_t)(t1 - t0));
    {
        char *p = g_line;
        p = put_str(p, "INFO  [lvgl-m4] warmed frame fnv1a=");
        p = put_hex64(p, g_hash);
        p = put_str(p, "\n");
        *p = '\0';
        sh_write0(g_line);
    }

    sh_write0("ALERT [lvgl-m4] workload ok\n");
    sh_exit(1);
    return 0;
}

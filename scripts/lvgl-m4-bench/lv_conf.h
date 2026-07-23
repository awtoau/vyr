/**
 * @file lv_conf.h  (lvgl-m4-bench)
 * The HONEST embedded LVGL v9.6 config for the bare-metal STM32F405/M4F
 * benchmark — NOT the vyvanse-runner's 64 MB desktop pool.
 *
 * Tuned to compare like-for-like with vyr's M4 banded render:
 *  - LV_COLOR_DEPTH 24 (RGB888) — vyr renders RGB888; match it.
 *  - LV_USE_OS NONE, software renderer, builtin malloc with a SMALL pool
 *    (the pool high-water mark is REPORTED by the harness via lv_mem_monitor).
 *  - Partial draw buffer sized to a 480x16 band (matches vyr's banding); the
 *    harness sets the draw buffer + a flush_cb that folds each band into a
 *    streaming FNV-1a hash (no real display).
 *  - All heavyweight 3rd-party libs OFF (lodepng, tjpgd, freetype, ThorVG,
 *    SVG, QR, barcode, vector graphics, file systems): the scene is built via
 *    the LVGL C API directly, so none of these are needed, and dropping them
 *    keeps flash + the libc surface minimal on bare metal.
 *  - LV_USE_LOG OFF (no printf dependency on bare metal).
 *
 * Divergences vs vyr documented in compare.md (font is Montserrat not the
 * Roboto subset; widget defaults differ; this is a SYSTEM comparison).
 */

/* clang-format off */
#if 1
#ifndef LV_CONF_H
#define LV_CONF_H

/*====================
   COLOR SETTINGS
 *====================*/
/* 24 = RGB888, to compare like-for-like with vyr's RGB888 render. */
#define LV_COLOR_DEPTH 24

/*=========================
   STDLIB WRAPPER SETTINGS
 *=========================*/
/* Builtin everything: no newlib malloc/printf needed on bare metal. */
#define LV_USE_STDLIB_MALLOC    LV_STDLIB_BUILTIN
#define LV_USE_STDLIB_STRING    LV_STDLIB_BUILTIN
#define LV_USE_STDLIB_SPRINTF   LV_STDLIB_BUILTIN

#define LV_STDINT_INCLUDE       <stdint.h>
#define LV_STDDEF_INCLUDE       <stddef.h>
#define LV_STDBOOL_INCLUDE      <stdbool.h>
#define LV_INTTYPES_INCLUDE     <inttypes.h>
#define LV_LIMITS_INCLUDE       <limits.h>
#define LV_STDARG_INCLUDE       <stdarg.h>

#if LV_USE_STDLIB_MALLOC == LV_STDLIB_BUILTIN
    /* The HONEST embedded pool. Start at 64 KB; the harness reports the
     * high-water mark (lv_mem_monitor.max_used) so we know the real need.
     * Tunable via -DLV_MEM_SIZE_KB=NN from the build (run.py sweeps it). */
    #ifndef LV_MEM_SIZE_KB
        #define LV_MEM_SIZE_KB 64
    #endif
    #define LV_MEM_SIZE (LV_MEM_SIZE_KB * 1024U)
    #define LV_MEM_POOL_EXPAND_SIZE 0
    #define LV_MEM_ADR 0
    #if LV_MEM_ADR == 0
        #undef LV_MEM_POOL_INCLUDE
        #undef LV_MEM_POOL_ALLOC
    #endif
#endif

/*====================
   HAL SETTINGS
 *====================*/
#define LV_DEF_REFR_PERIOD  33
#define LV_DPI_DEF 130

/*=================
 * OPERATING SYSTEM
 *=================*/
#define LV_USE_OS   LV_OS_NONE

/*========================
 * RENDERING CONFIGURATION
 *========================*/
#define LV_DRAW_BUF_STRIDE_ALIGN                1
#define LV_DRAW_BUF_ALIGN                       4
#define LV_DRAW_TRANSFORM_USE_MATRIX            0
#define LV_DRAW_LAYER_SIMPLE_BUF_SIZE    (8 * 1024)
#define LV_DRAW_LAYER_MAX_MEMORY 0
#define LV_DRAW_THREAD_STACK_SIZE    (8 * 1024)
#define LV_DRAW_THREAD_PRIO LV_THREAD_PRIO_HIGH

#define LV_USE_DRAW_SW 1
#if LV_USE_DRAW_SW == 1
    #define LV_DRAW_SW_SUPPORT_RGB565       1
    #define LV_DRAW_SW_SUPPORT_RGB565_SWAPPED       0
    #define LV_DRAW_SW_SUPPORT_RGB565A8     0
    #define LV_DRAW_SW_SUPPORT_RGB888       1
    #define LV_DRAW_SW_SUPPORT_XRGB8888     1
    #define LV_DRAW_SW_SUPPORT_ARGB8888     1
    #define LV_DRAW_SW_SUPPORT_ARGB8888_PREMULTIPLIED 1
    #define LV_DRAW_SW_SUPPORT_L8           1
    #define LV_DRAW_SW_SUPPORT_AL88         0
    #define LV_DRAW_SW_SUPPORT_A8           1
    #define LV_DRAW_SW_SUPPORT_I1           0
    #define LV_DRAW_SW_I1_LUM_THRESHOLD 127
    #define LV_DRAW_SW_DRAW_UNIT_CNT    1
    #define LV_USE_DRAW_ARM2D_SYNC      0
    #define LV_USE_NATIVE_HELIUM_ASM    0
    #define LV_DRAW_SW_COMPLEX          1
    #if LV_DRAW_SW_COMPLEX == 1
        #define LV_DRAW_SW_SHADOW_CACHE_SIZE 0
        #define LV_DRAW_SW_CIRCLE_CACHE_SIZE 4
    #endif
    #define  LV_USE_DRAW_SW_ASM     LV_DRAW_SW_ASM_NONE
    #define LV_USE_DRAW_SW_COMPLEX_GRADIENTS    0
#endif

#define LV_USE_NEMA_GFX 0
#define LV_USE_PXP 0
#define LV_USE_G2D 0
#define LV_USE_DRAW_DAVE2D 0
#define LV_USE_DRAW_SDL 0
#define LV_USE_DRAW_VG_LITE 0
#define LV_USE_DRAW_DMA2D 0
#define LV_USE_DRAW_OPENGLES 0
#define LV_USE_PPA  0
#define LV_USE_DRAW_EVE 0
#define LV_USE_DRAW_NANOVG 0

/*=======================
 * FEATURE CONFIGURATION
 *=======================*/

/*-------------
 * Logging — OFF (no printf on bare metal)
 *-----------*/
#define LV_USE_LOG 0

/*-------------
 * Asserts — keep NULL/MALLOC (very fast), drop the slow ones
 *-----------*/
#define LV_USE_ASSERT_NULL          1
#define LV_USE_ASSERT_MALLOC        1
#define LV_USE_ASSERT_STYLE         0
#define LV_USE_ASSERT_MEM_INTEGRITY 0
#define LV_USE_ASSERT_OBJ           0
#define LV_ASSERT_HANDLER_INCLUDE <stdint.h>
/* On assert, call our loud bare-metal halt (defined in harness). */
extern void lvm4_assert_fail(void);
#define LV_ASSERT_HANDLER lvm4_assert_fail();

#define LV_USE_CHECK_ARG 0
#define LV_CHECK_ARG_ASSERT_ON_FAIL 0

/*-------------
 * Debug
 *-----------*/
#define LV_USE_REFR_DEBUG 0
#define LV_USE_LAYER_DEBUG 0
#define LV_USE_PARALLEL_DRAW_DEBUG 0

/*-------------
 * Others
 *-----------*/
#define LV_ENABLE_GLOBAL_CUSTOM 0
#define LV_CACHE_DEF_SIZE       0
#define LV_IMAGE_HEADER_CACHE_DEF_CNT 0
#define LV_GRADIENT_MAX_STOPS   2
#define LV_COLOR_MIX_ROUND_OFS  0
#define LV_OBJ_STYLE_CACHE      1
#define LV_USE_OBJ_ID           0
#define LV_USE_OBJ_NAME         0
#define LV_USE_XML              0
#define LV_OBJ_ID_AUTO_ASSIGN   0
#define LV_USE_OBJ_ID_BUILTIN   0
#define LV_USE_OBJ_PROPERTY 0
#define LV_USE_OBJ_PROPERTY_NAME 0
#define LV_USE_GESTURE_RECOGNITION 0

/*=====================
 *  COMPILER SETTINGS
 *====================*/
#define LV_BIG_ENDIAN_SYSTEM 0
#define LV_ATTRIBUTE_TICK_INC
#define LV_ATTRIBUTE_TIMER_HANDLER
#define LV_ATTRIBUTE_FLUSH_READY
#define LV_ATTRIBUTE_SYNC_READY
#define LV_ATTRIBUTE_MEM_ALIGN_SIZE 1
#define LV_ATTRIBUTE_MEM_ALIGN
#define LV_ATTRIBUTE_LARGE_CONST
#define LV_ATTRIBUTE_LARGE_RAM_ARRAY
#define LV_ATTRIBUTE_FAST_MEM
#define LV_EXPORT_CONST_INT(int_value) struct _silence_gcc_warning
#define LV_ATTRIBUTE_EXTERN_DATA
/* M4F has a single-precision FPU; LVGL float ops use it. */
#define LV_USE_FLOAT            1
#define LV_USE_MATRIX           0
#ifndef LV_USE_PRIVATE_API
    #define LV_USE_PRIVATE_API  1
#endif

/*==================
 *   FONT USAGE
 *===================*/
/* Only the sizes the scene needs (and the default). Montserrat — the closest
 * LVGL built-in to vyr's Roboto subset; the FACE differs (documented). */
#define LV_FONT_MONTSERRAT_8  0
#define LV_FONT_MONTSERRAT_10 0
#define LV_FONT_MONTSERRAT_12 1
#define LV_FONT_MONTSERRAT_14 1
#define LV_FONT_MONTSERRAT_16 0
#define LV_FONT_MONTSERRAT_18 0
#define LV_FONT_MONTSERRAT_20 1
#define LV_FONT_MONTSERRAT_22 0
#define LV_FONT_MONTSERRAT_24 0
#define LV_FONT_MONTSERRAT_26 0
#define LV_FONT_MONTSERRAT_28 0
#define LV_FONT_MONTSERRAT_30 0
#define LV_FONT_MONTSERRAT_32 0
#define LV_FONT_MONTSERRAT_34 0
#define LV_FONT_MONTSERRAT_36 0
#define LV_FONT_MONTSERRAT_38 0
#define LV_FONT_MONTSERRAT_40 0
#define LV_FONT_MONTSERRAT_42 0
#define LV_FONT_MONTSERRAT_44 0
#define LV_FONT_MONTSERRAT_46 0
#define LV_FONT_MONTSERRAT_48 0
#define LV_FONT_MONTSERRAT_28_COMPRESSED    0
#define LV_FONT_DEJAVU_16_PERSIAN_HEBREW    0
#define LV_FONT_SOURCE_HAN_SANS_SC_14_CJK   0
#define LV_FONT_SOURCE_HAN_SANS_SC_16_CJK   0
#define LV_FONT_UNSCII_8  0
#define LV_FONT_UNSCII_16 0
#define LV_FONT_CUSTOM_DECLARE
#define LV_FONT_DEFAULT &lv_font_montserrat_14
#define LV_FONT_FMT_TXT_LARGE 0
#define LV_USE_FONT_COMPRESSED 0
#define LV_USE_FONT_PLACEHOLDER 1

/*=================
 *  TEXT SETTINGS
 *=================*/
#define LV_TXT_ENC LV_TXT_ENC_UTF8
#define LV_TXT_BREAK_CHARS " ,.;:-_)]}"
#define LV_TXT_LINE_BREAK_LONG_LEN 0
#define LV_TXT_LINE_BREAK_LONG_PRE_MIN_LEN 3
#define LV_TXT_LINE_BREAK_LONG_POST_MIN_LEN 3
#define LV_USE_BIDI 0
#define LV_USE_ARABIC_PERSIAN_CHARS 0
#define LV_TXT_COLOR_CMD "#"

/*==================
 * WIDGETS
 *================*/
#define LV_WIDGETS_HAS_DEFAULT_VALUE  1

#define LV_USE_ANIMIMG    0
#define LV_USE_ARC        1   /* gauge/scale uses arc */
#define LV_USE_ARCLABEL   0
#define LV_USE_BAR        1   /* slider + progress + switch base */
#define LV_USE_BUTTON     1
#define LV_USE_BUTTONMATRIX  0
#define LV_USE_CALENDAR   0
#define LV_USE_CANVAS     0
#define LV_USE_CHART      0
#define LV_USE_CHECKBOX   0
#define LV_USE_DROPDOWN   0
#define LV_USE_IMAGE      1   /* the checker image blit */
#define LV_USE_IMAGEBUTTON     0
#define LV_USE_KEYBOARD   0
#define LV_USE_LABEL      1
#if LV_USE_LABEL
    #define LV_LABEL_TEXT_SELECTION 0
    #define LV_LABEL_LONG_TXT_HINT 0
    #define LV_LABEL_WAIT_CHAR_COUNT 3
#endif
#define LV_USE_LED        0
#define LV_USE_LINE       1   /* the separator line */
#define LV_USE_LIST       0
#define LV_USE_LOTTIE     0
#define LV_USE_MENU       0
#define LV_USE_MSGBOX     0
#define LV_USE_ROLLER     0
#define LV_USE_SCALE      1   /* the gauge (vyr vy_gauge) */
#define LV_USE_SLIDER     1
#define LV_USE_SPAN       0
#define LV_USE_SPINBOX    0
#define LV_USE_SPINNER    0
#define LV_USE_SWITCH     1   /* the toggle */
#define LV_USE_TABLE      0
#define LV_USE_TABVIEW    0
#define LV_USE_TEXTAREA   0
#define LV_USE_TILEVIEW   0
#define LV_USE_WIN        0
#define LV_USE_3DTEXTURE  0

/*==================
 * THEMES
 *==================*/
#define LV_USE_THEME_DEFAULT 1
#if LV_USE_THEME_DEFAULT
    #define LV_THEME_DEFAULT_DARK 1   /* dark, like the vyr scene #22262B */
    #define LV_THEME_DEFAULT_GROW 0
    #define LV_THEME_DEFAULT_TRANSITION_TIME 0
#endif
#define LV_USE_THEME_SIMPLE 0
#define LV_USE_THEME_MONO 0

/*==================
 * LAYOUTS
 *==================*/
#define LV_USE_FLEX 0
#define LV_USE_GRID 0

/*====================
 * 3RD PARTS LIBRARIES — all OFF (scene built via C API, no decode/vector)
 *====================*/
#define LV_FS_DEFAULT_DRIVER_LETTER '\0'
#define LV_USE_FS_STDIO 0
#define LV_USE_FS_POSIX 0
#define LV_USE_FS_WIN32 0
#define LV_USE_FS_FATFS 0
#define LV_USE_FS_MEMFS 0
#define LV_USE_FS_LITTLEFS 0
#define LV_USE_FS_ARDUINO_ESP_LITTLEFS 0
#define LV_USE_FS_ARDUINO_SD 0
#define LV_USE_FS_UEFI 0
#define LV_USE_FS_FROGFS 0
#define LV_USE_LODEPNG 0
#define LV_USE_LIBPNG 0
#define LV_USE_BMP 0
#define LV_USE_TJPGD 0
#define LV_USE_LIBJPEG_TURBO 0
#define LV_USE_LIBWEBP 0
#define LV_USE_GIF 0
#define LV_USE_GSTREAMER 0
#define LV_BIN_DECODER_RAM_LOAD 1
#define LV_USE_RLE 0
#define LV_USE_QRCODE 0
#define LV_USE_BARCODE 0
#define LV_USE_FREETYPE 0
#define LV_USE_TINY_TTF 0
#define LV_USE_RLOTTIE 0
#define LV_USE_GLTF  0
#define LV_USE_VECTOR_GRAPHIC  0
#define LV_USE_THORVG_INTERNAL 0
#define LV_USE_THORVG_EXTERNAL 0
#define LV_USE_NANOVG 0
#define LV_USE_LZ4_INTERNAL  0
#define LV_USE_LZ4_EXTERNAL  0
#define LV_USE_SVG 0
#define LV_USE_SVG_ANIMATION 0
#define LV_USE_SVG_DEBUG 0
#define LV_USE_FFMPEG 0

/*==================
 * OTHERS
 *==================*/
#define LV_USE_SNAPSHOT 0
#define LV_USE_SYSMON   0
#define LV_USE_PROFILER 0
#define LV_USE_MONKEY 0
#define LV_USE_GRIDNAV 0
#define LV_USE_FRAGMENT 0
#define LV_USE_IMGFONT 0
#define LV_USE_OBSERVER 1
#define LV_USE_IME_PINYIN 0
#define LV_USE_FILE_EXPLORER 0
#define LV_USE_FONT_MANAGER 0
#define LV_USE_TEST 0

/*==================
 * DEVICES
 *==================*/
#define LV_USE_SDL 0
#define LV_USE_X11 0
#define LV_USE_WAYLAND 0
#define LV_USE_LINUX_FBDEV 0
#define LV_USE_NUTTX 0
#define LV_USE_LINUX_DRM 0
#define LV_USE_TFT_ESPI 0
#define LV_USE_EVDEV 0
#define LV_USE_LIBINPUT 0
#define LV_USE_ST7735 0
#define LV_USE_ST7789 0
#define LV_USE_ST7796 0
#define LV_USE_ILI9341 0
#define LV_USE_GENERIC_MIPI 0
#define LV_USE_RENESAS_GLCDC 0
#define LV_USE_ST_LTDC 0
#define LV_USE_WINDOWS 0
#define LV_USE_OPENGLES 0
#define LV_USE_QNX 0

/*==================
 * EXAMPLES / DEMOS — OFF
 *==================*/
#define LV_BUILD_EXAMPLES 0
#define LV_USE_DEMO_WIDGETS 0
#define LV_USE_DEMO_KEYPAD_AND_ENCODER 0
#define LV_USE_DEMO_BENCHMARK 0
#define LV_USE_DEMO_RENDER 0
#define LV_USE_DEMO_STRESS 0
#define LV_USE_DEMO_MUSIC 0
#define LV_USE_DEMO_FLEX_LAYOUT 0
#define LV_USE_DEMO_MULTILANG 0
#define LV_USE_DEMO_TRANSFORM 0
#define LV_USE_DEMO_SCROLL 0
#define LV_USE_DEMO_VECTOR_GRAPHIC 0

#endif /*LV_CONF_H*/
#endif /*End of "Content enable"*/

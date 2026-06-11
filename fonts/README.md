# vyr standard fonts (F5)

Vendored font files for the runtime text path (`vyr-core/src/text.rs`).
`vyr-cli` registers every `*.ttf` / `*.otf` here at startup; the **registry
name is the file stem, lowercased** (`roboto.ttf` → font family `roboto`,
matched case-insensitively from IR `font_family` / `style_text_font`).

| File | Family | Source | License |
|---|---|---|---|
| `roboto.ttf` | `roboto` | Roboto Regular — byte-identical copy of awto-vyvanse `flutter_sim/assets/fonts/Roboto-Regular.ttf` (the #271 ONE vector test font all backends load), upstream <https://github.com/googlefonts/roboto> | Apache-2.0 (`LICENSE-roboto.txt`) |

Byte-identity with the vyvanse copy matters: every backend rasterizes the
SAME outlines, so a cross-backend text diff measures the renderer, not the
font file.

## Spleen — deliberately not vendored (yet)

The #271 bitmap test font (Spleen 5x8, BSD-2-Clause) exists in the vyvanse
tree only as an LVGL 1-bpp C array (`fixtures/font-corpus/third-party/
lv_font_spleen_5x8.c`) — there is no TTF/OTF form in-tree, and F5's contract
is **one format (TTF/OTF), one pipeline**. Rendering Spleen's upstream OTF
through the vector path would also antialias it, defeating the point of a
1-bpp bitmap test font. Spleen support stays an open item on issue #5
(candidate: F15 bake-format ingestion of the existing C array).

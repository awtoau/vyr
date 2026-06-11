# roboto-ascii.ttf — provenance + license

The LVGL-style cut-down font for the F9 runnable M4 vehicle (#9): a
printable-ASCII subset of `fonts/roboto.ttf` (Roboto Regular), hinting and
all OpenType layout dropped. **8,084 B vs the 162,876 B full font (5.0%).**
`fonts/roboto.ttf` itself is untouched — the committed goldens pin its exact
rasterization.

- **License:** Apache License 2.0, same as the source face — see
  `fonts/LICENSE-roboto.txt` (Copyright Google; Roboto by Christian
  Robertson). A subset is a derivative work distributed under the same
  terms.
- **Generator:** `python3 scripts/make-subset-font.py` (fonttools'
  `pyftsubset`). Exact flags, with the why for each, in the script header:
  `--unicodes=U+0020-007E --no-hinting --layout-features= --name-IDs=1,2,3,6
  --notdef-outline`.
- **Why hinting can go:** vyr rasterizes unhinted (deterministic skrifa
  outlines), so fpgm/prep/cvt + per-glyph instructions are dead bytes on
  device — the same call LVGL's font converter makes by default.
- **Why layout can go:** vyr's text path places glyphs by plain advances
  (no shaping), so GSUB/GPOS (incl. kern) are unreachable.

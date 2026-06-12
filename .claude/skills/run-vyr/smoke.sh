#!/usr/bin/env bash
# smoke.sh — build vyr and drive its real render path end-to-end.
#
# vyr is a Rust IR-renderer: IR JSON in -> RGB888 PNG out (no GUI, no server).
# So "driving the app" = build vyr-cli, render the built-in demo AND a custom
# IR scene to PNG, and verify the pixels are real (not a blank/flat frame —
# the project's own honesty invariant, "blank != rendered").
#
# Usage:
#   .claude/skills/run-vyr/smoke.sh            # build + render + verify
#   .claude/skills/run-vyr/smoke.sh --gate     # also run ./dev.py ci --quick
#
# Run from the repo root (/home/dan/git/vyr). Output PNGs land in ./tmp/.
# Exit 0 = all renders produced real multi-colour pixels.
set -euo pipefail
cd "$(dirname "$0")/../../.."   # -> repo root (skill is at .claude/skills/run-vyr/)
mkdir -p tmp
CLI=./target/release/vyr-cli

echo "== build vyr-cli (release) =="
cargo build --release -p vyr-cli

# verify a PNG is non-blank: decode it and assert >1 distinct colour.
verify() {  # $1 = png path, $2 = label
  python3 - "$1" "$2" <<'PY'
import sys, zlib, struct
path, label = sys.argv[1], sys.argv[2]
data = open(path, "rb").read()
assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{label}: not a PNG"
# pull IHDR + concat IDAT
i, idat, w, h, depth, ctype = 8, b"", 0, 0, 0, 0
while i < len(data):
    ln = struct.unpack(">I", data[i:i+4])[0]; typ = data[i+4:i+8]; body = data[i+8:i+8+ln]
    if typ == b"IHDR": w, h, depth, ctype = *struct.unpack(">II", body[:8]), body[8], body[9]
    elif typ == b"IDAT": idat += body
    elif typ == b"IEND": break
    i += 12 + ln
raw = zlib.decompress(idat)
ch = {0:1,2:3,3:1,4:2,6:4}[ctype]; stride = w*ch
# undo PNG filtering (only the subset vyr emits: ctype 2 RGB, filter per row)
out = bytearray(); prev = bytearray(stride); p = 0
for _ in range(h):
    f = raw[p]; p += 1; row = bytearray(raw[p:p+stride]); p += stride
    for x in range(stride):
        a = row[x-ch] if x>=ch else 0; b = prev[x]; c = prev[x-ch] if x>=ch else 0
        if f==1: row[x]=(row[x]+a)&255
        elif f==2: row[x]=(row[x]+b)&255
        elif f==3: row[x]=(row[x]+((a+b)>>1))&255
        elif f==4:
            pp=a+b-c; pa=abs(pp-a); pb=abs(pp-b); pc=abs(pp-c)
            pr=a if pa<=pb and pa<=pc else (b if pb<=pc else c); row[x]=(row[x]+pr)&255
    out += row; prev = row
colours = {bytes(out[i:i+ch]) for i in range(0, len(out), ch)}
assert len(colours) > 1, f"{label}: BLANK/flat frame ({len(colours)} colour) — render is broken"
print(f"  PASS {label}: {w}x{h}, {len(colours)} distinct colours -> {path}")
PY
}

echo "== render: built-in demo scene (selftest-png) =="
"$CLI" selftest-png tmp/smoke-selftest.png >/dev/null 2>&1
verify tmp/smoke-selftest.png "selftest demo"
verify tmp/smoke-selftest-text.png "selftest text"

echo "== render: custom IR scene (render <ir.json> <out.png>) =="
cat > tmp/smoke-scene.json <<'JSON'
{"w":240,"h":160,"root":{"name":"view","attrs":{"background":"#F0F0F0"},"children":[
  {"name":"vy_frame","attrs":{"x":"8","y":"8","width":"224","height":"60","background":"#DCE6F5","border_width":"2","border_color":"#1E5AA8","radius":"8"}},
  {"name":"vy_chart","attrs":{"x":"16","y":"14","width":"120","height":"46","points":"10,60,30,85,45,90,55","div_count_x":"6","div_count_y":"4","line_color":"#1E5AA8"}},
  {"name":"vy_gauge","attrs":{"x":"150","y":"14","width":"44","height":"44","value":"65"}},
  {"name":"vy_slider","attrs":{"x":"16","y":"80","width":"208","height":"16","value":"60"}},
  {"name":"vy_toggle","attrs":{"x":"16","y":"108","width":"44","height":"22","value":"1"}}
]}}
JSON
"$CLI" render tmp/smoke-scene.json tmp/smoke-scene.png >/dev/null 2>&1
verify tmp/smoke-scene.png "custom IR (frame+chart+gauge+slider+toggle)"

if [ "${1:-}" = "--gate" ]; then
  echo "== full gate: ./dev.py ci --quick =="
  python3 dev.py ci --quick
fi

echo "SMOKE OK — vyr builds and renders real pixels. PNGs in ./tmp/smoke-*.png"

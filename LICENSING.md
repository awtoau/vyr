# Licensing

vyr is dual-licensed:

1. **GPL-3.0-only** (see [`LICENSE`](LICENSE)) — use, study, modify, and
   redistribute freely; derivative works and works linking vyr must be
   distributed under the GPL.
2. **Commercial license** — for shipping vyr in proprietary firmware or
   applications without GPL obligations. Contact **dan@awto.au**.

This is the same model used by Slint and Qt: development happens fully in the
open; the copyleft license keeps the open version open, and the commercial
license funds the work.

## Contributions

By contributing you certify the
[Developer Certificate of Origin](https://developercertificate.org/) — sign
commits off with `git commit -s`. Because dual-licensing requires the project
to relicense contributions commercially, substantial contributions will
additionally need a CLA (mechanics being finalised — see issue tracker; DCO
sign-off is sufficient for small fixes in the meantime).

## Third-party

Dependencies are permissively licensed (BSD-3/MIT/Apache-2.0 — tiny-skia,
skrifa, swash, png) and compatible with GPL distribution. vyr contains no code
from LVGL, TouchGFX, Qt, Flutter, or Skia; file formats consumed (vyvanse IR
JSON; upstream: TouchGFX project JSON, LVGL XML) are formats, not code.

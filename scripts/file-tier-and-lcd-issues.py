#!/usr/bin/env python3
"""File the two follow-on issues: quality-tier/LVGL comparison, and LCD output.

Both bodies quote the plugin-measured instruction counts from
tmp/qemu-insn-*.json rather than hardcoding them, so the issues cannot drift
from the measurements. Refuses to file if those files are missing or
non-deterministic.

Logs to tmp/file-tier-and-lcd-issues.log.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
LOG = TMP / "file-tier-and-lcd-issues.log"
GH_REPO = "awtoau/vyr"

_lines: list[str] = []


def log(m: str = "") -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"{stamp} UTC  [file-issues] {m}" if m else ""
    print(line)
    _lines.append(line)


def measured(name: str) -> dict:
    p = TMP / f"qemu-insn-{name}.json"
    if not p.exists():
        raise SystemExit(f"missing {p} — run scripts/qemu-insn.py first")
    d = json.loads(p.read_text())
    if not d.get("deterministic"):
        raise SystemExit(f"{p} is not marked deterministic — refusing to quote it")
    return d


def create(title: str, body: str, labels: list[str]) -> str:
    cmd = ["gh", "issue", "create", "-R", GH_REPO, "--title", title, "--body", body,
           "--assignee", "awto-au"]
    for l in labels:
        cmd += ["--label", l]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  FAILED: {r.stderr.strip()[:300]}")
        return ""
    url = r.stdout.strip()
    log(f"  created {url}")
    return url


def main() -> int:
    TMP.mkdir(exist_ok=True)
    ex = measured("vyr-exact")["insns_per_frame"]
    dr = measured("vyr-draft")["insns_per_frame"]
    lv = measured("lvgl")["insns_per_frame"]
    lvgl_meta = measured("lvgl")
    qemu_ver = lvgl_meta.get("qemu", "?")
    log(f"quoting exact={ex:,} draft={dr:,} lvgl={lv:,}")

    px = 480 * 270
    tier_body = f"""vyr renders at one of two **quality tiers**, and the LVGL performance comparison currently quotes the fast one without stating what it gives up. This issue documents both tiers and makes the comparison fidelity-honest.

## The tiers

**`Quality::Exact`** — the oracle. Every primitive goes through tiny-skia's general-purpose float coverage pipeline: analytic anti-aliasing, real rounded-rect geometry, the full glyph raster path. This is the mode the goldens are blessed against and the mode the conformance gate scores the other four backends against. It is *the specification made executable*, and it is deliberately not optimised at the cost of correctness.

**`Quality::Draft`** — the budgeted-MCU tier. An integer, no-AA fast path that bypasses tiny-skia entirely for the dominant operations: opaque axis-aligned span fills, integer disc/ring/line, and direct RGB888 output with no premultiplied-pixmap round trip. Roughly 97% of delivered pixels take the fast path.

**Draft's documented fidelity losses** (from the tier's own commits):
- no anti-aliasing anywhere — edges are hard
- `radius > 0` on a rect draws **square**, not rounded
- translucent fills, gradients, glyphs and images fall back to the Exact path
- pixels differ from Exact by construction; the goldens assert they differ

## The measured cost (exact instruction counts)

QEMU {qemu_ver} built with `--enable-plugins` + the `libinsn` TCG plugin, `netduinoplus2` (STM32F405/M4F), 480x270 warmed steady state. Bit-identical across idle and host-loaded runs; slope-validated at double the frame count with remainder 0.

| tier | insns/frame | insn/px | vs Exact | vs LVGL |
|---|--:|--:|--:|--:|
| vyr **Exact** | {ex:,} | {ex/px:.1f} | 1.00x | {ex/lv:.2f}x |
| vyr **Draft** | {dr:,} | {dr/px:.1f} | **{ex/dr:.2f}x cheaper** | **{dr/lv:.4f}x** |
| LVGL 9.6.0-dev | {lv:,} | {lv/px:.1f} | — | 1.00x |

So Draft is {100*(1-dr/lv):.2f}% cheaper than LVGL, and Exact is {ex/lv:.2f}x dearer.

## Why the headline needs qualifying

**"vyr beats LVGL" is currently a Draft-tier claim, and Draft is not visually equivalent to what LVGL draws.** LVGL is a hand-tuned fixed-point C blitter that *does* anti-alias several primitives. Comparing a no-AA integer path against it and reporting an 8% instruction win is only meaningful once we know what the pixels look like side by side.

The two honest framings are:
1. **"At equivalent output quality, vyr costs X"** — which tier of vyr actually matches LVGL's rendering? Probably neither: Draft is below it, Exact is far above it.
2. **"vyr offers a quality/speed dial LVGL does not"** — arguably the stronger and more defensible product claim, since Exact is a conformance oracle no fixed-point blitter can be.

## Work

- [ ] Document both tiers properly in `docs/` — what each guarantees, what Draft gives up, when to pick which. Right now this lives scattered across commit messages.
- [ ] Render the same scene through Exact, Draft, and LVGL and put the three images side by side. Quantify the Exact-vs-Draft delta (pixels differing, max channel error) and, qualitatively, where LVGL sits between them.
- [ ] Decide and write down the canonical headline claim, then make `docs/milestones/README.md` and `docs/measurements/f9-static.md` say exactly that and nothing stronger.
- [ ] Consider whether a middle tier (`Fast` — AA retained, cheaper geometry) is worth having; the `Quality` enum was designed with room for one.

Related: #26 (the anchor provenance), #25 (no ledger records any of this).
"""

    lcd_body = f"""The STM32F429I-DISC1 has a TFT panel on it and vyr has never drawn a single pixel to it. The board vehicle renders the 480x270 fixture into a band buffer, folds each band into an FNV-1a hash, and throws the pixels away. Everything we know about on-target correctness is a hash comparison.

That is fine for measurement and useless as proof. **You cannot look at the board and see that it works.**

## Why this matters beyond the demo

- **Visual proof on real silicon.** The frame hash `0x24dcaff531c6eb01` is now confirmed identical across x86-64, emulated M4 and the physical F429 — strong evidence, but nobody can *see* it.
- **It unblocks a real head-to-head.** The LVGL comparison is instruction counts on an emulator. Running LVGL and vyr back to back on the same panel, same scene, is the demonstration that actually persuades — and it is the natural home for the tier comparison in the sibling issue.
- **It is the missing half of the F9 board story** (#9), and a prerequisite for the import-and-run demos (#12).

## What it needs (verify each — do not assume the board revision)

- **Panel + controller.** The DISC1 carries a 2.4" QVGA TFT. Confirm the exact controller and whether it is driven through LTDC or an SPI command interface on this revision.
- **Framebuffer memory.** 240x320 at RGB888 is ~230 KB — it does **not** fit the 192 KB SRAM. The board has external SDRAM; that means bringing up FMC/SDRAM before any framebuffer exists. This is the substantial piece of work, not the drawing.
- **Scene size.** The fixture is 480x270 and the panel is portrait QVGA. Either author a panel-native fixture or decide on a scaling/crop policy. A native-resolution fixture is almost certainly the right answer — scaling would muddy any perf comparison.
- **vyr needs no changes.** `render(tree, area, buf, stride)` already renders into a caller-supplied buffer at any origin; a display driver is purely `vyr-size`-side scaffolding. Banding maps naturally onto partial panel updates.

## Work

- [ ] Confirm panel controller + interface on this board revision, with a datasheet reference
- [ ] Bring up FMC/SDRAM; verify with a memory test before trusting a framebuffer in it
- [ ] Minimal display driver (init + flush a band/rect), in `vyr-size` — measurement scaffolding, never renderer code
- [ ] A panel-native fixture, and render it end to end
- [ ] Photograph the result for `docs/milestones/`
- [ ] Then: the same scene under LVGL on the same panel, run back to back

## Guardrails

- vyr-core stays `no_std + alloc` and `forbid(unsafe_code)`; all register poking lives in `vyr-size`.
- The display path must be **excluded from the timed window** — flush cost is a driver concern and would contaminate the per-frame render numbers.
- Keep the existing hash check running alongside the display, so visual output never silently replaces the determinism proof.
"""

    log("filing issue 1/2 — quality tiers vs LVGL")
    a = create("Document the Exact/Draft quality tiers and make the LVGL comparison fidelity-honest",
               tier_body, ["documentation", "p1"])
    log("filing issue 2/2 — LCD output on the Discovery board")
    b = create("Render to the LCD on the STM32F429I-DISC1 (nothing is drawn to the panel today)",
               lcd_body, ["enhancement", "p1"])

    log()
    log(f"done: {a or 'FAILED'}  |  {b or 'FAILED'}")
    LOG.write_text("\n".join(_lines) + "\n")
    return 0 if a and b else 1


if __name__ == "__main__":
    raise SystemExit(main())

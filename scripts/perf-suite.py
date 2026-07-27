#!/usr/bin/env python3
"""perf-suite.py — the fingerprint of WHAT the perf ledger measures (#43).

#43, reshaped: the ledger is not versioned row-by-row. It is ALWAYS a full
reprocess under the current suite (the repo's "derived artifacts have no legacy;
the rebuild IS the migration" rule, extended from "the instrument changed" to
"the tests changed"). So there is only ever ONE suite in a ledger, and no
per-row version to track or join across.

The one guard that keeps that true: this fingerprint. It hashes the inputs that
change the MEANING of the numbers — the vyr fixture, band height, frame counts,
tier set, and the LVGL scene + config + upstream commit. The ledger records ONE
fingerprint; `./dev.py track` refuses to APPEND a row when the current suite no
longer matches the ledger's, and tells you to reprocess instead. A full
reprocess re-stamps it. That converts "reprocess when the tests change" from a
thing to remember into a gate.

It hashes NAMED SPANS, never whole files — a renderer or harness edit that does
not touch the measured work must not move the fingerprint (that false positive
is what would get the guard disabled).

Usage:
  python3 scripts/perf-suite.py            # print the current fingerprint + inputs
  python3 scripts/perf-suite.py --compare  # exit 1 if it differs from the ledger's
Log: tmp/perf-suite.log
"""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
# (the canonical store is docs/perf/ledger.db via scripts/ledger_store)
WORKLOAD = REPO / "vyr-size" / "src" / "workload.rs"
LVGL_MAIN = REPO / "scripts" / "lvgl-m4-bench" / "main.c"
LVGL_CONF = REPO / "scripts" / "lvgl-m4-bench" / "lv_conf.h"
LVGL_MIRROR = Path("/mnt/2tb/git_mirror/lvgl")


def _span(text: str, start: str, end: str) -> str:
    i = text.find(start)
    j = text.find(end)
    if i < 0 or j < 0:
        raise SystemExit(f"perf-suite: markers {start!r}/{end!r} not found — "
                         "the suite region cannot be fingerprinted")
    return text[i:j + len(end)]


def _const(text: str, name: str, pat: str) -> str:
    m = re.search(pat, text)
    if not m:
        raise SystemExit(f"perf-suite: could not read {name} from {WORKLOAD.name}")
    return m.group(1)


def suite_inputs() -> dict:
    """The named inputs that define WHAT is measured. Whole-file hashes are
    deliberately avoided; each entry is an extracted span or a single value."""
    wl = WORKLOAD.read_text()
    fixture = _span(wl, 'pub const FIXTURE_IR: &str = r##"', '"##;')
    band_h = _const(wl, "BAND_H", r"None => (\d+),\s*\n\};")
    timed = _const(wl, "TIMED_FRAMES", r"const TIMED_FRAMES: u32 = (\d+);")
    fw = _const(wl, "FIXTURE_W", r"pub const FIXTURE_W: u32 = (\d+);")
    fh = _const(wl, "FIXTURE_H", r"pub const FIXTURE_H: u32 = (\d+);")

    lm = LVGL_MAIN.read_text()
    lvgl_scene = _span(lm, "/* SUITE:lvgl_scene:start", "/* SUITE:lvgl_scene:end */")
    lvgl_timed = re.search(r"#define TIMED_FRAMES (\d+)", lm)
    lvgl_conf = LVGL_CONF.read_text()
    try:
        lvgl_commit = subprocess.run(
            ["git", "-C", str(LVGL_MIRROR), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        lvgl_commit = "unknown"

    return {
        "vyr_fixture_ir": fixture,
        "vyr_scene": {"w": fw, "h": fh, "band_h": band_h, "timed_frames": timed},
        "vyr_tiers": ["exact", "fast", "draft"],
        "lvgl_scene": lvgl_scene,
        "lvgl_timed_frames": lvgl_timed.group(1) if lvgl_timed else None,
        "lvgl_conf": lvgl_conf,
        "lvgl_commit": lvgl_commit,   # #43 decision: LVGL upstream is part of the suite
    }


def fingerprint(inputs: dict | None = None) -> str:
    inp = inputs if inputs is not None else suite_inputs()
    blob = json.dumps(inp, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()[:32]


def ledger_fingerprint() -> str | None:
    # Canonical store is SQLite now (scripts/ledger_store).
    sys.path.insert(0, str(REPO / "scripts"))
    import ledger_store as STORE
    note = STORE.schema_note()
    return note.get("suite_fingerprint") if note else None


def main() -> int:
    inp = suite_inputs()
    fp = fingerprint(inp)
    summary = {
        "suite_fingerprint": fp,
        "vyr_scene": inp["vyr_scene"],
        "vyr_tiers": inp["vyr_tiers"],
        "vyr_fixture_ir_len": len(inp["vyr_fixture_ir"]),
        "lvgl_timed_frames": inp["lvgl_timed_frames"],
        "lvgl_commit": inp["lvgl_commit"],
        "lvgl_scene_len": len(inp["lvgl_scene"]),
    }
    TMP.mkdir(exist_ok=True)
    (TMP / "perf-suite.log").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))

    if "--compare" in sys.argv:
        led = ledger_fingerprint()
        if led is None:
            print("perf-suite: the ledger records no fingerprint yet "
                  "(pre-#43 or never reprocessed).")
            return 0
        if led == fp:
            print(f"perf-suite: MATCH — the ledger was built under this suite ({fp}).")
            return 0
        print(f"perf-suite: MISMATCH — the suite changed since the ledger was built.\n"
              f"  ledger:  {led}\n  current: {fp}\n"
              f"  The tests changed WHAT is measured. Do not append — REPROCESS the "
              f"whole ledger under the new suite:\n"
              f"    python3 scripts/perf-replay.py   &&   "
              f"./dev.py track --rebuild-from-replay tmp/perf-replay.jsonl")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

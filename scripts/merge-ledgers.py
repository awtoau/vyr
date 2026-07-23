#!/usr/bin/env python3
"""Merge docs/perf and docs/metrics into one ledger, and report what is plottable.

The two ledgers carry an identical identity envelope (ts/commit/host/cpu/arch)
and disjoint payloads: docs/perf owns the resolution ladder (rungs[]), the anim
block and the arm cross-ISA block; docs/metrics owns flash/heap/M4-insns and the
per-primitive and per-scene host costs.

They overlap in exactly three DERIVED scalars — ladder480_full_ns_px,
ladder480_incr_ns_px, ladder4k_full_ns_px — which docs/metrics stores flat and
docs/perf stores in full inside rungs[]. Those three are redundant for storage,
but they are the ONLY fields both harnesses record, so they are the only bridge
that can form a continuous series across the union of commits.

Writes:
  tmp/merged-ledger.jsonl   the union, one row per (commit, ts), sorted by ts
  tmp/merged-ledger.log     progress + the coverage report
"""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parent.parent
PERF = REPO / "docs/perf/history.jsonl"
METRICS = REPO / "docs/metrics/history.jsonl"
TMP = REPO / "tmp"
OUT = TMP / "merged-ledger.jsonl"
LOG = TMP / "merged-ledger.log"

# The three scalars docs/metrics stores flat that are recoverable from rungs[].
# (metrics key, rung predicate) — the rung is matched on width.
BRIDGE = {
    "ladder480_full_ns_px": (480, "full_ns_px"),
    "ladder480_incr_ns_px": (480, "incr_ns_dirty_px"),
    "ladder4k_full_ns_px": (3840, "full_ns_px"),
}

_lines: list[str] = []


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")
    line = f"{stamp} UTC  INFO  [merge-ledgers] {msg}"
    print(line)
    _lines.append(line)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        log(f"MISSING {path.relative_to(REPO)} — treating as empty")
        return []
    rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    log(f"read {len(rows)} row(s) from {path.relative_to(REPO)}")
    return rows


def project_bridge(row: dict) -> dict:
    """Recover the three flat ladder scalars from a docs/perf row's rungs[]."""
    out = {}
    for key, (width, field) in BRIDGE.items():
        for rung in row.get("rungs", []):
            if rung.get("w") == width:
                out[key] = rung[field]
                break
    return out


def merge() -> list[dict]:
    perf = read_jsonl(PERF)
    metrics = read_jsonl(METRICS)

    merged: list[dict] = []

    for r in perf:
        merged.append(
            {
                "ts": r["ts"],
                "commit": r["commit"],
                "dirty": None,  # docs/perf never recorded tree cleanliness
                "host": r.get("host"),
                "cpu": r.get("cpu"),
                "arch": r.get("arch"),
                "source": "perf",
                "rungs": r.get("rungs"),
                "anim": r.get("anim"),
                "arm": r.get("arm"),
                "metrics": None,
                "bridge": project_bridge(r),
            }
        )

    for r in metrics:
        m = dict(r.get("metrics", {}))
        bridge = {k: m.pop(k) for k in list(BRIDGE) if k in m}
        merged.append(
            {
                "ts": r["ts"],
                "commit": r["commit"],
                "dirty": r.get("dirty"),
                "host": r.get("host"),
                "cpu": r.get("cpu"),
                "arch": r.get("arch"),
                "source": "metrics",
                "rungs": None,
                "anim": None,
                "arm": None,
                "metrics": m,
                "bridge": bridge,
            }
        )

    merged.sort(key=lambda d: d["ts"])
    return merged


def coverage_report(merged: list[dict]) -> None:
    log("")
    log("=== coverage ===")
    commits = []
    for row in merged:
        if row["commit"] not in commits:
            commits.append(row["commit"])
    log(f"{len(merged)} merged row(s) across {len(commits)} distinct commit(s): {', '.join(commits)}")

    perf_c = {r["commit"] for r in merged if r["source"] == "perf"}
    met_c = {r["commit"] for r in merged if r["source"] == "metrics"}
    log(f"  perf-only commits    : {sorted(perf_c - met_c)}")
    log(f"  metrics-only commits : {sorted(met_c - perf_c)}")
    log(f"  measured by BOTH     : {sorted(perf_c & met_c) or 'NONE'}")

    log("")
    log("series point counts (a trend needs >= 2):")
    counts: dict[str, int] = {}
    for row in merged:
        for k in row["bridge"]:
            counts[k] = counts.get(k, 0) + 1
        for k in (row["metrics"] or {}):
            counts[k] = counts.get(k, 0) + 1
        if row["anim"]:
            counts["anim_ms_frame"] = counts.get("anim_ms_frame", 0) + 1
    for k in sorted(counts, key=lambda x: (-counts[x], x)):
        mark = "PLOTTABLE" if counts[k] >= 2 else "single point"
        log(f"  {counts[k]}  {k:<24} {mark}")


def main() -> None:
    TMP.mkdir(exist_ok=True)
    log(f"perf={PERF.relative_to(REPO)}  metrics={METRICS.relative_to(REPO)}")
    merged = merge()
    OUT.write_text("".join(json.dumps(r) + "\n" for r in merged))
    log(f"wrote {len(merged)} row(s) -> {OUT.relative_to(REPO)}")
    coverage_report(merged)
    LOG.write_text("\n".join(_lines) + "\n")


if __name__ == "__main__":
    main()

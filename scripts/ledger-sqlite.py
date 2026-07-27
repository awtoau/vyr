#!/usr/bin/env python3
"""ledger-sqlite.py — a queryable SQLite MIRROR of the perf ledger.

`docs/perf/history.jsonl` stays the canonical, append-only, git-tracked record
(#25) — this does NOT replace it. It flattens the schema-3 matrix cells into a
`docs/perf/ledger.db` so the whole measurement history is queryable in SQL
without hand-walking nested JSON:

    SELECT commit_short, tier, opt_level, insns_per_frame_render_only
    FROM matrix WHERE platform='qemu-m4' AND status='measured'
    ORDER BY commit_date;

The DB is a DERIVED artifact (this repo's rule): regenerate it from the jsonl
any time; never edit it by hand, never commit it as a source of truth. Because
it is generated FROM the jsonl, it cannot break the append / replay pipeline or
the other tools that write the ledger — which is exactly why the canonical
store stays jsonl and this is a mirror, not a cutover.

Output: docs/perf/ledger.db (+ tmp/ledger-sqlite.log)
Usage:  python3 scripts/ledger-sqlite.py [--jsonl docs/perf/history.jsonl]
                                         [--db docs/perf/ledger.db]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
JSONL = REPO / "docs" / "perf" / "history.jsonl"
DB = REPO / "docs" / "perf" / "ledger.db"

# The commit-level columns (one row per ledger entry) and the matrix-cell
# columns (one row per platform × tier × opt × build_type). Kept explicit so
# the schema is documentation, and a new field in the jsonl is a visible add
# here rather than a silent drop.
COMMIT_COLS = ["commit", "commit_full", "ts", "commit_date", "subject",
               "dirty", "host", "cpu", "arch", "schema"]
CELL_DIMS = ["platform", "firmware", "tier", "opt_level", "target", "profile",
             "isa", "word_bits", "float", "build_type", "fold_provenance",
             "status", "reason"]
CELL_METRICS = ["insns_per_frame_total", "harness_fold_insns",
                "insns_per_frame_render_only", "cycles_per_frame", "ns_per_frame",
                "ns_per_px", "heap_peak_b", "stack_high_water_b",
                "flash_text_data_b", "frame_hash"]

SCHEMA = f"""
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE commits(
    {', '.join('"' + c + '" ' + ('INTEGER' if c in ('dirty','schema') else 'TEXT') for c in COMMIT_COLS)},
    PRIMARY KEY ("commit")
);
CREATE TABLE matrix(
    "commit" TEXT, commit_date TEXT,
    {', '.join('"' + d + '" TEXT' for d in CELL_DIMS)},
    {', '.join('"' + m + '" ' + ('TEXT' if m == 'frame_hash' else 'INTEGER') for m in CELL_METRICS)}
);
CREATE INDEX ix_matrix_dims ON matrix(platform, tier, opt_level, build_type);
CREATE VIEW m4_render AS
    SELECT "commit", commit_date, tier, opt_level, build_type,
           insns_per_frame_render_only AS insns, frame_hash
    FROM matrix WHERE platform='qemu-m4' AND status='measured'
    ORDER BY commit_date, tier, opt_level;
"""


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line)
    (REPO / "tmp").mkdir(exist_ok=True)
    with open(REPO / "tmp" / "ledger-sqlite.log", "a") as f:
        f.write(line + "\n")


def build(jsonl: Path, db_path: Path) -> tuple[int, int]:
    rows = [json.loads(ln) for ln in jsonl.read_text().splitlines() if ln.strip()]
    note = next((r for r in rows if r.get("kind") == "schema-note"), {})
    data = [r for r in rows if r.get("kind") != "schema-note"]

    db_path.unlink(missing_ok=True)  # derived artifact: always a clean rebuild
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO meta VALUES ('generated', ?)",
                (time.strftime("%Y-%m-%dT%H:%M:%S%z"),))
    con.execute("INSERT INTO meta VALUES ('source', ?)", (str(jsonl.relative_to(REPO)),))
    con.execute("INSERT INTO meta VALUES ('schema_note', ?)",
                (note.get("suite_fingerprint", ""),))

    n_cells = 0
    for r in data:
        con.execute(
            f'INSERT OR REPLACE INTO commits({",".join(chr(34)+c+chr(34) for c in COMMIT_COLS)}) '
            f'VALUES ({",".join("?" * len(COMMIT_COLS))})',
            [1 if c == "dirty" and r.get(c) else r.get(c) for c in COMMIT_COLS])
        for cell in r.get("matrix", {}).get("cells", []):
            metrics = cell.get("metrics", {})
            vals = ([r.get("commit"), r.get("commit_date")]
                    + [cell.get(d) for d in CELL_DIMS]
                    + [metrics.get(m) for m in CELL_METRICS])
            cols = ['"commit"', "commit_date"] + CELL_DIMS + CELL_METRICS
            con.execute(f'INSERT INTO matrix({",".join(cols)}) '
                        f'VALUES ({",".join("?" * len(cols))})', vals)
            n_cells += 1
    con.commit()
    con.close()
    return len(data), n_cells


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", default=str(JSONL))
    ap.add_argument("--db", default=str(DB))
    a = ap.parse_args()
    jsonl = Path(a.jsonl)
    if not jsonl.exists():
        log(f"no ledger at {jsonl}")
        return 1
    n_rows, n_cells = build(jsonl, Path(a.db))
    log(f"mirrored {n_rows} commits / {n_cells} matrix cells -> {a.db} "
        f"(canonical source {jsonl.name} untouched)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

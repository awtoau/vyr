#!/usr/bin/env python3
"""ledger_store.py — the perf ledger's canonical store, now SQLite.

Replaces the append-only `docs/perf/history.jsonl`. The store is
`docs/perf/ledger.db`; the JSONL is retired. Every ledger ROW is kept
LOSSLESSLY as its exact JSON in `rows.json`, so `ledger.py` reads back byte-for
-byte the same dicts it used to read from the file and regenerates
`index.html` unchanged — only the I/O moved. Derived flat tables (`commits`,
`matrix`) are rebuilt from those rows on every write, so the history is also
queryable in SQL:

    SELECT "commit", tier, opt_level, insns_per_frame_render_only
    FROM matrix WHERE platform='qemu-m4' ORDER BY commit_date;

`rows` is the source of truth; `commits`/`matrix` are derived and never edited
directly. Row ORDER is preserved by an autoincrement `seq` (the old file's line
order), because the ledger is chronological and index.html relies on it.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "docs" / "perf" / "ledger.db"

# Flat-table columns, mirrored from a ledger row / matrix cell. Kept explicit so
# a new field is a visible add here, not a silent drop. `rows.json` is lossless
# regardless — these are only for querying.
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
CREATE TABLE IF NOT EXISTS rows(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT, "commit" TEXT, ts TEXT, json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commits(
    {', '.join('"' + c + '" ' + ('INTEGER' if c in ('dirty','schema') else 'TEXT') for c in COMMIT_COLS)},
    PRIMARY KEY ("commit")
);
CREATE TABLE IF NOT EXISTS matrix(
    "commit" TEXT, commit_date TEXT,
    {', '.join('"' + d + '" TEXT' for d in CELL_DIMS)},
    {', '.join('"' + m + '" ' + ('TEXT' if m == 'frame_hash' else 'INTEGER') for m in CELL_METRICS)}
);
CREATE INDEX IF NOT EXISTS ix_matrix_dims ON matrix(platform, tier, opt_level, build_type);
CREATE VIEW IF NOT EXISTS m4_render AS
    SELECT "commit", commit_date, tier, opt_level, build_type,
           insns_per_frame_render_only AS insns, frame_hash
    FROM matrix WHERE platform='qemu-m4' AND status='measured'
    ORDER BY commit_date, tier, opt_level;
"""


def _connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    return con


def _derive_flat(con: sqlite3.Connection) -> None:
    """Rebuild commits/matrix from the lossless rows — the one derivation."""
    con.execute("DELETE FROM commits")
    con.execute("DELETE FROM matrix")
    for (js,) in con.execute("SELECT json FROM rows ORDER BY seq"):
        r = json.loads(js)
        if r.get("kind") == "schema-note":
            continue
        con.execute(
            f'INSERT OR REPLACE INTO commits({",".join(chr(34)+c+chr(34) for c in COMMIT_COLS)}) '
            f'VALUES ({",".join("?" * len(COMMIT_COLS))})',
            [1 if c == "dirty" and r.get(c) else r.get(c) for c in COMMIT_COLS])
        for cell in r.get("matrix", {}).get("cells", []):
            metrics = cell.get("metrics", {})
            cols = ['"commit"', "commit_date"] + CELL_DIMS + CELL_METRICS
            vals = ([r.get("commit"), r.get("commit_date")]
                    + [cell.get(d) for d in CELL_DIMS]
                    + [metrics.get(m) for m in CELL_METRICS])
            con.execute(f'INSERT INTO matrix({",".join(cols)}) '
                        f'VALUES ({",".join("?" * len(cols))})', vals)


def load_rows(db: Path = DB) -> list[dict]:
    """Every ledger row, in order — the drop-in for reading history.jsonl."""
    if not db.exists():
        return []
    con = _connect(db)
    out = [json.loads(js) for (js,) in con.execute("SELECT json FROM rows ORDER BY seq")]
    con.close()
    return out


def append(row: dict, db: Path = DB) -> None:
    """Append one row (the drop-in for `open(HISTORY,'a')`)."""
    con = _connect(db)
    con.execute('INSERT INTO rows(kind,"commit",ts,json) VALUES (?,?,?,?)',
                (row.get("kind", "measurement"), row.get("commit"), row.get("ts"),
                 json.dumps(row, separators=(",", ":"))))
    _derive_flat(con)
    con.commit()
    con.close()


def rewrite(rows: list[dict], db: Path = DB) -> None:
    """Replace the whole store (the drop-in for the rebuild-from-replay write)."""
    con = _connect(db)
    con.execute("DELETE FROM rows")
    con.execute("DELETE FROM sqlite_sequence WHERE name='rows'")
    for r in rows:
        con.execute('INSERT INTO rows(kind,"commit",ts,json) VALUES (?,?,?,?)',
                    (r.get("kind", "measurement"), r.get("commit"), r.get("ts"),
                     json.dumps(r, separators=(",", ":"))))
    _derive_flat(con)
    con.commit()
    con.close()


def schema_note(db: Path = DB) -> dict | None:
    for r in load_rows(db):
        if r.get("kind") == "schema-note":
            return r
    return None


def exists(db: Path = DB) -> bool:
    return db.exists() and bool(load_rows(db))

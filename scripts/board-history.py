#!/usr/bin/env python3
"""board-history.py — the F9 board leg over git HISTORY, on real silicon.

The board can only be driven by ONE physical probe, so this is SERIAL (never the
parallel replay). It walks history exactly as scripts/perf-replay.py does —
grouped by build_key, one measurement per distinct binary, the rest recorded as
`covers` — but each group is flashed to the actual F429 and timed with
DWT_CYCCNT.

New instrument, old renderer: a dedicated worktree checks out each commit's
SOURCE and today's scripts/board-run.py builds and flashes it (--repo). Cycles
are architectural, so they track the emulated instruction trend — the point of
running them on metal is the REAL ms and the real-hardware effects (flash wait
states, ART cache) that emulation cannot model, and to catch a hardware-only
regression the instruction count would miss.

Cycles only (--no-verify): the frame hash is already proven at HEAD and across
all of history in the qemu replay; re-flashing a verify image per commit would
double the ~1.4 h run for a hash we already trust.

Output: tmp/board-history.jsonl (one record per build group, resumable).
Log:    tmp/board-history.log
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
OUT = TMP / "board-history.jsonl"
LOG = TMP / "board-history.log"
WORKTREE = Path(os.environ.get("VYR_BOARD_WORKTREE",
                               "/mnt/2tb/git_debris/vyr-board-specimen"))
SHARED_TARGET = Path(os.environ.get("VYR_BOARD_TARGET",
                                    "/mnt/2tb/git_debris/vyr-board-specimen-target"))
FIRST_M4 = "e08aa63"
BOARD_RUN = REPO / "scripts" / "board-run.py"


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def _harness():
    spec = importlib.util.spec_from_file_location(
        "ph", REPO / "scripts" / "perf-harness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(*args: str, cwd: Path = REPO) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


def ensure_worktree() -> None:
    if not (WORKTREE / ".git").exists():
        WORKTREE.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "worktree", "add", "--detach", str(WORKTREE), "HEAD"],
                       cwd=REPO, check=True)
    SHARED_TARGET.mkdir(parents=True, exist_ok=True)
    tgt = WORKTREE / "target"
    if not tgt.is_symlink():
        if tgt.exists():
            import shutil
            shutil.rmtree(tgt, ignore_errors=True)
        tgt.symlink_to(SHARED_TARGET)


def checkout(sha: str) -> None:
    subprocess.run(["git", "checkout", "--detach", "-f", sha], cwd=WORKTREE, check=True)
    subprocess.run(["git", "clean", "-qfdx", "--exclude=target"], cwd=WORKTREE, check=True)
    tgt = WORKTREE / "target"
    if not tgt.is_symlink():
        if tgt.exists():
            import shutil
            shutil.rmtree(tgt, ignore_errors=True)
        tgt.symlink_to(SHARED_TARGET)


def measure(sha: str) -> dict | None:
    """Flash+time the worktree's checked-out source; return per-tier cycles."""
    out = TMP / "board-result.json"
    if out.exists():
        out.unlink()
    cmd = [sys.executable, str(BOARD_RUN), "--target", "f429", "--all",
           "--no-verify", "--repo", str(WORKTREE), "--out", str(out)]
    log("  $ " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=REPO)
    if not out.exists():
        log(f"  board-run.py produced no result (rc={r.returncode})")
        return None
    return json.loads(out.read_text())


def main() -> int:
    LOG.write_text("")
    H = _harness()
    first = sys.argv[1] if len(sys.argv) > 1 else FIRST_M4
    last = sys.argv[2] if len(sys.argv) > 2 else "HEAD"

    raw = git("log", "--reverse", "--format=%h\x1f%aI\x1f%s", f"{first}^..{last}")
    commits = [tuple(x.split("\x1f")) for x in raw.splitlines() if x]

    # Group consecutive commits that build byte-identically (same key), exactly
    # as perf-replay does — one flash per distinct binary.
    groups: list[dict] = []
    for sha, date, subj in commits:
        key = H.build_key_rev(sha)
        if groups and groups[-1]["build_key"] == key:
            groups[-1]["covers"].append(sha)
        else:
            groups.append({"build_key": key, "commit": sha, "date": date,
                           "subject": subj, "covers": []})
    log(f"{len(commits)} commit(s) -> {len(groups)} distinct board binary state(s)")

    done = set()
    if OUT.exists():
        done = {json.loads(l)["commit"] for l in OUT.read_text().splitlines() if l.strip()}
        log(f"resume: {len(done)} group(s) already measured")

    ensure_worktree()
    t0 = time.monotonic()
    for i, g in enumerate(groups, 1):
        if g["commit"] in done:
            continue
        log(f"[{i}/{len(groups)}] {g['commit']} {g['subject'][:56]}")
        checkout(g["commit"])
        res = measure(g["commit"])
        rec = {"commit": g["commit"], "commit_date": g["date"],
               "subject": g["subject"], "build_key": g["build_key"],
               "covers": g["covers"], "board": res}
        with OUT.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        tiers = {t: v.get("cycles_per_frame", {}).get("median")
                 for t, v in ((res or {}).get("tiers") or {}).items()}
        log(f"    cycles/frame {tiers}  (elapsed {time.monotonic()-t0:.0f}s)")

    log(f"done -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

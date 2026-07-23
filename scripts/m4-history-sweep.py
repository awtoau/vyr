#!/usr/bin/env python3
"""Measure vyr's M4 insns/frame at EVERY commit that can run the M4 vehicle.

The project's headline metric — Draft-tier instructions per frame on the
emulated Cortex-M4, against the LVGL anchor — is computed by `dev.py qemu-m4`
and printed, then discarded. Neither ledger records it, so there is no history
of how the gap actually closed. This walks the commits and builds that history.

Method
------
Each commit is measured with ITS OWN dev.py (the harness evolved alongside the
renderer, so running today's harness against old code would be dishonest). The
walk happens in a detached git worktree so the primary checkout is never
touched, and CARGO_TARGET_DIR is shared across commits so builds are
incremental rather than 22 cold rebuilds.

Boundaries (discovered from history, not assumed):
  · e08aa63 — first commit with the M4 vehicle (vyr-size/src/workload.rs)
  · 5da42a2 — first commit with Quality::Draft and `dev.py qemu-m4 --draft`
Commits before those are skipped for the respective tier.

Resolution caveat: the vehicle reports SYS_CLOCK in centiseconds and 1 cs =
10^7 insns, so a 20-frame run resolves to +/-500,000 insns/frame. Repeat runs
at HEAD were bit-identical, so quantization — not host jitter — dominates.
Every row records the raw cs so the quantum is visible.

Writes:
  tmp/m4-history.jsonl    one row per commit
  tmp/m4-history-sweep.log
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp"
WORKTREE = Path("/mnt/2tb/git_debris/vyr-m4-sweep")
SHARED_TARGET = Path("/mnt/2tb/git_debris/vyr-m4-sweep-target")
OUT = TMP / "m4-history.jsonl"
LOG = TMP / "m4-history-sweep.log"

FIRST_M4 = "e08aa63"      # vyr-size/src/workload.rs appears
FIRST_DRAFT = "5da42a2"   # Quality::Draft + dev.py --draft appear

# "= 170,000,000 insns (8,500,000/frame, 62 insn/px)"
RE_INSNS = re.compile(r"([\d,]+) insns \(([\d,]+)/frame(?:, (\d+) insn/px)?")
# "est insns    17 cs for 20 warmed frames"
RE_CS = re.compile(r"(\d+) cs for (\d+) warmed frames")
RE_HASH = re.compile(r"frame hash\s+M4 (0x[0-9a-f]+)")
RE_HEAP = re.compile(r"heap peak\s+M4 (\d+) B")
RE_COV = re.compile(r"fast-path coverage ([\d.]+)%")

_lines: list[str] = []


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"{stamp} UTC  [m4-sweep] {msg}"
    print(line, flush=True)
    _lines.append(line)
    LOG.write_text("\n".join(_lines) + "\n")


def git(*args: str, cwd: Path = REPO) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def commits() -> list[tuple[str, str, str]]:
    """(sha, iso-date, subject) oldest→newest, from FIRST_M4 to HEAD."""
    raw = git("log", "--reverse", "--format=%h\x1f%aI\x1f%s", f"{FIRST_M4}^..HEAD")
    return [tuple(ln.split("\x1f")) for ln in raw.splitlines()]


def has_draft(sha: str) -> bool:
    """True once FIRST_DRAFT is an ancestor (inclusive)."""
    r = subprocess.run(
        ["git", "merge-base", "--is-ancestor", FIRST_DRAFT, sha], cwd=REPO
    )
    return r.returncode == 0


def measure(sha: str, draft: bool) -> dict | None:
    cmd = ["python3", "dev.py", "qemu-m4"] + (["--draft"] if draft else [])
    # NOTE: dev.py resolves the built binary as REPO/target/release-mcu/... and
    # ignores CARGO_TARGET_DIR, so the shared dir is wired in as a SYMLINK at
    # <worktree>/target instead of via the env var. Setting CARGO_TARGET_DIR
    # here would build into the shared dir but leave dev.py looking in a
    # target/ that does not exist.
    try:
        p = subprocess.run(
            cmd, cwd=WORKTREE, capture_output=True, text=True, timeout=1800
        )
    except subprocess.TimeoutExpired:
        log(f"    TIMEOUT ({'draft' if draft else 'exact'})")
        return None
    out = p.stdout + p.stderr
    m = RE_INSNS.search(out)
    if not m:
        log(f"    no insns parsed ({'draft' if draft else 'exact'}), rc={p.returncode}")
        return None
    cs = RE_CS.search(out)
    h = RE_HASH.search(out)
    heap = RE_HEAP.search(out)
    cov = RE_COV.search(out)
    return {
        "insns_per_frame": int(m.group(2).replace(",", "")),
        "insn_per_px": int(m.group(3)) if m.group(3) else None,
        "cs": int(cs.group(1)) if cs else None,
        "frames": int(cs.group(2)) if cs else None,
        "frame_hash": h.group(1) if h else None,
        "heap_peak": int(heap.group(1)) if heap else None,
        "coverage_pct": float(cov.group(1)) if cov else None,
    }


def main() -> None:
    TMP.mkdir(exist_ok=True)
    WORKTREE.parent.mkdir(parents=True, exist_ok=True)

    if WORKTREE.exists():
        log(f"reusing worktree {WORKTREE}")
    else:
        log(f"creating worktree {WORKTREE}")
        git("worktree", "add", "--detach", str(WORKTREE), "HEAD")

    todo = commits()
    log(f"{len(todo)} commit(s) to measure, {FIRST_M4}^..HEAD")
    log(f"shared target dir: {SHARED_TARGET}")

    rows = []
    for i, (sha, date, subj) in enumerate(todo, 1):
        log(f"[{i}/{len(todo)}] {sha}  {subj[:58]}")
        try:
            subprocess.run(
                ["git", "checkout", "--detach", "-f", sha],
                cwd=WORKTREE, capture_output=True, check=True,
            )
            subprocess.run(["git", "clean", "-qfd"], cwd=WORKTREE, capture_output=True)
            # re-assert the shared-target symlink: some commits ship a target/
            # entry, and checkout -f can replace the link with a real dir.
            tgt = WORKTREE / "target"
            if not tgt.is_symlink():
                subprocess.run(["rm", "-rf", str(tgt)], check=False)
                tgt.symlink_to(SHARED_TARGET)
        except subprocess.CalledProcessError as ex:
            log(f"    checkout FAILED: {ex}")
            continue

        exact = measure(sha, draft=False)
        draft = measure(sha, draft=True) if has_draft(sha) else None

        row = {
            "commit": sha,
            "date": date,
            "subject": subj,
            "exact": exact,
            "draft": draft,
        }
        rows.append(row)
        OUT.write_text("".join(json.dumps(r) + "\n" for r in rows))

        e = f"{exact['insns_per_frame']:,}" if exact else "—"
        d = f"{draft['insns_per_frame']:,}" if draft else "—"
        log(f"    exact={e}  draft={d}")

    log(f"done — {len(rows)} row(s) -> {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""board_lock.py — one exclusive lock for the one physical STM32F429I-DISC1.

Several runners in this repo (scripts/board-run.py, board-lcd.py,
board-ltdc.py, board-diag.py) drive the SAME board through the SAME ST-LINK.
Two of them talking to the probe at once does not fail cleanly: probe-rs and
openocd will each half-attach, one flash lands on top of the other, and the
numbers that come back belong to neither run. Worse, it is not obviously
wrong -- you get a plausible-looking cycle count for an image you did not
build.

So: every touch of the probe goes inside `with BoardLock(logf):`, held for
exactly one flash+run and released in a finally, never across a build or any
analysis.

The primitive is `os.mkdir`, because directory creation is atomic
create-and-test on every filesystem we care about, needs no daemon, and
survives a crashed writer without leaving a half-written PID file behind.
The owner's name and pid go in a file INSIDE the directory so a lock left
behind by a dead process is diagnosable rather than merely obstructive.
"""
import datetime
import os
import re
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _primary_checkout(repo: Path) -> Path:
    """The MAIN working tree's root, even when called from a linked worktree.

    Load-bearing, not tidiness: agents run in `git worktree` checkouts under
    .claude/worktrees/, and a lock at `<worktree>/tmp/.board.lock` is a
    different directory per agent -- i.e. a mutex that mutexes nothing while
    looking exactly like one that does. In a linked worktree `<root>/.git` is a
    FILE reading `gitdir: <main>/.git/worktrees/<name>`, so the main root is
    that path's third parent. Falls back to `repo` if anything is unexpected,
    which is the non-worktree case.
    """
    dotgit = repo / ".git"
    try:
        if dotgit.is_file():
            line = dotgit.read_text().strip()
            if line.startswith("gitdir:"):
                gitdir = Path(line.split(":", 1)[1].strip()).resolve()
                # <main>/.git/worktrees/<name> -> <main>
                if gitdir.parent.name == "worktrees":
                    return gitdir.parent.parent.parent
    except OSError:
        pass
    return repo


PRIMARY = _primary_checkout(REPO)
TMP = PRIMARY / "tmp"
LOCK = TMP / ".board.lock"

# How long a runner will queue behind another agent before giving up. A flash
# + run is under a minute, and an agent may legitimately have several queued,
# so 15 minutes is generous without hanging a session indefinitely.
LOCK_WAIT_S = 900
# A lock older than this whose owner pid is gone is stale. 10 minutes is well
# past the longest legitimate hold (one flash + one ~13 s timed workload).
LOCK_STALE_S = 600
# Poll interval while queued: a hold is tens of seconds, so 2 s costs at most
# 2 s of extra latency and one stat() per second of waiting.
POLL_S = 2


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class BoardLock:
    """Exclusive use of the physical board, for the shortest possible window.

    `log` is a callable taking one string; pass the runner's own logger so the
    wait and any stale-lock break land in that runner's log file.
    """

    def __init__(self, agent, log=print):
        self.agent = agent
        self.log = log
        self.held = False

    def __enter__(self):
        TMP.mkdir(exist_ok=True)
        deadline = time.monotonic() + LOCK_WAIT_S
        announced = False
        while True:
            try:
                LOCK.mkdir()
                (LOCK / "owner").write_text(
                    f"{self.agent} pid={os.getpid()} at={_now()}\n")
                self.held = True
                self.log(f"board lock ACQUIRED ({LOCK}) by {self.agent}")
                return self
            except FileExistsError:
                if not announced:
                    self.log(f"board lock held by another agent; waiting up to "
                             f"{LOCK_WAIT_S}s: {self.owner()}")
                    announced = True
                self._maybe_break()
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"board lock not acquired within {LOCK_WAIT_S}s; "
                        f"owner: {self.owner()}")
                time.sleep(POLL_S)

    def owner(self):
        try:
            return (LOCK / "owner").read_text().strip()
        except OSError:
            return "(unreadable)"

    def _maybe_break(self):
        """Break a lock only if it is old AND its owner process is gone.

        Both conditions, never one: a long-running flash is old but alive, and
        a pid can be recycled. Breaking is announced loudly because it means
        someone's run died and nobody noticed.
        """
        try:
            age = time.time() - LOCK.stat().st_mtime
        except OSError:
            return
        if age < LOCK_STALE_S:
            return
        owner = self.owner()
        m = re.search(r"pid=(\d+)", owner)
        if m and _pid_alive(int(m.group(1))):
            return
        self.log(f"*** BREAKING STALE BOARD LOCK: age {age:.0f}s > "
                 f"{LOCK_STALE_S}s and the owner process is dead. "
                 f"Owner record: {owner} ***")
        try:
            (LOCK / "owner").unlink(missing_ok=True)
            LOCK.rmdir()
        except OSError as e:
            self.log(f"could not break stale lock: {e}")

    def __exit__(self, *exc):
        if not self.held:
            return False
        try:
            (LOCK / "owner").unlink(missing_ok=True)
            LOCK.rmdir()
            self.log("board lock released")
        except OSError as e:
            self.log(f"WARNING: failed to release board lock: {e}")
        return False

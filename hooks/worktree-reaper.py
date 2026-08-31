#!/usr/bin/env python3
"""Worktree reaper — the cleanup-side counterpart to worktree-guard.py.

worktree-guard.py enforces the *creation* half of the worktree-isolation rule:
parallel sessions must isolate. Nothing enforced the *cleanup* half. Because
`.worktrees/` is ignored machine-wide (dotfiles-managed ~/.config/git/ignore),
stale worktrees never appear in `git status`, so they accumulate invisibly.

Measured 2026-08-02: local-meeting-notes had 52 worktrees totalling 74 GB, of
which 68 GB was Rust `target/`. Notably ZERO were merged into main. A reaper
that only removed merged worktrees would have reclaimed nothing. So the primary
action here is ARTIFACT RECLAMATION from idle worktrees; removing a fully-merged
worktree is the secondary path.

Two independent actions, different gates:

  1. reap artifacts — delete build dirs (target/, node_modules/, .next/, ...)
     from any linked worktree, when NOTHING INSIDE THAT BUILD DIR has been
     modified for ARTIFACT_IDLE_HOURS. Safe on dirty worktrees: build output is
     gitignored, so source edits are untouched (verified empirically —
     uncommitted .rs edits survived exactly this).
  2. remove worktree — `git worktree remove` when the branch is merged into the
     default branch AND the tree is clean AND nothing under it has been modified
     for REMOVE_IDLE_DAYS. The branch itself is always preserved; only the
     checkout goes.

Idleness is measured DEEP, not from a directory's own st_mtime. A directory's
mtime records entry changes in that directory alone, so a running cargo build
writing target/debug/*.o leaves both target/ and the worktree root reading days
idle. Since deleting target/ is this tool's primary action, the naive check would
delete a build tree out from under an active build. See recently_touched().

Safety gates, all fail-open EXCEPT the idleness check, which fails closed:
  - Only LINKED worktrees. The main checkout is never touched.
  - Only directories git itself reports as ignored (`git check-ignore`), so a
    repo that tracks a dir named `dist/` is never harmed.
  - Skips any worktree occupied by a live session, reusing worktree-guard.py's
    existing lock dir (<git-common-dir>/.claude-sessions) rather than inventing
    a second liveness model that could disagree with the first.
  - Honors `git worktree lock`, git's own don't-touch marker. `git worktree
    remove` already refuses a locked tree, which covered removal but left the
    PRIMARY path (rmtree of its build dirs) unguarded.
  - Honors the same `.guard-off` per-repo escape hatch, plus WORKTREE_REAPER_OFF=1.
  - Throttled per-repo via a stamp file: Codex's only hook event is `Stop`, which
    fires PER TURN, so an unthrottled reaper would run constantly. The stamp is
    skipped only when a retry could do better — i.e. the run was cut short AND
    freed something. Both neighbouring rules were wrong in opposite directions:
    stamping unconditionally let a hook killed mid-plan mark the repo reaped for
    six hours having reclaimed nothing, and skipping the stamp on any truncation
    livelocked a repo that truncates while freeing nothing, re-running every
    turn forever. plan() has no resume state, so a run that made no progress
    would simply repeat itself.
  - Bounded by a wall-clock DEADLINE_SECONDS that every subprocess is clipped to,
    so it stops itself and logs rather than being killed silently mid-work. See
    the budget arithmetic under "tunables". The stdin payload read is bounded
    separately, because it is the one blocking call that is not a subprocess.
  - Enumerates via `git worktree list`, NOT a `.worktrees/*` glob — Codex places
    its worktrees at ~/.codex/worktrees/<id>/<repo>, outside the repo entirely.
    A glob would silently skip every Codex worktree.

Modes (argv[1]):
  reap    — hook mode: throttled, silent, exits 0 always.
  report  — dry run: prints what it WOULD do, with sizes. Never deletes.
  closeout — SessionEnd mode: remove this linked worktree immediately when it is
             clean and merged; otherwise record the handoff state. It never
             removes a branch or a dirty, detached, locked, or main checkout.

Wiring (this repo installs only the Claude half):
  Claude Code : SessionEnd -> python3 ~/.claude/hooks/worktree-reaper.py reap
                installed by install.sh in this repo, timeout 60s. The extra
                slack over the deadline is for rmtree, which cannot be bounded.
  Codex       : Stop       -> python3 ~/.codex/hooks/worktree-reaper.py reap
                declared in the CONSUMING dotfiles repo, at
                files/home/.codex/hooks.json — Codex hook config is a single
                user-level file, not composable the way install.sh's ensure()
                is, so this repo does not write it. Adopting this hook under
                Codex means adding that entry there.
                Its timeout is 20s: the TIGHTEST budget this hook runs under,
                and therefore the one DEADLINE_SECONDS is derived from. Raising
                the Claude side without reading this one is how the deadline
                came to be set at double the harness limit on the per-turn path.

Pure stdlib. Log: ~/.claude/logs/worktree-reaper.log
"""
from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --- tunables (env-overridable) ----------------------------------------------

ARTIFACT_IDLE_HOURS = int(os.environ.get("WORKTREE_REAPER_ARTIFACT_IDLE_HOURS", "48"))
REMOVE_IDLE_DAYS = int(os.environ.get("WORKTREE_REAPER_REMOVE_IDLE_DAYS", "7"))
THROTTLE_HOURS = int(os.environ.get("WORKTREE_REAPER_THROTTLE_HOURS", "6"))

# Wall-clock budget, and the per-subprocess caps inside it.
#
# The binding constraint is the TIGHTEST harness timeout this hook runs under,
# which is Codex's `Stop` at 20s (files/home/.codex/hooks.json in the consuming
# dotfiles repo) — not Claude's SessionEnd at 60s (install.sh). An earlier
# version set the deadline to 40 and claimed the limits sat under the harness;
# under Codex they sat at double it, so the guarantee was backwards on the very
# path the per-turn throttle exists for.
#
# The arithmetic, so the claim is checkable rather than asserted:
#
#   every subprocess timeout is CLIPPED to the remaining budget (see clipped()),
#   so no call can outlive DEADLINE_SECONDS
#     => total wall clock <= DEADLINE_SECONDS + startup/teardown (~1s)
#     => 15 + 1 < 20, the Codex Stop budget.  Claude's 60 leaves slack for the
#        one genuinely unbounded step below.
#
#   STDIN_PAYLOAD_TIMEOUT is charged against the same budget rather than added
#   to it: _STARTED_AT is stamped at import, before the payload is read, so a
#   stalled stdin shortens the work that follows instead of extending the run.
#   That is the safe direction, and it is why the cap is 2s and not 10 — the
#   pathological case must not eat the headroom below.
#
#   REMOVE_TIMEOUT is the exception to clipping: a `git worktree remove` killed
#   part-way strands a half-deleted worktree that both gates then skip forever,
#   so it runs only when its FULL cap is still available.
#
#   That leaves 5s of headroom (15 - 10), which is genuinely narrow: a single
#   find hitting its cap consumes it, and artifact rmtree — unbounded by design —
#   crosses it on any large reclaim. So removal IS frequently skipped whenever
#   there is artifact work in the same pass. That is tolerable only because
#   artifact reclamation is the documented primary action and removal the
#   secondary one, and because skipping it no longer blocks the throttle (see
#   cmd_reap's progress test). Widening the gap means either a smaller
#   REMOVE_TIMEOUT — which risks the strand it exists to prevent — or a larger
#   DEADLINE_SECONDS, which is capped by Codex's 20s. Left as is, named rather
#   than papered over.
#
# Not covered: shutil.rmtree() is in-process and cannot be given a timeout. A
# multi-GB target/ can outlive the deadline and be killed by the harness. Its
# worst case is a partially deleted build dir — gitignored output, finished on
# the next pass — so it is left uncovered rather than pretended away.
DEADLINE_SECONDS = int(os.environ.get("WORKTREE_REAPER_DEADLINE_SECONDS", "15"))
# Measuring a tree costs ~0.5s. Skip it rather than eat into deletion time.
SIZE_MEASURE_RESERVE_SECONDS = 4.0
SIZE_MEASURE_TIMEOUT_SECONDS = 3
FIND_TIMEOUT = int(os.environ.get("WORKTREE_REAPER_FIND_TIMEOUT", "10"))
REMOVE_TIMEOUT = int(os.environ.get("WORKTREE_REAPER_REMOVE_TIMEOUT", "10"))
# The stdin payload is the one blocking read clipped() does not cover, because
# it is not a subprocess. A real harness writes a few dozen bytes and closes in
# microseconds, so nothing legitimate comes near this; it exists only to break a
# deadlock. See read_payload() for the failure it was measured against.
STDIN_PAYLOAD_TIMEOUT = float(os.environ.get("WORKTREE_REAPER_STDIN_TIMEOUT", "2"))
_STARTED_AT = time.time()
# Report mode runs from a shell, not under a harness, so it has no budget to
# respect — and a truncated dry run silently under-reports what a live reap
# would do, which is the operator's only verification surface.
_DEADLINE_ON = True

# Mirrors worktree-guard.py so the two agree on what "live" means.
STALE_SECONDS = 15 * 60
LOCK_DIRNAME = ".claude-sessions"
OVERRIDE_FILENAME = ".guard-off"
STAMP_FILENAME = ".last-reap"

# Build output only. Every candidate must ALSO be confirmed gitignored before
# deletion, so an unusual repo that tracks one of these names is never harmed.
ARTIFACT_DIRS = (
    "target",         # rust/cargo — the 68 GB case
    "node_modules",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".turbo",
    "dist",
    "build",
    ".venv",
)
# Deliberately NOT here: __pycache__. It appears at every depth, and this reaper
# only inspects the worktree root, so listing it would imply coverage it lacks.

# Overridable so a test can assert on the log without writing the real one.
LOG_PATH = Path(
    os.environ.get(
        "WORKTREE_REAPER_LOG",
        str(Path.home() / ".claude" / "logs" / "worktree-reaper.log"),
    )
)
CLOSEOUT_LOG_PATH = Path(
    os.environ.get(
        "WORKTREE_CLOSEOUT_LOG",
        str(Path.home() / ".claude" / "logs" / "worktree-closeout.log"),
    )
)


# --- helpers -----------------------------------------------------------------

def read_payload(note=None, timeout: float = STDIN_PAYLOAD_TIMEOUT) -> dict:
    """The hook payload from stdin, or {} if it does not arrive in `timeout`.

    This was `json.load(sys.stdin)`, which reads to EOF — and an INHERITED stdin
    may never deliver one. A TTY, or a pipe the parent holds open, parks the
    process indefinitely at 0% CPU with nothing written to any log, which is the
    hardest possible shape to diagnose: it does not look like a reaper failure,
    it looks like the machine stopped.

    Measured 2026-08-27: `python3 -m unittest discover` over this directory hung
    for 7+ minutes here. test_worktree_reaper's reap helper passed no stdin, so
    the child inherited the runner's, and the flake tracked nothing but whether
    that stdin happened to be at EOF — /dev/null exits in 0.13s, an open pipe
    never exits. The closeout helper passed `input=`, which closes the pipe, and
    so never hung. That asymmetry was the whole bug.

    Waiting on readiness alone would not be enough: a stream can go ready, hand
    over half a payload and stall, so the deadline covers the whole read.

    Fails toward {} on every uncertain path, like the rest of this file — but
    unlike the gates above, {} here is not always harmless, so it is logged.
    cmd_reap falls back to os.getcwd(), which is the directory the harness would
    have named anyway. cmd_closeout uses payload["cwd"] to identify WHICH
    worktree just ended; falling back there reads the repo root and logs
    `state=main action=keep` instead of removing a merged worktree. Hence a cap
    a real harness cannot reach rather than a tight one.
    """
    try:
        fd = sys.stdin.fileno()
    except Exception:
        return {}
    deadline = time.time() + timeout
    chunks: list[bytes] = []
    while True:
        left = deadline - time.time()
        if left <= 0:
            if note:
                note(f"stdin payload did not arrive within {timeout}s; "
                     "continuing without it")
            return {}
        try:
            if not select.select([fd], [], [], left)[0]:
                continue
            chunk = os.read(fd, 65536)
        except Exception:
            return {}
        if not chunk:
            break
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except Exception:
        return {}


# `clip` defaults to True so the budget holds without every call site
# remembering — a bound that depends on discipline at a dozen call sites is not a
# bound. Pass clip=False only where being cut short is worse than overrunning.
def git(args: list[str], cwd: str, timeout: int = 10, clip: bool = True) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=clipped(timeout) if clip else timeout,
        )
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def git_ok(args: list[str], cwd: str, timeout: int = 10, clip: bool = True) -> bool:
    try:
        r = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=clipped(timeout) if clip else timeout,
        )
    except Exception:
        return False
    return r.returncode == 0


def common_dir(cwd: str) -> str | None:
    if not os.path.isdir(cwd):
        return None
    c = git(["rev-parse", "--git-common-dir"], cwd)
    if c is None:
        return None
    return os.path.abspath(os.path.join(cwd, c))


def default_branch(cwd: str) -> str | None:
    """Best-effort default branch: origin/HEAD, else main, else master."""
    head = git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd)
    if head:
        return head.rsplit("/", 1)[-1]
    for cand in ("main", "master"):
        if git_ok(["rev-parse", "--verify", "--quiet", cand], cwd):
            return cand
    return None


def all_worktrees(cwd: str) -> list[dict] | None:
    """All worktrees via `git worktree list --porcelain`, or None on failure.

    Deliberately not a `.worktrees/*` glob: Codex worktrees live outside the
    repo and would be missed entirely.

    None and [] are different answers and collapsing them was a stamp bug. git()
    returns None on a non-zero exit OR a timeout — index-lock contention, a
    clipped budget, a broken repo — and returning [] for that made "could not
    enumerate" indistinguishable from "this repo has no linked worktrees". The
    caller then read it as nothing-to-do and suppressed the repo for six hours,
    which is the same did-not-finish-yet-stamped class this file fixes elsewhere.
    """
    out = git(["worktree", "list", "--porcelain"], cwd)
    if out is None:
        return None
    if not out:
        return []
    entries: list[dict] = []
    cur: dict = {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if cur.get("path"):
                entries.append(cur)
            cur = {
                "path": line[len("worktree "):].strip(),
                "branch": None,
                "detached": False,
                "locked": False,
            }
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].strip().replace("refs/heads/", "")
        elif line.strip() == "detached":
            cur["detached"] = True
        elif line.startswith("bare"):
            cur["bare"] = True
        elif line.startswith("locked"):
            # git's own don't-touch marker. `git worktree remove` already refuses
            # a locked worktree without --force, which protected the SECONDARY
            # path while leaving the primary one — rmtree of its build dirs —
            # completely unguarded. Honour it for both.
            cur["locked"] = True
    if cur.get("path"):
        entries.append(cur)
    if not entries:
        return []
    return entries


def worktrees(cwd: str) -> list[dict] | None:
    """All LINKED worktrees, excluding the main checkout and bare entries."""
    entries = all_worktrees(cwd)
    if entries is None:
        return None
    # First entry is the main checkout — never touched by the reaper.
    return [entry for entry in entries[1:] if not entry.get("bare")]


def live_session_dirs(cd: str) -> list[str]:
    """cwds of sessions whose heartbeat lock is still fresh."""
    ld = Path(cd) / LOCK_DIRNAME
    now = time.time()
    out: list[str] = []
    try:
        entries = list(ld.glob("*.json"))
    except Exception:
        return out
    for f in entries:
        try:
            if now - f.stat().st_mtime > STALE_SECONDS:
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
            c = data.get("cwd")
            if isinstance(c, str) and c:
                out.append(os.path.abspath(c))
        except Exception:
            continue
    return out


def budget_left() -> float:
    """Seconds remaining before this run must stop. Infinite in report mode."""
    if not _DEADLINE_ON:
        return float("inf")
    return DEADLINE_SECONDS - (time.time() - _STARTED_AT)


def past_deadline() -> bool:
    """True once this run has used its wall-clock budget.

    The hook harness kills the process at its configured timeout, mid-work and
    without a log line, so this stops it first and reports what it did.

    Checking this ONCE per worktree does not bound anything: everything after the
    check — up to nine artifact scans plus three git calls — ran at full cap, a
    per-iteration worst case in the hundreds of seconds against a budget of tens.
    It is now checked before every subprocess, and every subprocess timeout is
    additionally clipped to what is left, so the budget holds even if a check is
    ever missed.
    """
    return budget_left() <= 0


def clipped(cap: int) -> int:
    """A subprocess timeout that cannot outlive the run's remaining budget.

    Clipping is safe for every caller here because each fails toward doing
    nothing: a truncated find reports "active" (skip), a truncated check-ignore
    reports "not ignored" (skip), a truncated `git status` returns None and is
    read as dirty (skip). `git worktree remove` is deliberately NOT clipped —
    interrupting it mid-delete strands the worktree — so it is gated on having
    its whole cap available instead.
    """
    if not _DEADLINE_ON:
        return cap
    return max(1, min(cap, int(budget_left())))


def recently_touched(path: str, hours: float, prune: tuple[str, ...] = ()) -> bool:
    """True if ANYTHING anywhere under `path` was modified within `hours`.

    A directory's own mtime is not this answer. It records entry changes in that
    one directory only, so a cargo build writing `target/debug/*.o` leaves both
    `target/` and the worktree root reading days idle. Verified:

        backdate wt/, wt/target/, wt/target/debug/ to 5 days ago
        write wt/target/debug/build.o
        -> wt/ 120.0h idle, wt/target/ 120.0h idle, wt/target/debug/ 0.0h

    This reaper's PRIMARY action is deleting `target/`, so gating that on the
    root's st_mtime deletes a build tree out from under a running build. The same
    blind spot cost a live browser profile elsewhere on 2026-08-02: an open-handle
    check said idle, and only a deep timestamp check saw the work in progress.

    `find -newermt ... -print -quit` stops at the first hit, but the answer that
    matters here is "idle", and an idle tree has no hit to stop at — so the cost
    of the deciding case is a FULL walk. Measured 2026-08-03 on this machine
    (warm cache), which is why FIND_TIMEOUT of 10s is not tight:

        rally-hq/node_modules        79,111 files   974 MB   full walk 0.40s
        creative-floors/node_modules 78,452 files   971 MB   full walk 0.47s
        local-meeting-notes target/  41,646 files   8.5 GB   full walk 0.22s

    Size is nearly irrelevant; file COUNT is the cost, and a Rust target/ is
    mostly a few large artifacts. Roughly 20x headroom warm.

    Fails CLOSED — an unreadable or slow tree reports "active". This gates a
    deletion, and the cost of a false "active" is deferred cleanup, while the cost
    of a false "idle" is destroying work. Failing closed is silent by nature,
    though: "active" and "could not tell" produce identical output and the run
    would report `artifacts=0` either way. So the uncertain paths log.
    """
    cmd = ["find", path]
    for name in prune:
        cmd += ["-name", name, "-prune", "-o"]
    cmd += ["-newermt", f"-{hours} hours", "-print", "-quit"]
    budget = clipped(FIND_TIMEOUT)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=budget)
    except subprocess.TimeoutExpired:
        log(f"idleness scan exceeded {budget}s, treating as active: {path}")
        return True
    except Exception as exc:
        log(f"idleness scan failed ({exc.__class__.__name__}), treating as active: {path}")
        return True
    if r.returncode != 0:
        log(f"idleness scan exited {r.returncode}, treating as active: {path}")
        return True
    return bool(r.stdout.strip())


def is_ignored(wt: str, name: str) -> bool:
    """True only if git itself considers this path ignored."""
    return git_ok(["check-ignore", "-q", name], wt, timeout=5)


def dir_size_kb(path: str, timeout: int = 120) -> int:
    """Size of a tree, or 0 when it cannot be measured in the time allowed.

    Report mode can afford the default. The reap path passes a short timeout and
    treats 0 as "unmeasured", because a wrong number in the log is worse than an
    honest gap: the log is the only record of what this hook did.
    """
    try:
        r = subprocess.run(["du", "-sk", path], capture_output=True, text=True, timeout=timeout)
        return int(r.stdout.split()[0]) if r.returncode == 0 else 0
    except Exception:
        return 0


def log(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {msg}\n")
    except Exception:
        pass


def log_closeout(msg: str) -> None:
    """Best-effort SessionEnd record kept separate from periodic reaping."""
    try:
        CLOSEOUT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with CLOSEOUT_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {msg}\n")
    except Exception:
        pass


# --- throttle ----------------------------------------------------------------

def throttled(cd: str) -> bool:
    """True if we reaped this repo recently. Codex fires Stop per TURN."""
    stamp = Path(cd) / LOCK_DIRNAME / STAMP_FILENAME
    try:
        if time.time() - stamp.stat().st_mtime < THROTTLE_HOURS * 3600:
            return True
    except Exception:
        pass
    return False


def touch_stamp(cd: str) -> None:
    try:
        p = Path(cd) / LOCK_DIRNAME
        p.mkdir(parents=True, exist_ok=True)
        (p / STAMP_FILENAME).write_text(str(int(time.time())), encoding="utf-8")
    except Exception:
        pass


# --- core --------------------------------------------------------------------

def cmd_closeout(payload: dict) -> None:
    """Close the current linked worktree when the session has safely finished.

    This is intentionally a narrow completion path rather than another reaping
    pass: no idle window, no artifact deletion, no branch deletion, and no
    remote fetch. It only inspects the worktree from the ending session and
    removes its checkout after proving the local default branch contains it.
    """
    if os.environ.get("WORKTREE_REAPER_OFF") == "1":
        return
    cwd = os.path.abspath(payload.get("cwd") or os.getcwd())
    cd = common_dir(cwd)
    if cd is None or (Path(cd) / LOCK_DIRNAME / OVERRIDE_FILENAME).exists():
        return

    root = git(["rev-parse", "--show-toplevel"], cwd, timeout=5)
    entries = all_worktrees(cwd)
    if root is None or entries is None:
        log_closeout(f"repo={cwd} state=unknown action=skip reason=enumeration_failed")
        return

    root = os.path.abspath(root)
    current_index = next(
        (
            index
            for index, entry in enumerate(entries)
            if os.path.abspath(str(entry.get("path", ""))) == root
        ),
        None,
    )
    if current_index is None:
        log_closeout(f"repo={root} state=unknown action=skip reason=not_listed")
        return
    if current_index == 0:
        log_closeout(f"repo={root} state=main action=keep")
        return

    current = entries[current_index]
    branch = current.get("branch")
    if current.get("bare") or current.get("locked") or current.get("detached") or not branch:
        reason = (
            "bare"
            if current.get("bare")
            else "locked"
            if current.get("locked")
            else "detached"
            if current.get("detached")
            else "no_branch"
        )
        log_closeout(f"repo={root} state={reason} action=keep")
        return

    status = git(["status", "--porcelain", "--untracked-files=normal"], root, timeout=10)
    if status is None:
        log_closeout(f"repo={root} branch={branch} state=unknown action=keep reason=status_failed")
        return
    if status.strip():
        log_closeout(f"repo={root} branch={branch} state=dirty action=handoff")
        return

    dflt = default_branch(root)
    if not dflt:
        log_closeout(f"repo={root} branch={branch} state=clean action=keep reason=default_unknown")
        return
    if not git_ok(["merge-base", "--is-ancestor", branch, dflt], root, timeout=10):
        log_closeout(
            f"repo={root} branch={branch} default={dflt} state=unmerged-clean "
            "action=open-pr-or-hold"
        )
        return

    primary = os.path.abspath(str(entries[0].get("path", "")))
    if not primary or not os.path.isdir(primary):
        log_closeout(f"repo={root} branch={branch} state=merged-clean action=keep reason=primary_missing")
        return
    if git_ok(["worktree", "remove", root], primary, timeout=REMOVE_TIMEOUT, clip=False):
        log_closeout(f"repo={root} branch={branch} state=merged-clean action=removed")
        return
    log_closeout(f"repo={root} branch={branch} state=merged-clean action=keep reason=remove_failed")


def plan(
    cwd: str, cd: str, wts: list[dict]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], bool]:
    """Compute (artifact_targets, removable_worktrees, truncated) without mutating.

    artifact_targets  : [(worktree_path, artifact_abs_path)]
    removable_worktrees: [(worktree_path, branch)]
    truncated         : the budget ran out, so the lists are a PREFIX of the
                        repo. Callers must not read an empty list as "nothing to
                        do" — that is how a repo gets stamped as reaped having
                        looked at four of its fifty-two worktrees.

    There is NO resume state: this restarts at the head of `git worktree list`
    every run, so truncation always re-walks the same prefix. A previous commit
    message claimed a truncated run "picks up where it stopped" — it does not.
    Progress comes only from the prefix getting cheaper as its artifacts are
    deleted, which is why the caller stamps on a run that made no progress
    rather than retrying an identical computation forever.
    """
    artifacts: list[tuple[str, str]] = []
    removable: list[tuple[str, str]] = []
    truncated = False

    live = live_session_dirs(cd)
    me = os.path.abspath(cwd)
    dflt = default_branch(cwd)

    for wt in wts:
        path = os.path.abspath(wt["path"])
        if not os.path.isdir(path):
            continue
        # Occupied by this session or another live one? Leave it alone.
        if me == path or me.startswith(path + os.sep):
            continue
        if any(d == path or d.startswith(path + os.sep) for d in live):
            continue
        if wt.get("locked"):
            continue
        if past_deadline():
            truncated = True
            break

        # Idleness is measured on the artifact dir ITSELF, deeply. The worktree
        # root's mtime does not move when a build writes into target/**, so the
        # previous root-level gate would have deleted a target/ mid-build.
        for name in ARTIFACT_DIRS:
            target = os.path.join(path, name)
            if not os.path.isdir(target):
                continue
            if past_deadline():
                truncated = True
                break
            if not is_ignored(path, name):
                continue
            if recently_touched(target, ARTIFACT_IDLE_HOURS):
                continue
            artifacts.append((path, target))
        if truncated:
            break

        # Cheap gates BEFORE the deep scan. `and` short-circuits left to right,
        # and recently_touched() is a full-tree find while merge-base is a
        # sub-millisecond rev-walk that rejects almost everything: zero of the 52
        # worktrees that motivated this tool were merged. Ordered the other way,
        # every worktree paid a complete deep walk only to fail the next gate.
        # (Measured on 40 worktrees: the deep finds cost 0.14s total, merge-base
        # 0.61s — so this ordering is discipline, not a measured speedup.)
        #
        # `git status` returning None means the command FAILED, and `None or ""`
        # made that read as "clean" — inverting this file's own rule that
        # uncertainty skips. An unreadable tree is treated as dirty.
        branch = wt.get("branch")
        if not (dflt and branch and not wt.get("detached") and branch != dflt):
            continue
        if past_deadline():
            truncated = True
            break
        if not git_ok(["merge-base", "--is-ancestor", branch, dflt], cwd, timeout=10):
            continue
        if past_deadline():
            truncated = True
            break
        status = git(["status", "--porcelain"], path, timeout=10)
        if status is None or status.strip():
            continue
        if past_deadline():
            truncated = True
            break
        # Scan with the build dirs excluded — they are the bulk of a worktree and
        # are already handled above, so including them is redundant work on the
        # one path where the artifact scan has already answered the question.
        # (An earlier comment justified this prune by claiming a multi-GB idle
        # tree "hit the find timeout and failed closed". Measurement contradicts
        # that: see recently_touched(), where the largest tree on this machine
        # full-walks in 0.47s. The claim was never observed, and a reviewer later
        # cited it back as evidence for a defect that does not exist.)
        if recently_touched(path, REMOVE_IDLE_DAYS * 24, prune=ARTIFACT_DIRS):
            continue
        removable.append((path, branch))

    return artifacts, removable, truncated


def cmd_reap(payload: dict) -> None:
    if os.environ.get("WORKTREE_REAPER_OFF") == "1":
        sys.exit(0)
    cwd = payload.get("cwd") or os.getcwd()
    cd = common_dir(cwd)
    if cd is None:
        sys.exit(0)
    if (Path(cd) / LOCK_DIRNAME / OVERRIDE_FILENAME).exists():
        sys.exit(0)
    if throttled(cd):
        sys.exit(0)

    # Enumeration failure is not "no worktrees". git() returns None on a non-zero
    # exit or a timeout, and reading that as an empty repo stamped a six-hour
    # suppression on a transient index-lock collision, silently. Retrying costs
    # one git call, so it retries — and says so, because a repo that fails to
    # enumerate every turn is a real problem that should not be inferred from a
    # reaper that never seems to run.
    wts = worktrees(cwd)
    if wts is None:
        log(f"repo={cwd} could not enumerate worktrees, not stamping")
        sys.exit(0)

    try:
        artifacts, removable, truncated = plan(cwd, cd, wts)
    except Exception as exc:
        # Stamping happens only after work completes, so a failure here retries
        # on the next session end instead of suppressing the repo for six hours.
        log(f"repo={cwd} plan failed: {exc.__class__.__name__}: {exc}")
        sys.exit(0)
    if not artifacts and not removable:
        # Nothing to do, whether or not the budget ran out. plan() has no resume
        # state, so a truncated run that found nothing will find nothing again
        # next turn from an identical starting point — declining to stamp there
        # buys no progress and, under Codex's per-turn Stop, re-runs forever.
        # Stamp, and log when the answer was a prefix rather than the whole repo.
        if truncated:
            log(f"repo={cwd} budget {DEADLINE_SECONDS}s exhausted before finding "
                f"work; stamping anyway (no resume state — a retry recomputes "
                f"the same prefix). Run `worktree-reaper.py report` by hand.")
        touch_stamp(cd)
        sys.exit(0)

    # Gates that plan() evaluated are re-read here, because the plan phase is the
    # long window: a session that starts inside a worktree, or a `git worktree
    # lock` taken while planning, would otherwise still lose its build dirs. The
    # per-item recently_touched() re-check below covers the deletion phase too;
    # these two cover the plan->delete gap, which is the wide one.
    live_now = live_session_dirs(cd)
    wts_now = worktrees(cwd)
    # None here means the lock state cannot be read at all. This gates deletion,
    # so it fails CLOSED: without knowing what is locked, nothing is eligible.
    locked_now = (
        {os.path.abspath(w["path"]) for w in wts_now if w.get("locked")}
        if wts_now is not None
        else None
    )
    if locked_now is None:
        log(f"repo={cwd} lost worktree enumeration before deleting; skipping this pass")

    def still_eligible(wt_path: str) -> bool:
        if locked_now is None or wt_path in locked_now:
            return False
        return not any(d == wt_path or d.startswith(wt_path + os.sep) for d in live_now)

    # Sum the trees actually deleted. The previous accounting diffed volume free
    # space across the run, which any concurrent writer contaminates: measured
    # 2026-08-15, a pass that deleted a 1017 MB node_modules logged freed=24MB
    # and one that deleted 581 MB logged freed=0MB, because agents were building
    # on the same volume. A log that understates its own work reads as a hook
    # that is not running.
    freed_kb = 0
    unmeasured = False
    n_art = 0
    for wt_path, target in artifacts:
        if past_deadline():
            truncated = True
            break
        if not still_eligible(wt_path):
            continue
        # Re-check immediately before deleting. plan() evaluated every worktree
        # up front, so its reading for the first entry is stale by however long
        # the whole plan phase took — and a build started in that window would
        # lose its tree, the exact failure the deep check exists to prevent.
        if recently_touched(target, ARTIFACT_IDLE_HOURS):
            continue
        # Measure before deleting, and only with budget to spare — a full walk of
        # a node_modules runs ~0.5s. No budget means no number, never a guess.
        if budget_left() > SIZE_MEASURE_RESERVE_SECONDS:
            kb = dir_size_kb(target, timeout=SIZE_MEASURE_TIMEOUT_SECONDS)
            if kb:
                freed_kb += kb
            else:
                unmeasured = True
        else:
            unmeasured = True
        try:
            shutil.rmtree(target)
            n_art += 1
        except Exception:
            pass
    n_wt = 0
    for path, _branch in removable:
        # Removal is the one call that must not be cut short. A `git worktree
        # remove` SIGKILLed part-way leaves a half-deleted checkout with its
        # admin entry intact: `git status` there then reports mass deletions, so
        # the dirty gate skips it forever, and the artifact gate has nothing left
        # to find. Unreachable by both paths. So it runs only with its whole cap
        # in hand, and the run truncates rather than starting one it cannot
        # finish.
        if budget_left() < REMOVE_TIMEOUT:
            truncated = True
            break
        if not still_eligible(path):
            continue
        # No --force: a tree that turned dirty since planning must survive.
        if git_ok(["worktree", "remove", path], cwd, timeout=REMOVE_TIMEOUT, clip=False):
            n_wt += 1
    if n_wt:
        git(["worktree", "prune"], cwd)

    # The throttle is skipped only when a retry can do better, and the test for
    # that is PROGRESS, not truncation.
    #
    # Gating on truncation alone was a livelock. Missing the removal budget gate
    # sets truncated, and with only 5s of headroom between REMOVE_TIMEOUT and
    # DEADLINE_SECONDS that happens readily — one find hitting its cap is enough.
    # A run that deleted nothing then declined to stamp, and since plan() has no
    # resume state the next turn recomputed exactly the same thing and declined
    # again. Under Codex's per-turn Stop that is every turn, forever, with zero
    # work done. Before this file gained a conditional stamp the throttle always
    # armed, so the regression made the pathological repos the ones that never
    # throttle.
    #
    # With progress as the test it converges: a run that freed something leaves
    # less work behind, so the next one gets further; a run that freed nothing
    # would repeat itself, so it arms the throttle and logs why.
    progressed = n_art or n_wt
    if not (truncated and progressed):
        touch_stamp(cd)
    log(
        f"repo={cwd} artifacts={n_art} worktrees_removed={n_wt} "
        f"freed{'>=' if unmeasured else '='}{freed_kb // 1024}MB "
        f"elapsed={time.time() - _STARTED_AT:.1f}s"
        + (
            f" TRUNCATED at {DEADLINE_SECONDS}s budget"
            + (", not stamped (progress made, retry next session)" if progressed
               else ", stamped anyway (no progress — a retry would repeat it)")
            if truncated else ""
        )
    )
    sys.exit(0)


def cmd_report(payload: dict) -> None:
    # No budget in report mode. cmd_report shares plan() with the hook, so the
    # deadline truncated the DRY RUN too — while the header below went on
    # printing the true worktree count, so the output claimed full coverage over
    # a prefix, with nothing marking the cut. This is the operator's only
    # verification surface and it is run by hand, where slow beats wrong.
    global _DEADLINE_ON
    _DEADLINE_ON = False

    cwd = payload.get("cwd") or os.getcwd()
    cd = common_dir(cwd)
    if cd is None:
        print("not a git repo:", cwd)
        sys.exit(0)
    wts = worktrees(cwd)
    if wts is None:
        print("could not enumerate worktrees (`git worktree list` failed):", cwd)
        sys.exit(1)
    artifacts, removable, _ = plan(cwd, cd, wts)

    print(f"repo: {cwd}")
    print(
        f"gates: artifacts idle>{ARTIFACT_IDLE_HOURS}h, "
        f"remove merged+clean idle>{REMOVE_IDLE_DAYS}d, throttle {THROTTLE_HOURS}h"
    )
    print(f"linked worktrees: {len(wts)}")
    if throttled(cd):
        print("NOTE: currently throttled — a live `reap` would no-op right now.")
    print()

    total = 0
    print(f"artifact dirs to delete ({len(artifacts)}):")
    for wtp, target in artifacts:
        kb = dir_size_kb(target)
        total += kb
        print(f"  {kb // 1024:>6} MB  {os.path.relpath(target, os.path.dirname(wtp))}")
    print(f"  --> {total // 1024} MB ({total / 1024 / 1024:.1f} GB) reclaimable")
    print()
    print(f"worktrees to remove (merged+clean+idle) ({len(removable)}):")
    for path, branch in removable:
        print(f"  {os.path.basename(path)}  [{branch}]")
    if not removable:
        print("  (none)")
    sys.exit(0)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "reap"
    if mode == "report":
        # Never touches stdin: run by hand from a shell, not by a hook.
        cmd_report({"cwd": sys.argv[2] if len(sys.argv) > 2 else os.getcwd()})
    elif mode == "closeout":
        cmd_closeout(read_payload(log_closeout))
    else:
        cmd_reap(read_payload(log))


if __name__ == "__main__":
    main()

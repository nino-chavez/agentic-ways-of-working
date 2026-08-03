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
    written only AFTER the work finishes — writing it first meant a hook killed
    mid-plan marked the repo reaped for six hours having reclaimed nothing.
  - Bounded by a wall-clock DEADLINE_SECONDS below the hook's own timeout, so it
    stops itself and logs rather than being killed silently mid-work.
  - Enumerates via `git worktree list`, NOT a `.worktrees/*` glob — Codex places
    its worktrees at ~/.codex/worktrees/<id>/<repo>, outside the repo entirely.
    A glob would silently skip every Codex worktree.

Modes (argv[1]):
  reap    — hook mode: throttled, silent, exits 0 always.
  report  — dry run: prints what it WOULD do, with sizes. Never deletes.

Wiring (this repo installs only the Claude half):
  Claude Code : SessionEnd -> python3 ~/.claude/hooks/worktree-reaper.py reap
                installed by install.sh in this repo.
  Codex       : Stop       -> python3 ~/.codex/hooks/worktree-reaper.py reap
                declared in the CONSUMING dotfiles repo, at
                files/home/.codex/hooks.json — Codex hook config is a single
                user-level file, not composable the way install.sh's ensure()
                is, so this repo does not write it. Adopting this hook under
                Codex means adding that entry there.

Pure stdlib. Log: ~/.claude/logs/worktree-reaper.log
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --- tunables (env-overridable) ----------------------------------------------

ARTIFACT_IDLE_HOURS = int(os.environ.get("WORKTREE_REAPER_ARTIFACT_IDLE_HOURS", "48"))
REMOVE_IDLE_DAYS = int(os.environ.get("WORKTREE_REAPER_REMOVE_IDLE_DAYS", "7"))
THROTTLE_HOURS = int(os.environ.get("WORKTREE_REAPER_THROTTLE_HOURS", "6"))

# Wall-clock budget, and the per-subprocess cap inside it. Both must stay BELOW
# the hook's own timeout (see install.sh), or the harness kills this process
# mid-work with nothing logged and no way to tell a slow run from a broken one.
DEADLINE_SECONDS = int(os.environ.get("WORKTREE_REAPER_DEADLINE_SECONDS", "40"))
FIND_TIMEOUT = int(os.environ.get("WORKTREE_REAPER_FIND_TIMEOUT", "10"))
_STARTED_AT = time.time()

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

LOG_PATH = Path.home() / ".claude" / "logs" / "worktree-reaper.log"


# --- helpers -----------------------------------------------------------------

def read_payload() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def git(args: list[str], cwd: str, timeout: int = 10) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def git_ok(args: list[str], cwd: str, timeout: int = 10) -> bool:
    try:
        r = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=timeout,
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


def worktrees(cwd: str) -> list[dict]:
    """All LINKED worktrees via `git worktree list --porcelain`.

    Deliberately not a `.worktrees/*` glob: Codex worktrees live outside the
    repo and would be missed entirely.
    """
    out = git(["worktree", "list", "--porcelain"], cwd)
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
    # First entry is the main checkout — never touched.
    return [e for e in entries[1:] if not e.get("bare")]


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


def past_deadline() -> bool:
    """True once this run has used its wall-clock budget.

    The hook harness kills the process at its configured timeout, mid-work and
    without a log line. A reaper that walks 39 worktrees with a find apiece can
    reach that, so it stops itself first and reports what it did. Checked between
    worktrees and between deletions, never inside one.
    """
    return time.time() - _STARTED_AT > DEADLINE_SECONDS


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

    `find -newermt ... -print -quit` stops at the first hit, so this stays cheap
    even on a multi-GB tree.

    Fails CLOSED — an unreadable or slow tree reports "active". This gates a
    deletion, and the cost of a false "active" is deferred cleanup, while the cost
    of a false "idle" is destroying work.
    """
    cmd = ["find", path]
    for name in prune:
        cmd += ["-name", name, "-prune", "-o"]
    cmd += ["-newermt", f"-{hours} hours", "-print", "-quit"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=FIND_TIMEOUT)
        if r.returncode != 0:
            return True
        return bool(r.stdout.strip())
    except Exception:
        return True


def is_ignored(wt: str, name: str) -> bool:
    """True only if git itself considers this path ignored."""
    return git_ok(["check-ignore", "-q", name], wt, timeout=5)


def dir_size_kb(path: str) -> int:
    """Only used in report mode — du is slow and never runs in the hook path."""
    try:
        r = subprocess.run(["du", "-sk", path], capture_output=True, text=True, timeout=120)
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

def plan(cwd: str, cd: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Compute (artifact_targets, removable_worktrees) without mutating anything.

    artifact_targets  : [(worktree_path, artifact_abs_path)]
    removable_worktrees: [(worktree_path, branch)]
    """
    artifacts: list[tuple[str, str]] = []
    removable: list[tuple[str, str]] = []

    live = live_session_dirs(cd)
    me = os.path.abspath(cwd)
    dflt = default_branch(cwd)

    for wt in worktrees(cwd):
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
            break

        # Idleness is measured on the artifact dir ITSELF, deeply. The worktree
        # root's mtime does not move when a build writes into target/**, so the
        # previous root-level gate would have deleted a target/ mid-build.
        for name in ARTIFACT_DIRS:
            target = os.path.join(path, name)
            if not (os.path.isdir(target) and is_ignored(path, name)):
                continue
            if recently_touched(target, ARTIFACT_IDLE_HOURS):
                continue
            artifacts.append((path, target))

        # Cheap gates BEFORE the deep scan. `and` short-circuits left to right,
        # and recently_touched() is a full-tree find while merge-base is a
        # sub-millisecond rev-walk that rejects almost everything: zero of the 52
        # worktrees that motivated this tool were merged. Ordered the other way,
        # every worktree paid a complete deep walk only to fail the next gate.
        #
        # `git status` returning None means the command FAILED, and `None or ""`
        # made that read as "clean" — inverting this file's own rule that
        # uncertainty skips. An unreadable tree is treated as dirty.
        branch = wt.get("branch")
        if not (dflt and branch and not wt.get("detached") and branch != dflt):
            continue
        if not git_ok(["merge-base", "--is-ancestor", branch, dflt], cwd):
            continue
        status = git(["status", "--porcelain"], path)
        if status is None or status.strip():
            continue
        # Scan with the build dirs excluded. They are the bulk of a worktree and
        # are already handled above; including them meant a multi-GB idle tree
        # found no early exit, hit the find timeout, failed closed, and could
        # therefore never become removable.
        if recently_touched(path, REMOVE_IDLE_DAYS * 24, prune=ARTIFACT_DIRS):
            continue
        removable.append((path, branch))

    return artifacts, removable


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

    try:
        artifacts, removable = plan(cwd, cd)
    except Exception as exc:
        # Stamping happens only after work completes, so a failure here retries
        # on the next session end instead of suppressing the repo for six hours.
        log(f"repo={cwd} plan failed: {exc.__class__.__name__}: {exc}")
        sys.exit(0)
    if not artifacts and not removable:
        touch_stamp(cd)
        sys.exit(0)

    before = shutil.disk_usage(cwd).free
    n_art = 0
    for _, target in artifacts:
        if past_deadline():
            break
        # Re-check immediately before deleting. plan() evaluated every worktree
        # up front, so its reading for the first entry is stale by however long
        # the whole plan phase took — and a build started in that window would
        # lose its tree, the exact failure the deep check exists to prevent.
        if recently_touched(target, ARTIFACT_IDLE_HOURS):
            continue
        try:
            shutil.rmtree(target)
            n_art += 1
        except Exception:
            pass
    n_wt = 0
    for path, _branch in removable:
        if past_deadline():
            break
        # No --force: a tree that turned dirty since planning must survive, and
        # neither may a worktree someone locked in the meantime.
        if git_ok(["worktree", "remove", path], cwd, timeout=FIND_TIMEOUT):
            n_wt += 1
    if n_wt:
        git(["worktree", "prune"], cwd)
    freed = max(0, shutil.disk_usage(cwd).free - before)

    touch_stamp(cd)
    log(
        f"repo={cwd} artifacts={n_art} worktrees_removed={n_wt} "
        f"freed={freed // (1024*1024)}MB elapsed={time.time() - _STARTED_AT:.1f}s"
    )
    sys.exit(0)


def cmd_report(payload: dict) -> None:
    cwd = payload.get("cwd") or os.getcwd()
    cd = common_dir(cwd)
    if cd is None:
        print("not a git repo:", cwd)
        sys.exit(0)
    artifacts, removable = plan(cwd, cd)

    print(f"repo: {cwd}")
    print(
        f"gates: artifacts idle>{ARTIFACT_IDLE_HOURS}h, "
        f"remove merged+clean idle>{REMOVE_IDLE_DAYS}d, throttle {THROTTLE_HOURS}h"
    )
    print(f"linked worktrees: {len(worktrees(cwd))}")
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
    else:
        cmd_reap(read_payload())


if __name__ == "__main__":
    main()

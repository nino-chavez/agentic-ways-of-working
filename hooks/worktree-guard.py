#!/usr/bin/env python3
"""Worktree isolation guard — enforce "multi-session work goes in worktrees".

The worktree-isolation rule (principles/working-style.md) was advisory-only: it
lived as text an agent reads and chooses to apply, so adherence was
non-deterministic and drifted. This hook makes it mechanical. When more than one
live Claude session has the same repo open, contended git operations (commit,
branch create/switch) are *blocked* in the shared main checkout — forcing the
work into a worktree first.

Three subcommands, dispatched by argv[1], wired to three hook events:
  register   — SessionStart: write/refresh this session's heartbeat lock.
  check      — PreToolUse (matcher "Bash"): refresh own lock, prune stale ones,
               and DENY the call if it runs a contended git op against a shared
               main checkout while another session is live there.
  unregister — SessionEnd: remove this session's lock.

Liveness model: each session owns one lock file under
`<git-common-dir>/.claude-sessions/<session_id>.json`. The git *common* dir is
shared by the main checkout and every linked worktree, so locks are visible to
all sessions of a repo regardless of which checkout they sit in. The file mtime
is a heartbeat, refreshed on every Bash tool call; a lock older than
STALE_SECONDS is treated as a dead session and pruned. SessionEnd removes it.

Effective-repo resolution: the guard does not blindly use the session cwd. It
walks the shell command, tracking `cd <dir>` and honoring `git -C <dir>`, so a
command like `cd ~/other-repo && git commit` is judged against ~/other-repo —
not the session's repo. A commit in an uncontended repo is never blocked just
because the *session* happens to live in a busy one.

Fail-open by design: anything uncertain (not a git repo, git error, unparseable
command, malformed payload) results in *allow*. A guard that blocks legitimate
solo work gets ripped out; this one only blocks when it is certain the
preconditions hold. Per-repo escape hatch: a `.guard-off` file in the lock dir.

Pure stdlib, no external deps, degrades quietly outside git repos. Wire via
settings.json (the repo's install.sh does this):
  SessionStart -> python3 <hooks>/worktree-guard.py register
  PreToolUse (matcher "Bash") -> python3 <hooks>/worktree-guard.py check
  SessionEnd -> python3 <hooks>/worktree-guard.py unregister
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

STALE_SECONDS = 15 * 60  # a lock older than this = dead session, prune it
LOCK_DIRNAME = ".claude-sessions"
OVERRIDE_FILENAME = ".guard-off"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}  # file-mutating tools guarded alongside Bash

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SEPARATORS = {"&&", "||", ";", "|", "|&", "&"}
_GLOBAL_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


# --- payload + git helpers ---------------------------------------------------

def read_payload() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def git(args: list[str], cwd: str) -> str | None:
    """Run a git command, return stripped stdout or None on any failure."""
    try:
        r = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def repo_context(cwd: str) -> dict | None:
    """Resolve the repo's shared git dir + whether cwd is a linked worktree.

    Returns None if cwd is not inside a git work tree (=> caller should allow).
    """
    if not os.path.isdir(cwd):
        return None
    common = git(["rev-parse", "--git-common-dir"], cwd)
    if common is None:
        return None
    git_dir = git(["rev-parse", "--git-dir"], cwd)
    if git_dir is None:
        return None
    common_abs = os.path.abspath(os.path.join(cwd, common))
    git_dir_abs = os.path.abspath(os.path.join(cwd, git_dir))
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd) or "(detached)"
    return {
        "common_dir": common_abs,
        # A linked worktree's --git-dir is <common>/worktrees/<name>; the main
        # checkout's --git-dir IS the common dir. Differ => isolated worktree.
        "is_worktree": git_dir_abs != common_abs,
        "branch": branch,
    }


def lock_dir(common_dir: str) -> Path:
    return Path(common_dir) / LOCK_DIRNAME


# --- lock lifecycle ----------------------------------------------------------

def write_lock(ld: Path, session_id: str, cwd: str, ctx: dict) -> None:
    ld.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "cwd": cwd,
        "branch": ctx["branch"],
        "is_worktree": ctx["is_worktree"],
        "ts": int(time.time()),
    }
    tmp = ld / f".{session_id}.tmp"
    final = ld / f"{session_id}.json"
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(final)  # atomic; also refreshes mtime (the heartbeat)
    except Exception:
        pass


def prune_stale(ld: Path, keep_session: str) -> None:
    now = time.time()
    try:
        entries = list(ld.glob("*.json"))
    except Exception:
        return
    for f in entries:
        if f.stem == keep_session:
            continue
        try:
            if now - f.stat().st_mtime > STALE_SECONDS:
                f.unlink()
        except Exception:
            pass


def live_others(ld: Path, my_session: str) -> list[dict]:
    """Other sessions whose lock is fresh (mtime within STALE_SECONDS)."""
    now = time.time()
    out: list[dict] = []
    try:
        entries = list(ld.glob("*.json"))
    except Exception:
        return out
    for f in entries:
        if f.stem == my_session:
            continue
        try:
            if now - f.stat().st_mtime > STALE_SECONDS:
                continue
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def remove_lock(ld: Path, session_id: str) -> None:
    try:
        (ld / f"{session_id}.json").unlink()
    except Exception:
        pass


# --- shell command analysis: effective repo per git invocation ---------------

def _resolve_dir(base: str, target: str) -> str:
    t = os.path.expanduser(target)
    if not os.path.isabs(t):
        t = os.path.join(base, t)
    return os.path.normpath(t)


def _statements(command: str) -> list[list[str]] | None:
    """Tokenize and split into statements on shell separators.

    Returns None on a tokenization failure (caller fails open).
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    stmts: list[list[str]] = []
    cur: list[str] = []
    for t in tokens:
        if t in _SEPARATORS:
            if cur:
                stmts.append(cur)
                cur = []
        else:
            cur.append(t)
    if cur:
        stmts.append(cur)
    return stmts


def _git_targets(command: str, base_dir: str) -> list[tuple[str, list[str]]] | None:
    """For each `git ...` invocation, the (effective_dir, git_args) it runs with.

    Tracks `cd <dir>` across `&&`/`;`-separated statements and honors `git -C
    <dir>`, so the directory each git call actually operates in is resolved
    rather than assumed to be the session cwd. Returns None on parse failure.
    """
    stmts = _statements(command)
    if stmts is None:
        return None
    targets: list[tuple[str, list[str]]] = []
    cur = base_dir
    for st in stmts:
        # Skip leading env-assignment prefixes (FOO=bar git ...).
        k = 0
        while k < len(st) and _ENV_ASSIGN.match(st[k]):
            k += 1
        if k >= len(st):
            continue
        head = st[k]
        rest = st[k + 1:]
        if head == "cd":
            target = next((a for a in rest if not a.startswith("-")), None)
            if target is not None:
                cur = _resolve_dir(cur, target)
        elif head == "git" or head.endswith("/git"):
            eff = cur
            j = 0
            while j < len(rest):  # honor a -C <path> override for this call
                if rest[j] == "-C" and j + 1 < len(rest):
                    eff = _resolve_dir(cur, rest[j + 1])
                    j += 2
                    continue
                j += 1
            targets.append((eff, rest))
    return targets


def _subcommand(args: list[str]) -> tuple[str | None, list[str]]:
    """Strip git global options, return (subcommand, remaining_args)."""
    i = 0
    n = len(args)
    while i < n:
        a = args[i]
        if a in _GLOBAL_OPTS_WITH_VALUE:
            i += 2  # skip the option and its value
            continue
        if a.startswith("-"):
            i += 1  # valueless global flag (e.g. --no-pager, --bare)
            continue
        return a, args[i + 1:]
    return None, []


def _is_contended_args(args: list[str]) -> bool:
    """True if this single git invocation stomps a shared checkout.

    Scoped to the two documented stomp mechanisms — "commits land on the wrong
    branch" and "sessions switch each other's branches": commit, and branch
    create/switch. Plain `git checkout <file>` and read-only ops are NOT
    contended. Conservative on purpose: a missed branch switch is tolerable; a
    false positive erodes trust in the guard.
    """
    sub, rest = _subcommand(args)
    if sub is None:
        return False
    if sub == "commit":
        return True
    if sub == "checkout" and any(o in ("-b", "-B") for o in rest):
        return True
    if sub == "switch":
        if any(o in ("-c", "-C") for o in rest):
            return True
        return any(not o.startswith("-") for o in rest)  # `git switch <branch>`
    if sub == "branch":
        mutating = {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move",
                    "--copy", "--list", "--edit-description", "--set-upstream-to"}
        if not any(o in mutating for o in rest) and any(not o.startswith("-") for o in rest):
            return True
    return False


def is_contended(command: str) -> bool:
    """Convenience wrapper: does the command contain any contended git op?"""
    targets = _git_targets(command, "/")
    return bool(targets) and any(_is_contended_args(args) for _, args in targets)


# --- edit-tool target resolution ---------------------------------------------

def _edit_target_path(payload: dict) -> str | None:
    """The file an Edit/Write/MultiEdit call targets (its `file_path`), or None."""
    p = (payload.get("tool_input") or {}).get("file_path")
    return p if isinstance(p, str) and p else None


# --- block message -----------------------------------------------------------

def suggest_worktree(repo_dir: str) -> str:
    # Paste-ready: concrete generated branch + absolute path, no placeholders.
    # Agents fumbled 2-4 turns filling in <branch> templates; a runnable
    # command removes that. Rename the branch later if a semantic name fits.
    repo = os.path.basename(repo_dir.rstrip("/")) or "repo"
    branch = f"wt-{time.strftime('%m%d-%H%M%S')}"
    wt_path = os.path.normpath(os.path.join(repo_dir, f"../{repo}-{branch}"))
    return f"git -C {repo_dir} worktree add {wt_path} -b {branch} && cd {wt_path}"


def deny_reason(others: list[dict], repo_dir: str, ld: Path) -> str:
    lines = [
        "Worktree isolation guard: BLOCKED.",
        "",
        f"You are about to run a contended git op in the SHARED main checkout ({repo_dir}),",
        f"but {len(others)} other live Claude session(s) have this repo open:",
    ]
    for o in others:
        wt = "worktree" if o.get("is_worktree") else "MAIN checkout"
        lines.append(f"  - session {str(o.get('session_id', '?'))[:8]} on '{o.get('branch', '?')}' ({wt}) at {o.get('cwd', '?')}")
    lines += [
        "",
        "Sessions sharing a working directory switch each other's branches and land",
        "commits on the wrong branch. Move this work into its own worktree first:",
        "",
        f"  {suggest_worktree(repo_dir)}",
        "",
        "Then re-run the git command from inside the worktree (commits there are allowed).",
        f"Override for this repo (use sparingly): touch {ld / OVERRIDE_FILENAME}",
    ]
    return "\n".join(lines)


def deny_reason_edit(others: list[dict], repo_dir: str, ld: Path, path: str) -> str:
    lines = [
        "Worktree isolation guard: BLOCKED (file edit).",
        "",
        f"You are about to edit a file in the SHARED main checkout ({repo_dir}),",
        f"but {len(others)} other live Claude session(s) are working in this same main checkout:",
    ]
    for o in others:
        lines.append(f"  - session {str(o.get('session_id', '?'))[:8]} on '{o.get('branch', '?')}' at {o.get('cwd', '?')}")
    lines += [
        "",
        f"  target: {path}",
        "",
        "Two sessions editing the same checkout overwrite each other's work and land",
        "commits on the wrong branch. Move this work into its own worktree first:",
        "",
        f"  {suggest_worktree(repo_dir)}",
        "",
        "Then re-run from inside the worktree (edits there are allowed).",
        f"Override for this repo (use sparingly): touch {ld / OVERRIDE_FILENAME}",
    ]
    return "\n".join(lines)


def register_warning(others: list[dict], repo_dir: str) -> str:
    lines = [
        "WORKTREE ISOLATION WARNING.",
        "",
        f"You started in the SHARED main checkout ({repo_dir}), but "
        f"{len(others)} other live Claude session(s) are ALREADY working here:",
    ]
    for o in others:
        lines.append(f"  - session {str(o.get('session_id', '?'))[:8]} on '{o.get('branch', '?')}' at {o.get('cwd', '?')}")
    lines += [
        "",
        "Per working-style.md, parallel sessions MUST isolate in worktrees — sharing this",
        "checkout will overwrite each other's edits and land commits on the wrong branch.",
        "Before doing any work, create your own worktree:",
        "",
        f"  {suggest_worktree(repo_dir)}",
        "",
        "Edits and commits in the main checkout will be BLOCKED while another session is live here.",
    ]
    return "\n".join(lines)


def allow() -> None:
    sys.exit(0)


def warn_session_start(context: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))
    sys.exit(0)


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


# --- subcommands -------------------------------------------------------------

def cmd_register(payload: dict) -> None:
    cwd = payload.get("cwd") or os.getcwd()
    session_id = payload.get("session_id") or "unknown"
    ctx = repo_context(cwd)
    if ctx is None:
        allow()
    ld = lock_dir(ctx["common_dir"])
    write_lock(ld, session_id, cwd, ctx)
    prune_stale(ld, session_id)
    # If this session is starting in the SHARED main checkout while other live
    # sessions already occupy it, warn loudly at start (inject context) so the
    # agent creates a worktree BEFORE doing work — not after colliding.
    if not ctx["is_worktree"] and not (ld / OVERRIDE_FILENAME).exists():
        main_others = [o for o in live_others(ld, session_id) if not o.get("is_worktree")]
        if main_others:
            warn_session_start(register_warning(main_others, cwd))
    allow()


def cmd_unregister(payload: dict) -> None:
    cwd = payload.get("cwd") or os.getcwd()
    ctx = repo_context(cwd)
    if ctx is None:
        allow()
    remove_lock(lock_dir(ctx["common_dir"]), payload.get("session_id") or "unknown")
    allow()


def cmd_check(payload: dict) -> None:
    tool = payload.get("tool_name")
    if tool not in EDIT_TOOLS and tool != "Bash":
        allow()
    cwd = payload.get("cwd") or os.getcwd()
    session_id = payload.get("session_id") or "unknown"

    # Heartbeat: refresh this session's lock in its OWN repo on every guarded
    # call, so SessionStart is not required for correctness (covers hooks added
    # mid-session). Registration is keyed to the session's home repo.
    sctx = repo_context(cwd)
    if sctx is not None:
        sld = lock_dir(sctx["common_dir"])
        write_lock(sld, session_id, cwd, sctx)
        prune_stale(sld, session_id)

    # Write/Edit/MultiEdit: block a file edit to a SHARED main checkout when
    # another live session occupies that same main checkout. Resolve the repo
    # from the target file's path, not the session cwd. Solo sessions and edits
    # inside an isolated worktree are always allowed.
    if tool in EDIT_TOOLS:
        path = _edit_target_path(payload)
        if not path:
            allow()  # no resolvable path => fail open
        target_dir = os.path.dirname(os.path.abspath(path)) or cwd
        tctx = repo_context(target_dir)
        if tctx is None or tctx["is_worktree"]:
            allow()  # not a repo, or already isolated => allow
        tld = lock_dir(tctx["common_dir"])
        if (tld / OVERRIDE_FILENAME).exists():
            allow()  # per-repo escape hatch
        main_others = [o for o in live_others(tld, session_id) if not o.get("is_worktree")]
        if main_others:
            deny(deny_reason_edit(main_others, target_dir, tld, path))
        allow()

    command = (payload.get("tool_input") or {}).get("command", "")

    # Resolve the effective repo of each git op (honoring cd / git -C) and block
    # only the first contended op against a shared, contended main checkout.
    targets = _git_targets(command, cwd)
    if not targets:
        allow()  # no git op, or parse failure => fail open
    for eff_dir, args in targets:
        if not _is_contended_args(args):
            continue
        tctx = repo_context(eff_dir)
        if tctx is None or tctx["is_worktree"]:
            continue  # not a repo, or already isolated in a worktree => allow
        tld = lock_dir(tctx["common_dir"])
        if (tld / OVERRIDE_FILENAME).exists():
            continue  # per-repo escape hatch
        if live_others(tld, session_id):
            deny(deny_reason(live_others(tld, session_id), eff_dir, tld))
    allow()


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    payload = read_payload()
    if mode == "register":
        cmd_register(payload)
    elif mode == "unregister":
        cmd_unregister(payload)
    else:
        cmd_check(payload)


if __name__ == "__main__":
    main()

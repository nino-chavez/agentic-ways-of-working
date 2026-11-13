#!/usr/bin/env python3
"""Worktree isolation guard — enforce "multi-session work goes in worktrees".

The worktree-isolation rule (principles/working-style.md) was advisory-only: it
lived as text an agent reads and chooses to apply, so adherence was
non-deterministic and drifted. This hook makes it mechanical. When another live
Claude session occupies the same shared main checkout, git operations that
rewrite HEAD, the index or the working tree are *blocked* there — forcing the
work into a worktree first. Separately, folding in a branch another live session
holds escalates to the user, wherever either session sits.

Three subcommands, dispatched by argv[1], wired to three hook events:
  register   — SessionStart: write/refresh this session's heartbeat lock.
  check      — PreToolUse (matcher "Bash"): refresh own lock, prune stale ones,
               then apply two independent checks (see "Contention model").
  unregister — SessionEnd: remove this session's lock.

Liveness model: each session owns one lock file under
`<git-common-dir>/.claude-sessions/<session_id>.json`. The git *common* dir is
shared by the main checkout and every linked worktree, so locks are visible to
all sessions of a repo regardless of which checkout they sit in. The file mtime
is a heartbeat, refreshed on every Bash tool call; a lock older than
STALE_SECONDS is treated as a dead session and pruned. SessionEnd removes it.
A session that exports CLAUDE_GUARD_ROLE=readonly (the post-commit review hook
does) still registers, but is never counted as contention — it cannot commit,
switch branches or edit, so it cannot stomp anyone.

Contention model — two hazards, two checks:

  1. Occupancy. Two sessions in the SAME shared main checkout stomp each other's
     HEAD, index and working tree. A contended git op there is DENIED. A session
     isolated in a linked worktree has its own HEAD/index/working tree, so it
     neither stomps nor blocks the main checkout — it is not counted. (Until
     2026-08-27 the Bash path counted worktree sessions too, denying a solo
     commit in main while the other session sat safely isolated; the Edit path
     never did. The two paths now agree.)

  2. Branch ownership. git refuses to CHECK OUT a branch another worktree holds,
     but merges or rebases it without complaint — so occupancy alone misses the
     case where one session integrates a branch another is still committing to.
     A merge/rebase whose operand matches a live session's branch escalates to
     the user (permissionDecision "ask"), in the main checkout or a worktree.
     Motivating incident (operator, 2026-08-27): session B was correctly pushed
     into a worktree, session A then merged B's branch in main and left
     MERGE_HEAD plus five staged files behind. Nothing blocked it, because
     `merge` was not a contended op and B was no longer in main.

One check ignores contention. `git submodule deinit` in a linked worktree
deletes the submodule.* entries from the SHARED .git/config, so the main
checkout and every other worktree lose their submodule registration. That
happens even when this session is the only one in the repo, so the check runs
before the contention gate. It happened three times in one dotfiles repo
(2026-10-23 and twice on 2026-10-30), each time to remove a worktree that `git
worktree remove` refused because it held initialized submodules.

Effective-repo resolution: the guard does not blindly use the session cwd. It
walks the shell command, tracking `cd <dir>` and honoring `git -C <dir>`, so a
command like `cd ~/other-repo && git commit` is judged against ~/other-repo —
not the session's repo. A commit in an uncontended repo is never blocked just
because the *session* happens to live in a busy one. Statements split on
newlines and on separators glued to a word (`cd x;git`), heredoc bodies are
data and are dropped, a `$(...)` or backtick substitution is checked as a
command of its own, and a statement may open with `(`, `{` or a reserved word
such as `do` or `then`. Until 2026-11-13 any git call after a newline was
invisible to every check.

Fail-open by design: anything uncertain (not a git repo, git error, unparseable
command, malformed payload) results in *allow*. A guard that blocks legitimate
solo work gets ripped out; this one only blocks when it is certain the
preconditions hold. Per-repo escape hatch: a `.guard-off` file in the lock dir.

One exception fails closed. A `-C` or `cd` target the guard cannot evaluate
(`$W`, `${W}`, `$(...)`, backticks, an unknown `~user`) leaves the target
unknown. For `submodule deinit` that is a deny: the damage needs no second
session, the op is rare, and a false positive costs one re-run with a literal
path. On 2026-11-13 `W=<worktree>; git -C $W submodule deinit --all` went
through and de-registered every dotfiles submodule. The occupancy and branch
checks stay fail-open on an unknown target. Both need a second live session in
that same repo, which cannot be looked up without the path, and denying every
`git -C "$X" commit` would block routine solo work.

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
ROLE_ENV = "CLAUDE_GUARD_ROLE"  # a session may declare itself non-contending
READONLY_ROLE = "readonly"

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Characters the tokenizer returns as their own tokens. A run of them (`&&`,
# `;\n`, `\n\n`) comes back as one token, and any such token ends a statement.
_SEPARATOR_CHARS = ";&|\n"
# Words that open a statement without being its command: `do git ...` in a loop,
# `then git ...`, `( cd x && git ...)`, `{ git ...; }`.
_STATEMENT_PREFIXES = {"do", "then", "else", "elif", "if", "while", "until", "!", "{", "(", "time"}
# `<<EOF`, `<<-EOF`, `<< 'EOF'`, `<<"EOF"`, `<<\EOF`; not the here-string `<<<`.
_HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(['\"]?)\\?([A-Za-z_][A-Za-z0-9_]*)\2")
# A placeholder left by _mask_substitutions: `$(subst3)` or `` `subst3` ``.
_SUBST_REF = re.compile(r"\$\(subst(\d+)\)|`subst(\d+)`")
_MAX_SUBST_DEPTH = 16
_GLOBAL_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}

# Subcommands that rewrite HEAD, the index, or the working tree unconditionally.
# `pull` is fetch+merge; `am` applies a mailbox onto the current branch.
_ALWAYS_CONTENDED = {"commit", "merge", "rebase", "cherry-pick", "revert", "am", "pull"}
# `reset <path>` and `reset --soft` do not touch the working tree; these do.
_RESET_STOMPING = {"--hard", "--merge", "--keep"}
# `stash push/list/show` are safe; these write the working tree.
_STASH_STOMPING = {"pop", "apply"}
# Ops that fold a named ref into the current checkout — subject to the branch-
# ownership check on top of the occupancy check.
_INTEGRATION_SUBS = {"merge", "rebase"}
# Options of those ops that consume the following token, so it is not a ref.
# -S/--gpg-sign deliberately excluded: its key id attaches (-Skeyid), so
# treating it as value-taking would swallow the ref in `git merge -S feat/x`.
_INTEGRATION_OPTS_WITH_VALUE = {
    "-m", "-X", "-s", "--strategy", "--strategy-option", "--onto",
    "--into-name", "-x", "--exec", "-F", "--file",
}
# Marker -> (op name, `git ...` abort command) for a half-finished operation.
_IN_PROGRESS_MARKERS = [
    ("MERGE_HEAD", "merge", "merge --abort"),
    ("CHERRY_PICK_HEAD", "cherry-pick", "cherry-pick --abort"),
    ("REVERT_HEAD", "revert", "revert --abort"),
    ("REBASE_HEAD", "rebase", "rebase --abort"),
]


# --- payload + git helpers ---------------------------------------------------

def read_payload() -> dict:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return {}
    # Valid JSON that isn't an object (null, [], "str", 123) parses fine and
    # then blows up on .get() — fail open on shape too, not just on syntax.
    return payload if isinstance(payload, dict) else {}


def _cwd(payload: dict) -> str:
    """The payload's cwd, or the process cwd when it is missing or not a str.

    A truthy list or dict slips past `or os.getcwd()` and makes os.path.isdir
    raise TypeError, which it does not swallow.
    """
    cwd = payload.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else os.getcwd()


def _tool_input(payload: dict) -> dict:
    """The payload's tool_input, or {} when it is missing or not an object."""
    ti = payload.get("tool_input")
    return ti if isinstance(ti, dict) else {}  # `or {}` lets a truthy non-dict through


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
        # Per-checkout git dir. MERGE_HEAD and friends live HERE, not in the
        # common dir, so an unfinished merge is detected per worktree.
        "git_dir": git_dir_abs,
        # A linked worktree's --git-dir is <common>/worktrees/<name>; the main
        # checkout's --git-dir IS the common dir. Differ => isolated worktree.
        "is_worktree": git_dir_abs != common_abs,
        "branch": branch,
    }


def lock_dir(common_dir: str) -> Path:
    return Path(common_dir) / LOCK_DIRNAME


# --- lock lifecycle ----------------------------------------------------------

def session_role() -> str:
    """This session's declared role, normalized. "" when undeclared.

    A session that cannot mutate the checkout cannot stomp another one, so it
    must not be counted as contention. The post-commit `claude-review` hook is
    the motivating case: it launches a headless session whose allowlist is
    `git show/diff/log/blame`, Read, Glob, Grep — no commit, no branch switch,
    no edit. Left undeclared, its lock made *every* commit block the next one in
    the same repo, since the review of commit N is still live when commit N+1
    runs. Declared readonly, it registers for visibility but never contends.

    Opt-in by design: an undeclared session is assumed to contend, so this can
    only ever loosen the guard for a session that has explicitly said it is safe
    to ignore.
    """
    role = os.environ.get(ROLE_ENV, "").strip().lower()
    return role if role == READONLY_ROLE else ""


def write_lock(ld: Path, session_id: str, cwd: str, ctx: dict) -> None:
    ld.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "cwd": cwd,
        "branch": ctx["branch"],
        "is_worktree": ctx["is_worktree"],
        "role": session_role(),
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
    """Other sessions whose lock is fresh (mtime within STALE_SECONDS).

    Contention only. Sessions that declared themselves readonly are skipped —
    they cannot stomp a checkout, so counting them would block legitimate solo
    work, which is the failure mode this guard is explicitly built to avoid.
    Locks written before the role field existed have no `role` and still count.
    """
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
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("role") == READONLY_ROLE:
                continue
            out.append(data)
        except Exception:
            continue
    return out


def remove_lock(ld: Path, session_id: str) -> None:
    try:
        (ld / f"{session_id}.json").unlink()
    except Exception:
        pass


# --- shell command analysis: effective repo per git invocation ---------------

def _unresolvable(target: str) -> bool:
    """True when the shell would expand `target` in a way the guard cannot.

    `$W`, `${W}`, `$(...)` and backticks need the shell's state; a `~user`
    that does not exist here stays literal after expanduser.
    """
    return "$" in target or "`" in target or os.path.expanduser(target).startswith("~")


def _resolve_dir(base: str | None, target: str) -> str | None:
    """`target` as an absolute path, or None when it cannot be known.

    A relative target under an unknown base is unknown too; an absolute one
    recovers.
    """
    if _unresolvable(target):
        return None
    t = os.path.expanduser(target)
    if not os.path.isabs(t):
        if base is None:
            return None
        t = os.path.join(base, t)
    return os.path.normpath(t)


def _strip_heredocs(command: str) -> str:
    """Drop heredoc bodies: they are data, not statements.

    Splitting on newlines would otherwise read a `git commit` line inside
    `cat > notes <<'EOF'` as a command. An unbalanced quote in a body (`it's`)
    also made shlex raise, which failed the whole command open.
    """
    lines = command.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        out.append(lines[i])
        body_start = i + 1
        # Bodies follow the opening line in marker order. A marker whose
        # delimiter never comes is not a heredoc (`"a <<EOF"` in quotes,
        # `$((a<<b))`) and swallows nothing, so the lines after it are still
        # judged, and a real heredoc on the same line is still stripped.
        for m in _HEREDOC.finditer(lines[i]):
            delim, dash = m.group(3), m.group(1) == "-"
            k = body_start
            while k < n and (lines[k].lstrip("\t") if dash else lines[k]) != delim:
                k += 1
            if k < n:
                body_start = k + 1
        i = body_start
    return "\n".join(out)


def _mask_substitutions(command: str) -> tuple[str, list[str]]:
    """Replace each outermost `...` and $(...) with an indexed opaque word.

    Returns (masked command, bodies). Their contents run in a subshell: a `cd`
    there never moves the outer shell, and a `|` or `;` inside must not split
    the outer statement (a backticked `-C` target holding a pipe made a deinit
    look like no deinit at all). The placeholder keeps its `$` or backtick, so a
    target built from one stays unresolvable; `_git_targets` analyzes each body
    as a command of its own. An unbalanced $( or backtick is left as written,
    and a backtick pair never spans lines, so a stray one cannot swallow
    statements.
    """
    out: list[str] = []
    bodies: list[str] = []
    i, n = 0, len(command)
    while i < n:
        if command.startswith("$(", i):
            depth, j = 0, i + 1
            while j < n:
                if command[j] == "(":
                    depth += 1
                elif command[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j < n:
                bodies.append(command[i + 2:j])
                out.append(f"$(subst{len(bodies) - 1})")
                i = j + 1
                continue
        elif command[i] == "`":
            j = command.find("`", i + 1)
            if j != -1 and "\n" not in command[i + 1:j]:
                bodies.append(command[i + 1:j])
                out.append(f"`subst{len(bodies) - 1}`")
                i = j + 1
                continue
        out.append(command[i])
        i += 1
    return "".join(out), bodies


def _subst_refs(token: str, bodies: list[str]) -> list[re.Match]:
    """Placeholders in `token` that index a real body.

    A literal `$(subst9` the user typed is left unmasked when unbalanced, so an
    index is checked rather than trusted.
    """
    return [m for m in _SUBST_REF.finditer(token) if _subst_index(m) < len(bodies)]


def _subst_index(m: re.Match) -> int:
    return int(m.group(1) if m.group(1) is not None else m.group(2))


def _unmask(token: str, bodies: list[str]) -> str:
    """A token with its substitution placeholders written back, for messages."""
    for m in reversed(_subst_refs(token, bodies)):
        body = bodies[_subst_index(m)]
        text = f"$({body})" if m.group(1) is not None else f"`{body}`"
        token = token[:m.start()] + text + token[m.end():]
    return token


def _statements(command: str) -> tuple[list[list[str]], list[str]] | None:
    """Tokenize and split into statements on shell separators and newlines.

    Returns (statements, substitution bodies), or None on a tokenization
    failure (caller fails open).
    """
    # Heredocs first: a `$(cat <<'EOF' ... EOF)` commit message body may hold
    # unbalanced parens that would throw off the substitution scan.
    command = _strip_heredocs(command).replace("\\\n", "")  # join continuations
    command, bodies = _mask_substitutions(command)
    lex = shlex.shlex(command, posix=True, punctuation_chars=_SEPARATOR_CHARS)
    lex.whitespace_split = True
    lex.whitespace = " \t\r"  # newline is a separator, not whitespace
    lex.commenters = ""  # shlex.split's default; `#` mid-word is not a comment
    try:
        tokens = list(lex)
    except ValueError:
        return None
    stmts: list[list[str]] = []
    cur: list[str] = []
    for t in tokens:
        if t and all(c in _SEPARATOR_CHARS for c in t):
            if cur:
                stmts.append(cur)
                cur = []
        else:
            cur.append(t)
    if cur:
        stmts.append(cur)
    return stmts, bodies


def _git_targets(
    command: str, base_dir: str | None, base_raw: str | None = None, depth: int = 0
) -> list[tuple[str | None, list[str], str | None]] | None:
    """For each `git ...` invocation: (effective_dir, git_args, unresolved_token).

    Tracks `cd <dir>` across statements and honors `git -C <dir>`, so the
    directory each git call actually operates in is resolved rather than
    assumed to be the session cwd. effective_dir is None when a shell expansion
    hides it; unresolved_token is then the `-C` or `cd` value responsible.
    A command substitution is analyzed as its own command, starting in the
    directory the outer shell is in when it runs. Returns None on parse failure.
    """
    parsed = _statements(command)
    if parsed is None:
        return None
    stmts, bodies = parsed
    targets: list[tuple[str | None, list[str], str | None]] = []
    cur: str | None = base_dir
    cur_raw: str | None = base_raw  # the cd target that made `cur` unknown
    for st in stmts:
        # Substitutions run before the statement that holds them. Past
        # _MAX_SUBST_DEPTH levels of nesting a body stays opaque: unbounded
        # recursion would hit Python's limit and crash the hook, which allows.
        for tok in st if depth < _MAX_SUBST_DEPTH else ():
            for m in _subst_refs(tok, bodies):
                inner = _git_targets(bodies[_subst_index(m)], cur, cur_raw, depth + 1)
                targets += inner or []
        # Skip env-assignment prefixes (FOO=bar git ...) and statement openers.
        k = 0
        while k < len(st) and (_ENV_ASSIGN.match(st[k]) or st[k] in _STATEMENT_PREFIXES):
            k += 1
        if k >= len(st):
            continue
        head = st[k].lstrip("(")  # `(cd x && git ...)` tokenizes as "(cd"
        rest = st[k + 1:]
        if head == "cd":
            target = next((a for a in rest if not a.startswith("-")), None)
            if target is not None:
                cur = _resolve_dir(cur, target)
                if cur is None:
                    cur_raw = _unmask(target, bodies) if _unresolvable(target) else cur_raw
                else:
                    cur_raw = None
        elif head == "git" or head.endswith("/git"):
            eff, raw = cur, cur_raw
            j = 0
            while j < len(rest):  # global options only: `commit -C <rev>` is not a dir
                a = rest[j]
                if a in _GLOBAL_OPTS_WITH_VALUE:
                    if a == "-C" and j + 1 < len(rest):  # each -C is relative to the last
                        value = rest[j + 1]
                        eff = _resolve_dir(eff, value)
                        if eff is None:
                            raw = _unmask(value, bodies) if _unresolvable(value) else raw
                        else:
                            raw = None
                    j += 2
                    continue
                if not a.startswith("-"):
                    break  # the subcommand
                j += 1
            targets.append((eff, rest, raw))
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


def _is_contended_args(args: list[str], cwd: str | None = None) -> bool:
    """True if this single git invocation stomps a shared checkout.

    Covers the ops that rewrite HEAD, the index, or the working tree. The
    original set was commit + branch create/switch only, which left `merge`,
    `rebase`, `cherry-pick`, `revert`, `am`, `pull`, `reset --hard` and
    `stash pop` allowed — ten of fourteen mutating ops, measured 2026-08-27.
    A merge through that gap is what stranded MERGE_HEAD in operator's main
    checkout.

    Still conservative where a token is genuinely ambiguous: read-only ops,
    `reset --soft`, `stash push` and `git checkout <file>` are NOT contended. A
    missed op is tolerable; a false positive erodes trust in the guard.
    """
    sub, rest = _subcommand(args)
    if sub is None:
        return False
    if sub in _ALWAYS_CONTENDED:
        return True
    if sub == "reset":
        return any(o in _RESET_STOMPING for o in rest)
    if sub == "stash":
        return any(o in _STASH_STOMPING for o in rest)
    if sub == "checkout":
        if any(o in ("-b", "-B") for o in rest):
            return True
        if "--" in rest:
            return False  # explicit path mode: `git checkout -- <path>`
        operands = [o for o in rest if not o.startswith("-")]
        if not operands:
            return False
        # `checkout <branch>` switches the shared checkout (the documented
        # stomp); `checkout <file>` only restores a file. The token alone does
        # not say which, so an operand that exists as a path is read as file
        # mode and anything else as a ref. Errs toward contended when cwd is
        # unknown, which only matters once another session shares the checkout.
        base = cwd or "."
        return not any(os.path.exists(os.path.join(base, o)) for o in operands)
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


def _is_submodule_deinit(args: list[str]) -> bool:
    """True for `git submodule [--quiet] deinit ...`."""
    sub, rest = _subcommand(args)
    if sub != "submodule":
        return False
    return next((a for a in rest if not a.startswith("-")), None) == "deinit"


def _integration_operands(args: list[str]) -> list[str]:
    """Non-flag operands of a merge/rebase — the refs it folds into this checkout.

    Positional meaning differs across `merge <ref>`, `rebase <upstream>` and
    `rebase --onto <base> <upstream>`, so rather than parse positions this
    collects every operand and lets the caller test each against live sessions'
    branches. Over-collecting is safe: the branch check only ASKS, never denies.
    """
    sub, rest = _subcommand(args)
    if sub not in _INTEGRATION_SUBS:
        return []
    out: list[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--":
            break
        if a in _INTEGRATION_OPTS_WITH_VALUE:
            i += 2  # skip the option and the token it consumes
            continue
        if a.startswith("-"):
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def in_progress_op(git_dir: str) -> tuple[str, str] | None:
    """(op name, `git ...` abort command) if this checkout is mid-op, else None."""
    if not git_dir:
        return None  # else the markers would resolve relative to cwd
    try:
        for marker, name, abort in _IN_PROGRESS_MARKERS:
            if os.path.exists(os.path.join(git_dir, marker)):
                return name, abort
        # `rebase -i` / `rebase -m` leave a state DIRECTORY, not REBASE_HEAD.
        for d in ("rebase-merge", "rebase-apply"):
            if os.path.isdir(os.path.join(git_dir, d)):
                return "rebase", "rebase --abort"
    except Exception:
        return None
    return None


def is_contended(command: str) -> bool:
    """Convenience wrapper: does the command contain any contended git op?"""
    targets = _git_targets(command, "/")
    return bool(targets) and any(_is_contended_args(args, eff) for eff, args, _ in targets)


# --- edit-tool target resolution ---------------------------------------------

def _edit_target_path(payload: dict) -> str | None:
    """The file an Edit/Write/MultiEdit call targets (its `file_path`), or None."""
    p = _tool_input(payload).get("file_path")
    return p if isinstance(p, str) and p else None


# --- block message -----------------------------------------------------------

def suggest_worktree(repo_dir: str) -> str:
    # Paste-ready: concrete generated branch + absolute path, no placeholders.
    # Agents fumbled 2-4 turns filling in <branch> templates; a runnable
    # command removes that. Rename the branch later if a semantic name fits.
    #
    # Worktrees live INSIDE the repo at .worktrees/<branch> (2026-07-22): the
    # earlier ../<repo>-wt-* sibling convention scattered per-session folders
    # across the parent directory (film-room accumulated five). `.worktrees/`
    # is ignored machine-wide via the dotfiles-managed ~/.config/git/ignore
    # (git's default excludes path), so the main checkout's status stays clean.
    branch = f"wt-{time.strftime('%m%d-%H%M%S')}"
    wt_path = os.path.join(repo_dir, ".worktrees", branch)
    return f"git -C {repo_dir} worktree add {wt_path} -b {branch} && cd {wt_path}"


def unwind_lines(ctx: dict, repo_dir: str) -> list[str]:
    """Extra guidance when this checkout is ALREADY mid-merge/rebase.

    The guard fires PRE-tool, so on the call that first blocks a merge there is
    no MERGE_HEAD yet and this is empty. It earns its place on the NEXT blocked
    call, once a half-finished op is sitting in the shared checkout — the
    operator 2026-08-27 state: MERGE_HEAD present, five files staged, no commit.
    Without it an agent reads "move to a worktree" and branches off a dirty
    index, carrying the conflict along instead of unwinding it. Not dead code.
    """
    op = in_progress_op(ctx.get("git_dir", ""))
    if op is None:
        return []
    name, abort = op
    return [
        "",
        f"NOTE: this checkout is ALREADY mid-{name} — an earlier {name} was left unfinished.",
        "Unwind it HERE first; a new worktree would branch off the dirty index:",
        "",
        f"  git -C {repo_dir} {abort}",
        f"  git -C {repo_dir} status     # expect a clean tree",
        "",
        f"If that {name} was wanted, finish it (resolve, `git add`, `git {name} --continue`)",
        "before moving on — do not leave it half-applied for the next session.",
    ]


def deny_reason(others: list[dict], repo_dir: str, ld: Path, ctx: dict) -> str:
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
    lines += unwind_lines(ctx, repo_dir)
    return "\n".join(lines)


def deny_reason_edit(others: list[dict], repo_dir: str, ld: Path, path: str, ctx: dict) -> str:
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
    lines += unwind_lines(ctx, repo_dir)
    return "\n".join(lines)


def _deinit_safe_route(worktree: str, main: str, base: str) -> list[str]:
    """The worktree-removal route that leaves the shared config alone."""
    return [
        "If you are removing a worktree: `git worktree remove` refuses one with",
        "initialized submodules, and deinit does not change that. Instead:",
        "",
        f"  git -C {worktree} status --porcelain      # must print nothing",
        f"  git -C {worktree} cherry {base} HEAD      # no '+' lines: every commit is in {base}",
        f"  git -C {main} worktree remove --force {worktree}",
        "",
        "--force also discards uncommitted work, which is why both checks come first.",
        "If the main checkout already shows '-' in `git submodule status`, re-register it:",
        "",
        f"  git -C {main} submodule init",
    ]


def deny_reason_deinit(repo_dir: str, ctx: dict) -> str:
    # Paste-ready, like suggest_worktree: real paths and the remote's default
    # branch, so the safe route needs no template filling.
    worktree = git(["rev-parse", "--show-toplevel"], repo_dir) or repo_dir
    common = ctx["common_dir"]
    main = os.path.dirname(common) if os.path.basename(common) == ".git" else common
    base = git(["rev-parse", "--abbrev-ref", "origin/HEAD"], repo_dir) or "main"
    return "\n".join([
        "Worktree isolation guard: BLOCKED (submodule deinit in a linked worktree).",
        "",
        f"`git submodule deinit` here edits the repository's SHARED config ({common}/config).",
        "It deletes the submodule.* entries, so the main checkout and every other worktree",
        "lose their submodule registration too, even with no other session running.",
        "",
        *_deinit_safe_route(worktree, main, base),
    ])


def deny_reason_deinit_unresolved(token: str | None) -> str:
    # No repo is known here, so nothing is resolved through git and the safe
    # route keeps placeholders. Naming the token tells the agent what to replace.
    where = f"`{token}`" if token else "a shell expansion"
    return "\n".join([
        "Worktree isolation guard: BLOCKED (submodule deinit, target directory unknown).",
        "",
        f"This `git submodule deinit` runs in a directory given by {where}, which the guard",
        "cannot expand, so it cannot tell whether the target is a linked worktree. In a",
        "linked worktree, deinit deletes the submodule.* entries from the repository's",
        "SHARED .git/config, and the main checkout and every other worktree lose their",
        "submodule registration (dotfiles, 2026-11-13, through `git -C $W`).",
        "",
        "Re-run with the literal absolute path (`git -C /abs/path ...`). Deinit in a main",
        "checkout is then allowed; in a linked worktree the guard denies it again and",
        "prints the route below with real paths filled in.",
        "",
        *_deinit_safe_route("<worktree>", "<main-checkout>", "origin/HEAD"),
    ])


def ask_reason_branch(held: list[dict], repo_dir: str, ctx: dict) -> str:
    names = sorted({str(o.get("branch")) for o in held})
    quoted = ", ".join(f"'{n}'" for n in names)
    lines = [
        "Worktree isolation guard: branch-ownership check.",
        "",
        f"This command folds in {quoted}, which another live Claude session is",
        "working on right now:",
    ]
    for o in held:
        wt = "worktree" if o.get("is_worktree") else "MAIN checkout"
        lines.append(
            f"  - session {str(o.get('session_id', '?'))[:8]} on '{o.get('branch', '?')}'"
            f" ({wt}) at {o.get('cwd', '?')}"
        )
    lines += [
        "",
        f"  repo: {repo_dir}",
        "",
        "git refuses to CHECK OUT a branch another worktree holds, but merges or rebases",
        "it without complaint. Integrating it mid-flight races that session's commits and",
        "can strand a half-finished merge here (operator, 2026-08-27).",
        "",
        "Approve if that session has finished pushing to the branch. Otherwise let it",
        "integrate its own work, or open a PR instead of merging locally.",
    ]
    lines += unwind_lines(ctx, repo_dir)
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


def ask(reason: str) -> None:
    """Escalate to the operator rather than blocking outright.

    "allow", "deny", or "ask" are the PreToolUse permissionDecision values
    (verified against the installed CLI, v2.1.241). Used for branch ownership:
    merging a branch a session merely has open is often legitimate, and a hard
    deny on that would be the false positive that gets a guard ripped out.
    """
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


# --- subcommands -------------------------------------------------------------

def cmd_register(payload: dict) -> None:
    cwd = _cwd(payload)
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
    cwd = _cwd(payload)
    ctx = repo_context(cwd)
    if ctx is None:
        allow()
    remove_lock(lock_dir(ctx["common_dir"]), payload.get("session_id") or "unknown")
    allow()


def cmd_check(payload: dict) -> None:
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or (tool not in EDIT_TOOLS and tool != "Bash"):
        allow()
    cwd = _cwd(payload)
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
            deny(deny_reason_edit(main_others, target_dir, tld, path, tctx))
        allow()

    command = _tool_input(payload).get("command", "")
    if not isinstance(command, str):
        allow()  # shlex.split needs a str; anything else => fail open

    # Resolve the effective repo of each git op (honoring cd / git -C), then run
    # the two checks of the contention model on it. First hit wins; deny
    # outranks ask, since a stomped checkout is the worse outcome.
    targets = _git_targets(command, cwd)
    if not targets:
        allow()  # no git op, or parse failure => fail open
    for eff_dir, args, unresolved in targets:
        if eff_dir is None:
            # Target hidden behind a shell expansion. Deinit fails closed; the
            # other checks need a repo's locks to read, so they fail open (see
            # the module docstring for why the two differ).
            if _is_submodule_deinit(args):
                deny(deny_reason_deinit_unresolved(unresolved))
            continue
        tctx = repo_context(eff_dir)
        if tctx is None:
            continue  # not a repo => allow
        tld = lock_dir(tctx["common_dir"])
        if (tld / OVERRIDE_FILENAME).exists():
            continue  # per-repo escape hatch
        # Shared-config damage needs no second session, so this precedes the gate.
        if tctx["is_worktree"] and _is_submodule_deinit(args):
            deny(deny_reason_deinit(eff_dir, tctx))
        others = live_others(tld, session_id)
        if not others:
            continue  # solo in this repo => never blocked

        # (1) Occupancy — only sessions sharing this SHARED main checkout count.
        # One isolated in a linked worktree has its own HEAD, index and working
        # tree: it cannot be stomped by a commit here, so it must not block one.
        # This is what the Edit path has always done; the Bash path used to
        # count worktree sessions too and denied legitimate solo work in main.
        if not tctx["is_worktree"] and _is_contended_args(args, eff_dir):
            main_others = [o for o in others if not o.get("is_worktree")]
            if main_others:
                deny(deny_reason(main_others, eff_dir, tld, tctx))

        # (2) Branch ownership — the hazard occupancy misses entirely. Runs in
        # worktrees too: the race is on the branch, not on the checkout.
        operands = _integration_operands(args)
        if operands:
            held = [o for o in others if o.get("branch") in operands]
            if held:
                ask(ask_reason_branch(held, eff_dir, tctx))
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

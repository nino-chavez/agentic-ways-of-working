#!/usr/bin/env python3
"""Session reaper — mechanical backstop for agent resource accumulation.

Third sibling to worktree-reaper.py (stale worktrees and their build output)
and connector-reaper.py (orphaned MCP connector processes). Those two cover
what an ended agent session leaves on DISK and in its CONNECTOR tree. This one
covers what it leaves RUNNING: dev servers, browser tabs, and database stacks.

Measured 2026-09-11 in ~/Workspace/dev/apps/rally-hq — the case this exists to
catch, all of it left by sessions that had already ended:

  dev servers   Seven `vite dev` processes started from .worktrees/wave/*,
                running 13-22 hours at 0% CPU. None of their worktrees had a
                live session lock.
  browser tabs  The shared browse-tool Chrome (profile `shared`, CDP 9339)
                held 211 page targets across 232 processes and 18.6 GB RSS.
                52 pointed at a preview server on localhost:8846 that no
                longer listened, 17 at 127.0.0.1:5230, 6 at localhost:5199.
                Closing all but the live ones took it to 4.4 GB / 33 procs.
  supabase      Five local stacks (docker `supabase_<svc>_<project-id>`);
                three up four days with no worktree session using them. Their
                analytics containers alone held 1.4 GB and burned 8-16% CPU
                each. `docker stop` per container freed ~2 GB.

Three independent actions, each separately gated and separately dry-runnable:

  1. dev servers — SIGTERM a `vite dev` / `next dev` / `astro dev` /
     `wrangler dev` process that is BOTH orphaned by process tree AND sitting
     in a linked worktree with no live session lock, older than
     DEV_SERVER_IDLE_HOURS.
  2. browser tabs — close page targets whose URL points at a local port that
     is not listening. Over CDP, one target at a time. The browser process is
     never signalled and the profile directory is never touched (that profile
     holds Nino's logins, and browse-profile-guard.py protects it).
  3. supabase stacks — `docker stop` every container of a local stack whose
     project id no live session claims, running longer than STACK_IDLE_HOURS.
     Never `docker rm`, never a volume, never `supabase stop --no-backup`.

WHY BOTH LIVENESS GATES ON ACTION 1, which is a correction to the brief this
was built from. The specified gate was "cwd is a linked worktree with no live
session lock". That gate alone kills live work. Measured while building this,
in the same repo: pid 71900 `vite dev --port 5213` had cwd
.worktrees/fix/captain-lookup-paginate — a worktree with NO lock — while the
session that started it was alive in .claude/worktrees/keen-dijkstra-63c4f3
and owned the process four ancestors up. A session in worktree A routinely
starts a server in worktree B, and a lock on the worktree cannot see that.
The ancestor walk (borrowed from connector-reaper.py) is what keeps it. Both
signals must independently say "free"; the orphan walk is load-bearing, not
belt and braces.

Safety posture, matching worktree-reaper.py: every gate FAILS CLOSED. ps
fails, lsof cannot resolve a cwd, a session lock will not parse, a port check
errors instead of refusing, docker will not answer — each means "in use", and
nothing is touched. "Could not tell" is never "free".

Off switches: SESSION_REAPER_OFF=1; the per-repo .guard-off file worktree-guard
and worktree-reaper already honour (<git-common-dir>/.claude-sessions/.guard-off);
or SESSION_REAPER_ACTIONS to run a subset (e.g. "dev,tabs").

Modes (argv[1]):
  reap     — hook mode: throttled per repo, budgeted, silent, exits 0 always.
  reap-now — hand-run: the same gates and the same actions, no throttle and no
             wall-clock budget. Mirrors worktree-reaper's reap-now, for the
             pass a 15s hook window cannot fit. Optional argv[2] names the repo.
  report   — dry run: prints what it WOULD do and, as importantly, what it is
             KEEPING and why. Never kills, never closes, never stops.

Wiring (this repo installs only the Claude half, like worktree-reaper):
  Claude Code : SessionEnd -> python3 ~/.claude/hooks/session-reaper.py reap
                installed by install.sh, timeout 30s.
  Codex       : Stop       -> python3 ~/.codex/hooks/session-reaper.py reap
                declared in the CONSUMING dotfiles repo at
                files/home/.codex/hooks.json. Codex hook config is a single
                user-level file rather than something install.sh's ensure()
                can compose, so this repo does not write it. Its timeout is
                20s — the tightest budget this hook runs under, and therefore
                the one DEADLINE_SECONDS is derived from.

Pure stdlib. Log: ~/.claude/logs/session-reaper.log
"""
from __future__ import annotations

import errno
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

# --- tunables (env-overridable) ----------------------------------------------

DEV_SERVER_IDLE_HOURS = float(
    os.environ.get("SESSION_REAPER_DEV_IDLE_HOURS", "1")
)
STACK_IDLE_HOURS = float(os.environ.get("SESSION_REAPER_STACK_IDLE_HOURS", "24"))
THROTTLE_HOURS = float(os.environ.get("SESSION_REAPER_THROTTLE_HOURS", "6"))

# Wall-clock budget. The binding constraint is the TIGHTEST harness timeout
# this hook runs under — Codex's `Stop` at 20s — not Claude's SessionEnd, the
# same derivation worktree-reaper.py documents. Every subprocess timeout is
# clipped to what is left (see clipped()), so total wall clock stays under
# DEADLINE_SECONDS + startup, i.e. 15 + ~1 < 20.
#
# Unlike worktree-reaper, the headroom here is genuinely comfortable rather
# than nominal, because nothing in this file walks a filesystem tree. Measured
# on this machine: `lsof -w -a -d cwd -p <pids>` 0.03s, `docker inspect` on two
# containers 0.02s, one `ps -Aww` ~0.05s, one CDP `GET /json` a few ms.
#
# The ONE genuine cost is `docker stop`, whose default SIGTERM grace is 10s PER
# CONTAINER and a supabase project has ~9 of them. So every container of a
# project goes in ONE call with an explicit short grace (DOCKER_STOP_GRACE),
# and the call is gated on having its whole cap in hand rather than clipped —
# worktree-reaper's REMOVE_TIMEOUT reasoning. Being cut short there is benign
# (some containers stay up; the next pass finishes) but overrunning the harness
# timeout is not.
DEADLINE_SECONDS = float(os.environ.get("SESSION_REAPER_DEADLINE_SECONDS", "15"))
PS_TIMEOUT = 10
LSOF_TIMEOUT = 10
DOCKER_TIMEOUT = 10
DOCKER_STOP_TIMEOUT = 10
DOCKER_STOP_GRACE = int(os.environ.get("SESSION_REAPER_DOCKER_STOP_GRACE", "3"))
CDP_TIMEOUT = float(os.environ.get("SESSION_REAPER_CDP_TIMEOUT", "3"))
PORT_PROBE_TIMEOUT = float(os.environ.get("SESSION_REAPER_PORT_TIMEOUT", "0.4"))
# The stdin payload is the one blocking read clipped() does not cover, because
# it is not a subprocess. See read_payload(), and worktree-reaper.py's much
# longer note on the hang this bound exists to break.
STDIN_PAYLOAD_TIMEOUT = float(os.environ.get("SESSION_REAPER_STDIN_TIMEOUT", "2"))

_STARTED_AT = time.time()
# report and reap-now run from a shell, not under a harness, so they have no
# budget to respect — and a truncated dry run silently under-reports what a
# live reap would do, which is the operator's only verification surface.
_DEADLINE_ON = True

# The agent browser. Overridable so the tests can point action 2 at a local
# http.server fixture and never touch the real 9339.
CDP_URL = os.environ.get("SESSION_REAPER_CDP_URL", "http://127.0.0.1:9339").rstrip("/")

# Mirrors worktree-guard.py / worktree-reaper.py so all three agree on what
# "live" means. Copied rather than imported: these files are hyphenated and so
# are not importable by name, and a hook that dies when a sibling is missing is
# worse than a hook that carries twenty duplicated lines.
STALE_SECONDS = 15 * 60
LOCK_DIRNAME = ".claude-sessions"
OVERRIDE_FILENAME = ".guard-off"
STAMP_FILENAME = ".last-session-reap"

ALL_ACTIONS = ("dev", "tabs", "stacks")

# Substring tests against the full argv, AND-ed with an argv[0] basename test
# below. The substring alone is not enough: a `/bin/zsh -c '... npx vite dev
# --port 5213 ...'` wrapper carries the same text, and so does the `ps | grep`
# that goes looking for one. A reaper that SIGTERMs a shell for mentioning a
# dev server is a bug that shows up once, expensively.
DEV_SERVER_PATTERNS = ("vite dev", "next dev", "astro dev", "wrangler dev")
DEV_SERVER_ARGV0 = {
    "node", "npm", "npx", "pnpm", "yarn", "bun",
    "vite", "next", "astro", "wrangler",
}

# Identifies a live process as an agent client that may legitimately own a dev
# server. Copied from connector-reaper.py's CLIENT_PATTERNS, and deliberately
# broad for the same reason: a false positive means "treated as live, never
# killed", which is the safe direction.
CLIENT_PATTERNS = ("claude", "codex", "chatgpt", "node_repl")

# Agent state/transcript directories. A path under one of these, appearing
# AFTER the executable, is data the agent passed to something else — a shell
# snapshot, a transcript path, an env export — not an identification of the
# process. See _client_text(); the leading token is never stripped, so an agent
# binary that genuinely lives in one of these is still recognised.
AGENT_STATE_DIRS = (".claude", ".codex")

# `NAME=value` — an environment assignment, which names no process. Flags are
# excluded by the leading-character class, so `--port=5198` is untouched.
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}

# Connect errors that say "this address family goes nowhere on this machine",
# as opposed to "nothing is listening" (ECONNREFUSED) or "cannot tell".
_UNROUTABLE = {
    errno.EAFNOSUPPORT, errno.ENETUNREACH, errno.EHOSTUNREACH,
    errno.EADDRNOTAVAIL, errno.EPFNOSUPPORT,
}

DB_CONTAINER_PREFIX = "supabase_db_"
CONTAINER_PREFIX = "supabase_"

# Credential redaction, copied from connector-reaper.py. Dev-server argv is not
# the credential-carrying surface MCP argv is, but this file logs command lines
# and a `zsh -c` wrapper in the observed tree carried environment exports.
_CREDENTIAL_FLAG_RE = re.compile(
    r"(--api-key|--token|--secret|--password|--bearer)(=|\s+)(\S+)", re.IGNORECASE
)
_CREDENTIAL_PREFIX_RE = re.compile(
    r"(sk-ant-|sk-proj-|ctx7sk-|ghp_|xox[a-z]-)[A-Za-z0-9_-]*"
)

LOG_PATH = Path(
    os.environ.get(
        "SESSION_REAPER_LOG",
        str(Path.home() / ".claude" / "logs" / "session-reaper.log"),
    )
)


# --- logging / budget --------------------------------------------------------

def log(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {msg}\n")
    except Exception:
        pass


def budget_left() -> float:
    if not _DEADLINE_ON:
        return float("inf")
    return DEADLINE_SECONDS - (time.time() - _STARTED_AT)


def past_deadline() -> bool:
    return budget_left() <= 0


def clipped(cap: float) -> float:
    """A subprocess timeout that cannot outlive the run's remaining budget.

    Safe for every caller here because each fails toward doing nothing: a
    truncated ps aborts the pass, a truncated lsof leaves a cwd unresolved
    (skip), a truncated docker inspect leaves an uptime unknown (skip).
    `docker stop` is deliberately NOT clipped — see DEADLINE_SECONDS.
    """
    if not _DEADLINE_ON:
        return cap
    return max(1.0, min(cap, budget_left()))


def redact(command: str) -> str:
    """Never let a full credential reach stdout or the log. From connector-reaper."""
    def _flag_sub(m: re.Match) -> str:
        return f"{m.group(1)}{m.group(2)}{m.group(3)[:6]}***"

    out = _CREDENTIAL_FLAG_RE.sub(_flag_sub, command)
    return _CREDENTIAL_PREFIX_RE.sub(lambda m: f"{m.group(0)[:6]}***", out)


def truncate(s: str, n: int = 120) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def safe_cmd(command: str) -> str:
    """What goes in a log line: redacted, then capped."""
    return truncate(redact(command))


def fmt_hours(seconds: float) -> str:
    h = seconds / 3600.0
    if h < 1:
        return f"{seconds / 60:.0f}m"
    if h < 48:
        return f"{h:.1f}h"
    return f"{h / 24:.1f}d"


def read_payload(timeout: float = STDIN_PAYLOAD_TIMEOUT) -> dict:
    """The hook payload from stdin, or {} if it does not arrive in `timeout`.

    NOT `json.load(sys.stdin)`, which reads to EOF — an inherited stdin may
    never deliver one, parking the hook at 0% CPU with nothing in any log.
    worktree-reaper.py's read_payload() carries the full measurement (commit
    56087f7); this is the same bound for the same reason.
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


# --- git / session locks -----------------------------------------------------

def git(args: list[str], cwd: str, timeout: float = 10) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=clipped(timeout),
        )
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def common_dir(cwd: str) -> str | None:
    if not os.path.isdir(cwd):
        return None
    c = git(["rev-parse", "--git-common-dir"], cwd)
    if c is None:
        return None
    return os.path.abspath(os.path.join(cwd, c))


def all_worktrees(cwd: str) -> list[dict] | None:
    """Every worktree via `git worktree list --porcelain`, main checkout first.

    None (not []) on failure. Collapsing "could not enumerate" into "no
    worktrees" is the bug class worktree-reaper.py's all_worktrees() names; here
    it would be worse, because an empty list would read as "no worktree claims
    this supabase project" and make every stack eligible.
    """
    out = git(["worktree", "list", "--porcelain"], cwd)
    if out is None:
        return None
    entries: list[dict] = []
    cur: dict = {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if cur.get("path"):
                entries.append(cur)
            cur = {"path": line[len("worktree "):].strip(), "main": not entries}
        elif line.startswith("bare"):
            cur["bare"] = True
    if cur.get("path"):
        entries.append(cur)
    return entries


def live_session_dirs(cd: str) -> tuple[list[str], bool]:
    """(cwds of sessions whose heartbeat is fresh, could_read_them_all).

    worktree-reaper's version returns just the list, because there a false
    "nothing live" defers a deletion of gitignored build output. Here it gates a
    SIGTERM and a `docker stop`, so an unreadable or unparseable lock file has to
    mean "cannot tell" and stop the action, not "nobody is home".

    An ABSENT lock dir is not a failure — it legitimately means no session has
    registered in this repo — so ok stays True for that.
    """
    ld = Path(cd) / LOCK_DIRNAME
    now = time.time()
    out: list[str] = []
    ok = True
    try:
        entries = list(ld.glob("*.json"))
    except Exception:
        return [], False
    for f in entries:
        try:
            if now - f.stat().st_mtime > STALE_SECONDS:
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            ok = False
            continue
        c = data.get("cwd")
        if isinstance(c, str) and c:
            # realpath, not abspath: `git worktree list` reports symlink-resolved
            # paths, and a lock written from a symlinked cwd (macOS /var ->
            # /private/var) compared by abspath never matches — so the occupancy
            # gate reads an occupied worktree as free. worktree-reaper.py hit
            # exactly this.
            out.append(os.path.realpath(c))
        else:
            ok = False
    return out, ok


def under(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent + os.sep)


def throttled(cd: str) -> bool:
    """True if this repo was reaped recently. Codex fires Stop per TURN.

    One stamp for all three actions, keyed per repo, mirroring worktree-reaper.
    Actions 2 and 3 are machine-global while action 1 is repo-scoped, so a
    global stamp would let a pass in repo A suppress the first pass repo B ever
    got. Per-repo cannot do that. The cost is that the browser and docker checks
    may re-run once per repo per window — one CDP GET and one `docker ps`, which
    the budget note above prices at a few hundredths of a second.
    """
    try:
        stamp = Path(cd) / LOCK_DIRNAME / STAMP_FILENAME
        return time.time() - stamp.stat().st_mtime < THROTTLE_HOURS * 3600
    except Exception:
        return False


def touch_stamp(cd: str) -> None:
    try:
        p = Path(cd) / LOCK_DIRNAME
        p.mkdir(parents=True, exist_ok=True)
        (p / STAMP_FILENAME).write_text(str(int(time.time())), encoding="utf-8")
    except Exception:
        pass


# --- process snapshot (from connector-reaper.py) ------------------------------

def parse_etime(raw: str) -> float:
    """ps's `etime` field: [[dd-]hh:]mm:ss -> seconds. From connector-reaper.py."""
    days = 0
    s = raw.strip()
    if "-" in s:
        d, s = s.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            days = 0
    try:
        nums = [int(p) for p in s.split(":")]
    except ValueError:
        return 0.0
    if len(nums) == 3:
        h, m, sec = nums
    elif len(nums) == 2:
        h, (m, sec) = 0, nums
    elif len(nums) == 1:
        h, m, sec = 0, 0, nums[0]
    else:
        return 0.0
    return float(days * 86400 + h * 3600 + m * 60 + sec)


def snapshot_processes() -> dict[int, dict] | None:
    """pid -> {ppid, etime, rss_kb, command}, or None on failure.

    None (not {}) on any ps failure, so a transient ps problem can never be read
    as "nothing is running". From connector-reaper.py.
    """
    try:
        r = subprocess.run(
            ["ps", "-Aww", "-o", "pid=,ppid=,etime=,rss=,command="],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=clipped(PS_TIMEOUT),
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    procs: dict[int, dict] = {}
    for line in r.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid, ppid, rss_kb = int(parts[0]), int(parts[1]), int(parts[3])
        except ValueError:
            continue
        procs[pid] = {
            "ppid": ppid,
            "etime": parse_etime(parts[2]),
            "rss_kb": rss_kb,
            "command": parts[4],
        }
    return procs


def _client_text(command: str, strip_prefixes: tuple[str, ...]) -> str:
    """A command line with non-leading tokens naming "noise" paths removed.

    Two measured false positives, both of which make an orphan read as owned and
    so quietly switch the dev-server action off:

      1. A repo path. pid 30300's argv was `node
         /Users/nino/.../rally-hq/.worktrees/codex/bloom-application-journey/
         node_modules/.bin/vite dev --port 5198` — which contains "codex", a
         BRANCH NAME. Its npm parent carried no path and was correctly read as
         an orphan, so the pass would have SIGTERMed half the process group.
      2. An agent's own plumbing, quoted inside a shell wrapper. The shell
         Claude's Bash tool wraps a command in survives as long as its child
         does: `/bin/zsh -c source /Users/nino/.claude/shell-snapshots/….sh …
         export CODEX_COMPANION_TRANSCRIPT_PATH=/Users/nino/.claude/projects/
         ….jsonl`. Both "claude" and "codex" are in there as DATA — and note
         the second one twice over, in the path AND in the VARIABLE NAME, which
         is why stripping paths alone did not fix it. Reparented to pid 1 after
         its session ends, that wrapper would exempt every dev server beneath
         it, forever.

    So two kinds of token go: a path under one of `strip_prefixes`, and an
    environment assignment (`NAME=…`), which is never how a process is named.

    THE FIRST TOKEN IS NEVER STRIPPED, and that is the part that keeps this
    safe rather than clever. A real agent process can live inside one of these
    directories — `/Users/nino/.codex/computer-use/Codex Computer Use.app/…` is
    running on this machine right now — and stripping its executable would hide
    a live client, which is the one direction that destroys work. Only what
    comes AFTER the executable is treated as noise.

    Nor can this be replaced by "test argv[0] only": macOS agent paths contain
    spaces (`~/Library/Application Support/Claude/claude-code/…`), so a
    whitespace split puts the identifying part in a later token.

    connector-reaper.py does not need any of this — MCP argv is package names,
    not paths — which is why the rule lives here and not there.
    """
    if not strip_prefixes:
        return command

    def drop(tok: str) -> bool:
        return bool(_ENV_ASSIGNMENT_RE.match(tok)) or any(
            tok.startswith(p) for p in strip_prefixes
        )

    return " ".join(
        tok if i == 0 or not drop(tok) else ""
        for i, tok in enumerate(command.split())
    )


def _is_client(command: str, strip_prefixes: tuple[str, ...] = ()) -> bool:
    lowered = _client_text(command, strip_prefixes).lower()
    return any(p in lowered for p in CLIENT_PATTERNS)


def ancestor_owner(
    pid: int, procs: dict[int, dict], strip_prefixes: tuple[str, ...] = (),
    max_depth: int = 64,
) -> str:
    """"live_client" | "orphan" | "unknown". From connector-reaper.py.

    "unknown" — a pid in the chain is missing from the snapshot, or the walk
    cycled, or it ran past max_depth. Never a basis for killing.
    """
    seen: set[int] = set()
    cur = pid
    for _ in range(max_depth):
        if cur in seen:
            return "unknown"
        seen.add(cur)
        info = procs.get(cur)
        if info is None:
            return "unknown"
        if _is_client(info["command"], strip_prefixes):
            return "live_client"
        if cur == 1:
            return "orphan"
        cur = info["ppid"]
    return "unknown"


# --- action 1: orphan dev servers --------------------------------------------

def looks_like_dev_server(command: str) -> bool:
    """argv[0] basename is a JS runtime/runner AND the argv names a dev server."""
    parts = command.split()
    if not parts:
        return False
    if os.path.basename(parts[0]) not in DEV_SERVER_ARGV0:
        return False
    return any(p in command for p in DEV_SERVER_PATTERNS)


def process_cwds(pids: list[int]) -> dict[int, str]:
    """pid -> realpath'd cwd, for the pids lsof could resolve.

    One lsof call for every pid, not one per pid. `-w` suppresses warnings: a
    pid that exited between `ps` and here makes lsof exit non-zero while still
    printing every other pid's cwd correctly, and without -w that partial
    success reads as total failure.

    A pid ABSENT from the result has no entry, and every caller treats a missing
    cwd as "cannot tell" -> skip. That is the fail-closed direction.
    """
    if not pids:
        return {}
    try:
        r = subprocess.run(
            ["lsof", "-w", "-a", "-d", "cwd", "-Fpn",
             "-p", ",".join(str(p) for p in pids)],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=clipped(LSOF_TIMEOUT),
        )
    except Exception:
        return {}
    out: dict[int, str] = {}
    cur: int | None = None
    for line in r.stdout.splitlines():
        if line.startswith("p"):
            try:
                cur = int(line[1:])
            except ValueError:
                cur = None
        elif line.startswith("n") and cur is not None:
            path = line[1:]
            if path.startswith("/"):
                try:
                    out[cur] = os.path.realpath(path)
                except Exception:
                    pass
            cur = None
    return out


def plan_dev_servers(cwd: str, cd: str) -> tuple[list[dict], list[dict]]:
    """(kill, keep) rows for every dev-server process in this repo's worktrees.

    A row is eligible only when ALL of these hold, and every one of them fails
    closed:
      - argv says dev server (looks_like_dev_server)
      - lsof resolved its cwd
      - that cwd sits inside a LINKED worktree of this repo (never the main
        checkout, never some other repo)
      - that worktree has no live session lock, and every lock file parsed
      - its ancestor chain reaches pid 1 without passing a live agent client
      - elapsed time > DEV_SERVER_IDLE_HOURS
    """
    keep: list[dict] = []
    kill: list[dict] = []

    procs = snapshot_processes()
    if procs is None:
        keep.append({"reason": "ps_enumeration_failed", "scope": "action"})
        return kill, keep

    candidates = {
        pid: info for pid, info in procs.items()
        if looks_like_dev_server(info["command"])
    }
    if not candidates:
        return kill, keep

    entries = all_worktrees(cwd)
    if entries is None:
        keep.append({"reason": "worktree_enumeration_failed", "scope": "action"})
        return kill, keep
    linked = [
        os.path.realpath(e["path"])
        for e in entries
        if not e.get("main") and not e.get("bare") and os.path.isdir(e["path"])
    ]
    # Paths that never IDENTIFY an agent client when they appear after the
    # executable: every worktree of this repo (a branch named `codex/…`), and
    # the agents' own state directories (a wrapped shell carrying a transcript
    # path under ~/.claude). See _client_text() for both measured cases.
    repo_paths = tuple(
        sorted(
            {os.path.realpath(e["path"]) for e in entries}
            | {os.path.realpath(e["path"]) + os.sep for e in entries}
            | {str(Path.home() / d) for d in AGENT_STATE_DIRS}
        )
    )

    live, live_ok = live_session_dirs(cd)
    if not live_ok:
        keep.append({"reason": "session_locks_unreadable", "scope": "action"})
        return kill, keep
    me = os.path.realpath(cwd)

    cwds = process_cwds(sorted(candidates))
    for pid in sorted(candidates):
        info = candidates[pid]
        row = {
            "pid": pid,
            "age_s": info["etime"],
            "rss_kb": info["rss_kb"],
            "command": safe_cmd(info["command"]),
            "worktree": None,
        }
        proc_cwd = cwds.get(pid)
        if proc_cwd is None:
            row["reason"] = "cwd_unresolved"
            keep.append(row)
            continue
        wt = next((w for w in linked if under(proc_cwd, w)), None)
        if wt is None:
            row["reason"] = "not_in_a_linked_worktree"
            row["worktree"] = proc_cwd
            keep.append(row)
            continue
        row["worktree"] = wt
        if under(me, wt):
            row["reason"] = "this_session's_worktree"
            keep.append(row)
            continue
        if any(under(d, wt) for d in live):
            row["reason"] = "live_session_lock"
            keep.append(row)
            continue
        owner = ancestor_owner(pid, procs, repo_paths)
        if owner != "orphan":
            # The load-bearing gate. A session in worktree A starting a server in
            # worktree B leaves B lockless while the process is still owned.
            row["reason"] = f"ancestor_{owner}"
            keep.append(row)
            continue
        if info["etime"] <= DEV_SERVER_IDLE_HOURS * 3600:
            row["reason"] = f"younger_than_{DEV_SERVER_IDLE_HOURS}h"
            keep.append(row)
            continue
        kill.append(row)
    return kill, keep


def kill_pid(pid: int) -> bool:
    """SIGTERM, never SIGKILL: a dev server gets to close its sockets and its
    file watchers. Already-gone counts as success."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return True
    except Exception:
        return False


# --- action 2: dead-host tabs in the agent browser ----------------------------

def cdp_get(path: str) -> object | None:
    try:
        with urllib.request.urlopen(f"{CDP_URL}{path}", timeout=CDP_TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    try:
        return json.loads(body)
    except Exception:
        return body


def local_port(url: str) -> int | None:
    """The port of a URL pointing at this machine, or None if it is not local."""
    try:
        parts = urlsplit(url)
    except Exception:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if host not in LOCAL_HOSTS:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return port


def port_listening(host: str, port: int) -> bool | None:
    """True listening, False refused on every address, None uncertain.

    getaddrinfo rather than a hardcoded 127.0.0.1, because `localhost` resolves
    to BOTH families and this repo's dev servers bind variously: some with
    `--host 127.0.0.1`, some on the default. Probing only IPv4 against a server
    listening on ::1 reads "not listening" and closes a working tab.

    None on any error that is not a refusal — an unreachable family, a timeout,
    a resolver failure — because this gates closing someone's tab.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception:
        return None
    if not infos:
        return None
    refused = False
    for family, socktype, proto, _canon, addr in infos:
        s = socket.socket(family, socktype, proto)
        s.settimeout(PORT_PROBE_TIMEOUT)
        try:
            s.connect(addr)
            return True
        except ConnectionRefusedError:
            refused = True
        except OSError as exc:
            # A family the machine does not route is not evidence either way, so
            # the other family still decides. Anything else — notably a timeout,
            # which is what a firewalled-but-live port looks like — makes the
            # whole answer uncertain, and uncertain keeps the tab.
            if exc.errno not in _UNROUTABLE:
                return None
        except Exception:
            return None
        finally:
            s.close()
    return False if refused else None


def plan_tabs() -> tuple[list[dict], list[dict]]:
    """(close, keep) rows for the agent browser's page targets.

    Skips entirely — no rows, no error — when CDP does not answer: the browser
    simply is not running, which is the common case and not a failure.
    """
    close: list[dict] = []
    keep: list[dict] = []

    targets = cdp_get("/json")
    if not isinstance(targets, list):
        keep.append({"reason": "cdp_not_answering", "scope": "action"})
        return close, keep

    pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
    port_state: dict[int, bool | None] = {}
    for t in pages:
        url = str(t.get("url", ""))
        tid = t.get("id")
        row = {"id": tid, "url": truncate(url, 100), "port": None}
        if not tid:
            row["reason"] = "no_target_id"
            keep.append(row)
            continue
        port = local_port(url)
        if port is None:
            row["reason"] = "not_a_local_url"
            keep.append(row)
            continue
        row["port"] = port
        host = (urlsplit(url).hostname or "127.0.0.1").lower()
        if port not in port_state:
            # Cached per pass: the measured case had 52 tabs on one dead port.
            port_state[port] = port_listening(host, port)
        state = port_state[port]
        if state is None:
            row["reason"] = "port_probe_uncertain"
            keep.append(row)
        elif state:
            row["reason"] = "port_listening"
            keep.append(row)
        else:
            close.append(row)

    # Chrome exits when its last page target closes, and ending the browser is
    # the one thing this action must never do — the profile holds Nino's logins
    # and browse-profile-guard.py exists to protect it. Keep one page back.
    if close and len(close) == len(pages):
        spared = close.pop()
        spared["reason"] = "last_remaining_page"
        keep.append(spared)
    return close, keep


def close_tab(target_id: str) -> bool:
    return cdp_get(f"/json/close/{target_id}") is not None


# --- action 3: idle local supabase stacks -------------------------------------

def docker(args: list[str], timeout: float, clip: bool = True) -> str | None:
    try:
        r = subprocess.run(
            ["docker", *args],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
            timeout=clipped(timeout) if clip else timeout,
        )
    except Exception:
        return None
    return r.stdout if r.returncode == 0 else None


def project_id_of(worktree: str) -> str | None:
    """`project_id` from a worktree's supabase/config.toml, or None."""
    try:
        text = (Path(worktree) / "supabase" / "config.toml").read_text(
            encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        return None
    except Exception:
        raise
    m = re.search(r'^\s*project_id\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


def parse_docker_time(raw: str) -> float | None:
    """Docker's RFC3339 timestamp -> epoch seconds. None if unparseable."""
    s = raw.strip()
    if not s or s.startswith("0001-01-01"):
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?", s)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except Exception:
        return None
    return dt.timestamp()


def plan_stacks(cwd: str, cd: str, sweep: bool = False) -> tuple[list[dict], list[dict]]:
    """(stop, keep) rows, one per local supabase project.

    Claimed (never stopped, in either mode):
      - the MAIN checkout's own project id, unconditionally exempt
      - the project id of any worktree holding a live session lock

    SCOPE, and the reason `sweep` exists. `docker ps` is machine-wide while the
    session locks and worktrees are this repo's, so an unqualified rule lets one
    repo judge another repo's stacks. Traced before shipping: a SessionEnd in a
    repo with no supabase at all (blog, website-nc, dotfiles) reads every
    project_id as None, computes an EMPTY claimed set, and makes every stack on
    the machine eligible — including `supabase_db_rally-hq` at any moment no
    rally-hq session happens to be live. Volumes survive, so it is recoverable,
    but it interrupts live work, and this hook fires on every session end in
    every repo.

    So hook mode (sweep=False) only considers a project that some worktree of
    THIS repo declares in its supabase/config.toml. A repo that knows nothing
    about a stack does not get to stop it.

    `reap-now` and `report` pass sweep=True, which drops the declaration
    requirement and keeps every other gate. That is the operator-present path,
    and it is the one that reaches a stack whose creating worktree has since
    been reset — measured 2026-09-11, `rally-bloom-journey-20260910` appears in
    no config.toml on disk, so the hook alone would never reclaim it.

    Uptime is `State.StartedAt`, not `Created`: a stack created four days ago
    but started an hour ago (a Docker Desktop restart) reads as one hour, which
    defers the reap. Wrong in the safe direction — it waits longer, never
    shorter — and `Created` is reported alongside so the difference is visible.
    """
    stop: list[dict] = []
    keep: list[dict] = []

    names_out = docker(["ps", "--filter", f"name={CONTAINER_PREFIX}",
                        "--format", "{{.Names}}"], DOCKER_TIMEOUT)
    if names_out is None:
        keep.append({"reason": "docker_unavailable", "scope": "action"})
        return stop, keep
    names = [n.strip() for n in names_out.splitlines() if n.strip()]
    dbs = [n for n in names if n.startswith(DB_CONTAINER_PREFIX)]
    if not dbs:
        return stop, keep

    entries = all_worktrees(cwd)
    if entries is None:
        keep.append({"reason": "worktree_enumeration_failed", "scope": "action"})
        return stop, keep
    live, live_ok = live_session_dirs(cd)
    if not live_ok:
        keep.append({"reason": "session_locks_unreadable", "scope": "action"})
        return stop, keep

    claimed: set[str] = set()   # in use right now — never stopped
    declared: set[str] = set()  # this repo knows about it — hook mode's scope
    me = os.path.realpath(cwd)
    for e in entries:
        path = os.path.realpath(e["path"])
        occupied = e.get("main") or under(me, path) or any(under(d, path) for d in live)
        try:
            pid_ = project_id_of(path)
        except Exception:
            if occupied:
                # An occupied worktree whose config we cannot read could be
                # claiming any project. Fail closed on the whole action.
                keep.append({"reason": "config_toml_unreadable", "scope": "action"})
                return [], keep
            continue
        if not pid_:
            continue
        declared.add(pid_)
        if occupied:
            claimed.add(pid_)

    projects = sorted(
        (n[len(DB_CONTAINER_PREFIX):] for n in dbs), key=len, reverse=True
    )

    def containers_of(project: str) -> list[str]:
        # Longest project wins, so a short id that is a suffix of a longer one
        # ("hq" under "rally-hq") cannot steal the other's containers.
        out = []
        for n in names:
            owner = next(
                (p for p in projects
                 if n.startswith(CONTAINER_PREFIX) and n.endswith("_" + p)),
                None,
            )
            if owner == project:
                out.append(n)
        return out

    inspect = docker(
        ["inspect", "--format", "{{.Name}}|{{.State.StartedAt}}|{{.Created}}", *dbs],
        DOCKER_TIMEOUT,
    )
    started: dict[str, tuple[float | None, float | None]] = {}
    for line in (inspect or "").splitlines():
        parts = line.strip().lstrip("/").split("|")
        if len(parts) != 3:
            continue
        started[parts[0]] = (parse_docker_time(parts[1]), parse_docker_time(parts[2]))

    now = time.time()
    for project in sorted(projects):
        db = DB_CONTAINER_PREFIX + project
        # Uptime is filled in BEFORE the gates rather than inside them, so a row
        # kept for some other reason still carries the number. A report line
        # reading `up ?` next to "claimed" tells the operator nothing about
        # whether the stack is one they have forgotten.
        start_at, created_at = started.get(db, (None, None))
        row = {
            "project": project,
            "containers": containers_of(project),
            "uptime_s": (now - start_at) if start_at else None,
            "created_age_s": (now - created_at) if created_at else None,
        }
        if project in claimed:
            row["reason"] = "claimed_by_live_session_or_main"
            keep.append(row)
            continue
        if not sweep and project not in declared:
            # Not this repo's stack to judge. See the SCOPE note above.
            row["reason"] = "not_declared_by_this_repo"
            keep.append(row)
            continue
        if start_at is None:
            row["reason"] = "uptime_unknown"
            keep.append(row)
            continue
        if row["uptime_s"] <= STACK_IDLE_HOURS * 3600:
            row["reason"] = f"running_less_than_{STACK_IDLE_HOURS:.0f}h"
            keep.append(row)
            continue
        if not row["containers"]:
            row["reason"] = "no_containers_resolved"
            keep.append(row)
            continue
        stop.append(row)
    return stop, keep


def stop_stack(containers: list[str]) -> bool:
    """`docker stop` every container of one project in ONE call.

    Never `docker rm`, never a volume, never `supabase stop`. Volumes and their
    data survive; `supabase start` brings the stack back with its database
    intact. `-t` because the default SIGTERM grace is 10s per container and a
    supabase project has about nine of them, which alone would outlive the hook
    budget. Not clipped — the caller gates on having the whole cap.
    """
    out = docker(
        ["stop", "-t", str(DOCKER_STOP_GRACE), *containers],
        DOCKER_STOP_TIMEOUT, clip=False,
    )
    return out is not None


# --- modes -------------------------------------------------------------------

def selected_actions() -> tuple[str, ...]:
    """Which actions this run may take. Unset means all; empty means none.

    `if not raw` would have collapsed those two, so SESSION_REAPER_ACTIONS=""
    — the obvious way to write "do nothing" — enabled everything instead. An
    off switch that turns the tool fully on is the worst available default.
    """
    raw = os.environ.get("SESSION_REAPER_ACTIONS")
    if raw is None:
        return ALL_ACTIONS
    wanted = {p.strip() for p in raw.split(",") if p.strip()}
    return tuple(a for a in ALL_ACTIONS if a in wanted)


def _repo_context(cwd: str) -> tuple[str, str] | None:
    cd = common_dir(cwd)
    if cd is None:
        return None
    if (Path(cd) / LOCK_DIRNAME / OVERRIDE_FILENAME).exists():
        return None
    return cwd, cd


def cmd_reap(payload: dict, now: bool = False) -> None:
    """Hook reap, or — with `now` — the hand-run, unthrottled, unbudgeted one.

    `now` drops the throttle and the deadline and NOTHING else: every gate in
    every plan_* function still applies.

    The session-lock set is re-read once between planning and killing, because
    that gap is the wide one — a session can start in a worktree while the
    plan phase is walking the rest of the repo, and it would lose its dev
    server. worktree-reaper.py does the same for the same reason. Tabs and
    stacks get no second read: a dead port and a 24-hour-idle container do not
    become live inside a few hundred milliseconds, and a second `docker ps`
    would cost more budget than it protects.

    Every action is wrapped, so a failure in one cannot take the hook down
    with a traceback or stop the other two. A hook that exits non-zero is a
    visible error in the harness for something the operator did not ask for.
    """
    global _DEADLINE_ON
    if os.environ.get("SESSION_REAPER_OFF") == "1":
        return
    ctx = _repo_context(payload.get("cwd") or os.getcwd())
    if ctx is None:
        return
    cwd, cd = ctx
    if now:
        _DEADLINE_ON = False
    elif throttled(cd):
        return

    actions = selected_actions()
    n_dev = dev_kb = n_tabs = n_stacks = n_containers = 0
    truncated = False

    if "dev" in actions and not past_deadline():
        try:
            kill, _keep = plan_dev_servers(cwd, cd)
        except Exception as exc:
            kill = []
            log(f"repo={cwd} dev action failed: {exc.__class__.__name__}: {exc}")
        # Re-read the lock set: the plan phase is the wide window, and a session
        # that started in one of these worktrees while it ran would otherwise
        # lose its dev server. Unreadable now means nothing is eligible.
        live_now, live_ok_now = live_session_dirs(cd)
        if kill and not live_ok_now:
            log(f"repo={cwd} session locks became unreadable before killing; skipping")
            kill = []
        for row in kill:
            if past_deadline():
                truncated = True
                break
            if any(under(d, row["worktree"]) for d in live_now):
                log(
                    f"skip pid={row['pid']} reason=session_started_while_planning "
                    f"worktree={row['worktree']}"
                )
                continue
            if kill_pid(row["pid"]):
                n_dev += 1
                dev_kb += row["rss_kb"]
                log(
                    f"killed_dev_server pid={row['pid']} "
                    f"age={fmt_hours(row['age_s'])} rss={row['rss_kb'] // 1024}MB "
                    f"worktree={row['worktree']} cmd={row['command']}"
                )
            else:
                log(f"kill_failed pid={row['pid']} cmd={row['command']}")

    if "tabs" in actions and not past_deadline():
        try:
            close, _keep = plan_tabs()
        except Exception as exc:
            close = []
            log(f"repo={cwd} tabs action failed: {exc.__class__.__name__}: {exc}")
        for row in close:
            if past_deadline():
                truncated = True
                break
            if close_tab(str(row["id"])):
                n_tabs += 1
                log(
                    f"closed_tab port={row['port']} reason=port_not_listening "
                    f"url={row['url']}"
                )
            else:
                log(f"close_tab_failed port={row['port']} url={row['url']}")

    if "stacks" in actions and not past_deadline():
        try:
            # sweep only with the operator present. The hook confines itself to
            # stacks this repo declares; see plan_stacks()'s SCOPE note.
            stop, _keep = plan_stacks(cwd, cd, sweep=now)
        except Exception as exc:
            stop = []
            log(f"repo={cwd} stacks action failed: {exc.__class__.__name__}: {exc}")
        for row in stop:
            # Not clipped, so it runs only with its whole cap in hand. Being cut
            # short here is benign — some containers stay up and the next pass
            # finishes them — but overrunning the harness timeout is not.
            if budget_left() < DOCKER_STOP_TIMEOUT:
                truncated = True
                break
            if stop_stack(row["containers"]):
                n_stacks += 1
                n_containers += len(row["containers"])
                log(
                    f"stopped_stack project={row['project']} "
                    f"containers={len(row['containers'])} "
                    f"uptime={fmt_hours(row['uptime_s'])} "
                    f"names={','.join(row['containers'])}"
                )
            else:
                log(f"stop_stack_failed project={row['project']}")

    # Stamp unless a retry could genuinely do better. worktree-reaper.py's rule:
    # the test is PROGRESS, not truncation — a truncated run that freed nothing
    # would recompute the same thing next turn and, under Codex's per-turn Stop,
    # forever.
    progressed = n_dev or n_tabs or n_stacks
    if not (truncated and progressed):
        touch_stamp(cd)
    log(
        f"repo={cwd} {'reap-now ' if now else ''}"
        f"dev_servers={n_dev} dev_rss_freed={dev_kb // 1024}MB "
        f"tabs_closed={n_tabs} stacks_stopped={n_stacks} "
        f"containers_stopped={n_containers} "
        f"elapsed={time.time() - _STARTED_AT:.1f}s"
        + (f" TRUNCATED at {DEADLINE_SECONDS}s budget" if truncated else "")
    )


def cmd_report(cwd: str) -> None:
    global _DEADLINE_ON
    _DEADLINE_ON = False

    cd = common_dir(cwd)
    print(f"repo: {cwd}")
    print(
        f"gates: dev servers orphaned+lockless idle>{DEV_SERVER_IDLE_HOURS}h, "
        f"tabs on a dead local port, stacks unclaimed+up>{STACK_IDLE_HOURS:.0f}h, "
        f"throttle {THROTTLE_HOURS:.0f}h"
    )
    print(f"browser: {CDP_URL}   actions: {','.join(selected_actions())}")
    if cd is None:
        print("NOTE: not a git repo — the dev-server and stack actions need one.")
        sys.exit(0)
    if (Path(cd) / LOCK_DIRNAME / OVERRIDE_FILENAME).exists():
        print(f"NOTE: disabled for this repo ({LOCK_DIRNAME}/{OVERRIDE_FILENAME}).")
    if throttled(cd):
        print("NOTE: currently throttled — a live `reap` would no-op right now.")
    live, live_ok = live_session_dirs(cd)
    print(f"live sessions: {len(live)}{'' if live_ok else '  (SOME LOCKS UNREADABLE)'}")
    for d in live:
        print(f"  {d}")
    print()

    actions = selected_actions()

    if "dev" in actions:
        kill, keep = plan_dev_servers(cwd, cd)
        print(f"[1] dev servers to SIGTERM ({len(kill)}):")
        for row in kill:
            print(
                f"  pid={row['pid']:<7} age={fmt_hours(row['age_s']):>7} "
                f"rss={row['rss_kb'] // 1024:>5}MB  {row['worktree']}"
            )
            print(f"      {row['command']}")
        if not kill:
            print("  (none)")
        print(f"    keeping ({len(keep)}):")
        for row in keep:
            if row.get("scope") == "action":
                print(f"      ACTION SKIPPED — {row['reason']}")
                continue
            print(
                f"      pid={row['pid']:<7} age={fmt_hours(row['age_s']):>7} "
                f"reason={row['reason']}"
            )
            print(f"        {row['command']}")
        print()

    if "tabs" in actions:
        close, keep = plan_tabs()
        by_port: dict[object, int] = {}
        for row in close:
            by_port[row["port"]] = by_port.get(row["port"], 0) + 1
        print(f"[2] browser tabs to close ({len(close)}):")
        for port, n in sorted(by_port.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>4} tab(s) on dead local port {port}")
        for row in close:
            print(f"      {row['url']}")
        if not close:
            print("  (none)")
        skipped = [r for r in keep if r.get("scope") == "action"]
        print(f"    keeping ({len(keep) - len(skipped)} page target(s)):")
        for row in skipped:
            print(f"      ACTION SKIPPED — {row['reason']}")
        for row in keep:
            if row.get("scope") == "action":
                continue
            print(f"      reason={row['reason']:<22} {row['url']}")
        print()

    if "stacks" in actions:
        stop, keep = plan_stacks(cwd, cd)
        sweep_stop, _sweep_keep = plan_stacks(cwd, cd, sweep=True)
        extra = [r for r in sweep_stop if r["project"] not in {s["project"] for s in stop}]
        print(f"[3] supabase stacks to `docker stop` ({len(stop)}):")
        for row in stop:
            print(
                f"  {row['project']}  up {fmt_hours(row['uptime_s'])}, "
                f"created {fmt_hours(row['created_age_s']) if row['created_age_s'] else '?'} ago, "
                f"{len(row['containers'])} container(s)"
            )
        if not stop:
            print("  (none)")
        if extra:
            print(f"    `reap-now` would ALSO stop ({len(extra)}) — "
                  "unclaimed, but declared in no worktree of this repo, so the "
                  "hook leaves them to the operator:")
            for row in extra:
                print(
                    f"      {row['project']}  up {fmt_hours(row['uptime_s'])}, "
                    f"{len(row['containers'])} container(s)"
                )
        print(f"    keeping ({len([r for r in keep if r.get('scope') != 'action'])}):")
        for row in keep:
            if row.get("scope") == "action":
                print(f"      ACTION SKIPPED — {row['reason']}")
                continue
            up = fmt_hours(row["uptime_s"]) if row["uptime_s"] else "?"
            print(
                f"      {row['project']:<36} up {up:>7}  reason={row['reason']}"
            )
    sys.exit(0)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "reap"
    if mode == "report":
        # Never touches stdin: run by hand from a shell, not by a hook.
        cmd_report(sys.argv[2] if len(sys.argv) > 2 else os.getcwd())
    elif mode == "reap-now":
        cmd_reap({"cwd": sys.argv[2] if len(sys.argv) > 2 else os.getcwd()}, now=True)
    else:
        cmd_reap(read_payload())
    sys.exit(0)


if __name__ == "__main__":
    main()

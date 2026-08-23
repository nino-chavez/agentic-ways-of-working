#!/usr/bin/env python3
"""Connector reaper — audits and reaps orphaned MCP connector processes.

Sibling of worktree-reaper.py (cleanup-side counterpart to worktree-guard.py,
for stale worktrees) and read-guard.py (token-cost guard on Read). This one
targets the doctrine bullet "Connectors are processes, and nothing reaps
them" in principles/working-style.md § Harness hygiene: every stdio MCP
server is a local process tree the client spawns and is supposed to clean up,
and nothing enforces that it does.

Measured 2026-08-23 07:23 CDT: 27 MCP connector processes, 1.14 GiB RSS,
uptime up to 24h22m. Twelve chrome-devtools-mcp trees, five
@upstash/context7-mcp, five @playwright/mcp. Every one `enabled: false` in
config — they had started before the disable and nothing ended them. But
ownership mattered: 20+ were children of four LIVE, attached Claude CLI
sessions (open terminal tabs) and two of a live Codex app-server. A prior
cleanup agent correctly refused to touch them.

So, unlike worktree-reaper (whose primary action is artifact reclamation),
this tool's primary action is REPORTING. Reaping is deliberately narrow: a
connector process is reaped only when it is provably orphaned, i.e. its
owning client is gone — not merely idle, not merely disabled in config.

A connector qualifies for reaping when ALL hold:
  - its command matches a known connector pattern (CONNECTOR_PATTERNS)
  - walking its ancestor chain never crosses a live client process (claude,
    codex, ChatGPT, node_repl) before reaching pid 1 (reparented to init)
  - it has stayed in that orphaned state longer than GRACE_SECONDS, so a
    client mid-restart is not raced
  - --dry-run is not set

On ANY uncertainty — ps fails, an ancestor can't be resolved, a pid vanishes
mid-walk — the process is reported "unknown" and never killed. Fails closed,
same posture as worktree-reaper's idleness gate.

Modes:
  report        — default when run by hand. Prints connector process trees
                   grouped by owning client (or ORPHANED / UNKNOWN buckets),
                   plus the ratio the doctrine cares about: connector process
                   GROUPS against LOADED CLIENT SESSIONS (a ratio above 1:1
                   is duplicate-spawn — one config line is not one process).
                   Never kills. Does mutate the orphan-tracking state file
                   (see below) — that is bookkeeping, not a destructive
                   action, so it stays consistent with "never deletes".
  --reap        — hook mode: throttled, silent (log only), does the narrow
                   reaping described above.
  --reap --dry-run — names what --reap would kill (prints AND logs) and
                   exits without killing. Not throttled — it is a preview,
                   not a destructive pass, same reasoning as `report`.

Deviations from worktree-reaper.py, the structural model for this file:

  - No per-repo scope. worktree-reaper keys everything off a git common-dir;
    connectors belong to the whole machine/session, not a repo. The off
    switch, throttle stamp, and orphan-tracking state therefore live under a
    single global cache dir (~/.claude/cache/connector-reaper/), matching
    read-guard.py's convention rather than worktree-reaper's per-repo lock
    dir.
  - No stdin payload. worktree-reaper reads {"cwd": ...} from stdin because
    its unit of work is "the repo this hook fired in". This tool's unit of
    work is "every connector process on the machine right now", so it never
    reads stdin at all (a hook harness may still pipe a payload in; it is
    simply ignored).
  - Grace period needs cross-invocation state, which worktree-reaper's
    idleness gate does not: a build dir's mtime already encodes "how long
    has this been idle". A process's own uptime (`ps -o etime`) does NOT
    encode "how long has this been orphaned" — it could have lived for hours
    under a live client and only just been orphaned. So this tool persists a
    first-seen-orphaned timestamp per process instance (orphans.json,
    ~/.claude/cache/connector-reaper/) and reaps only once
    now - first_seen >= GRACE_SECONDS. Keyed by pid + a hash of the full
    command line, not pid alone: pid reuse is real, and keying on pid alone
    would let a stale timestamp attach to an unrelated process that happens
    to reuse the pid, making it eligible for reaping the instant it is seen.
    Keying on command hash means a different program on a reused pid always
    gets a fresh clock.

Secret-safety: connector command lines carry credentials in argv — the real
observed case was `npm exec @upstash/context7-mcp --api-key ctx7sk-<live
key>`, readable by any process on the machine via `ps`. Every command line
this tool prints or logs is passed through redact() first: known credential
flags (--api-key/--token/--secret/--password/--bearer) and known provider
token shapes (sk-ant-, sk-proj-, ctx7sk-, ghp_, xox*) are masked to at most
their first 6 characters + "***", regardless of whether a flag preceded
them. There is a test asserting a real key is never emitted, in either the
report/dry-run stdout path or the log file.

Off switch: CONNECTOR_REAPER_OFF=1, or touch the .guard-off file this module
reports the path to. Throttle: per-repo precedent from worktree-reaper.py,
because SessionEnd can fire often — a stamp file gates real --reap passes to
once per THROTTLE_HOURS; report and --dry-run always run (nothing to guard).

Pure stdlib. Log: ~/.claude/logs/connector-reaper.log
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

# --- tunables (env-overridable) ----------------------------------------------

# How long a connector must have been provably orphaned before it is reaped.
# Exists so a client mid-restart (killed, about to relaunch its MCP servers)
# is never raced.
GRACE_SECONDS = int(os.environ.get("CONNECTOR_REAPER_GRACE_SECONDS", "300"))

# Mirrors worktree-reaper.py's THROTTLE_HOURS: SessionEnd can fire often, and
# an unthrottled reap pass would re-walk every process on every session end.
THROTTLE_HOURS = int(os.environ.get("CONNECTOR_REAPER_THROTTLE_HOURS", "6"))

PS_TIMEOUT = int(os.environ.get("CONNECTOR_REAPER_PS_TIMEOUT", "10"))

# Machine-wide, not per-repo — connectors are not scoped to a git checkout.
# Mirrors read-guard.py's cache-dir convention rather than worktree-reaper's
# per-repo <git-common-dir>/.claude-sessions lock dir.
CACHE_DIR = Path(
    os.environ.get(
        "CONNECTOR_REAPER_CACHE_DIR",
        str(Path.home() / ".claude" / "cache" / "connector-reaper"),
    )
)
OVERRIDE_FILE = CACHE_DIR / ".guard-off"
STAMP_PATH = CACHE_DIR / ".last-reap"
STATE_PATH = CACHE_DIR / "orphans.json"

LOG_PATH = Path(
    os.environ.get(
        "CONNECTOR_REAPER_LOG",
        str(Path.home() / ".claude" / "logs" / "connector-reaper.log"),
    )
)

# One entry per line — this is where a new connector goes. Matched as a
# substring against the full argv, so `npm exec <pkg>` / `npx <pkg>` wrapper
# forms are caught automatically: the package name below is a substring of
# the wrapped command line either way. No separate "npm exec" pattern is
# needed or wanted — it would match every npm-run process on the machine.
CONNECTOR_PATTERNS = (
    "@upstash/context7-mcp",
    "@playwright/mcp",
    "chrome-devtools-mcp",
    "mcp-remote@",
    "@modelcontextprotocol/server-",
)

# Substrings (matched case-insensitively) identifying a live process as an
# agent client that may legitimately own a connector. Deliberately broad:
# a false positive here means "treated as live, never reaped", which is the
# safe direction. A false negative means killing something that still has an
# owner, which is not.
CLIENT_PATTERNS = (
    "claude",
    "codex",
    "chatgpt",
    "node_repl",
)

# Known flags that carry a credential as their next token, and known
# provider token-shape prefixes that get masked wherever they appear, flag
# or no flag. Add a provider prefix here when a new one is observed — this
# is the part of redaction that rots (mirrors the secret-scan pre-commit
# hook's own note about its provider-prefix list).
_CREDENTIAL_FLAG_RE = re.compile(
    r"(--api-key|--token|--secret|--password|--bearer)(=|\s+)(\S+)",
    re.IGNORECASE,
)
_CREDENTIAL_PREFIX_RE = re.compile(
    r"(sk-ant-|sk-proj-|ctx7sk-|ghp_|xox[a-z]-)[A-Za-z0-9_-]*",
)


# --- redaction -----------------------------------------------------------------

def _mask(value: str) -> str:
    """At most the first 6 characters of `value`, then '***'."""
    return f"{value[:6]}***"


def redact(command: str) -> str:
    """Never let a full credential reach stdout or the log file.

    Two independent passes: known flags (--api-key ...) catch credentials by
    position, known token-shape prefixes (ctx7sk-, sk-ant-, ...) catch them
    by shape regardless of what flag — if any — preceded them. Order doesn't
    matter for correctness (the flag pass already masks to <=6 chars, and
    the prefix regex's char class doesn't match '*' so it will not re-touch
    an already-masked value), but running the flag pass first keeps the
    common case's masked output anchored to the flag name.
    """
    def _flag_sub(m: re.Match) -> str:
        flag, sep, value = m.group(1), m.group(2), m.group(3)
        return f"{flag}{sep}{_mask(value)}"

    out = _CREDENTIAL_FLAG_RE.sub(_flag_sub, command)
    out = _CREDENTIAL_PREFIX_RE.sub(lambda m: _mask(m.group(0)), out)
    return out


# --- process snapshot ----------------------------------------------------------

def parse_etime(raw: str) -> float:
    """Parse ps's `etime` field: [[dd-]hh:]mm:ss -> seconds."""
    days = 0
    s = raw.strip()
    if "-" in s:
        d, s = s.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            days = 0
    parts = s.split(":")
    try:
        nums = [int(p) for p in parts]
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


def snapshot_processes(timeout: int = PS_TIMEOUT) -> dict[int, dict] | None:
    """pid -> {ppid, stat, etime, rss_kb, tty, command}, or None on failure.

    None (not {}) on any ps failure — collapsing "could not enumerate" into
    "no processes" would let a transient ps failure be read as "nothing to
    reap", the same bug class worktree-reaper.py's all_worktrees() names.
    """
    try:
        r = subprocess.run(
            ["ps", "-Aww", "-o", "pid=,ppid=,stat=,etime=,rss=,tty=,command="],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    procs: dict[int, dict] = {}
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        pid_s, ppid_s, stat, etime_s, rss_s, tty, command = parts
        try:
            pid = int(pid_s)
            ppid = int(ppid_s)
            rss_kb = int(rss_s)
        except ValueError:
            continue
        procs[pid] = {
            "ppid": ppid,
            "stat": stat,
            "etime": parse_etime(etime_s),
            "rss_kb": rss_kb,
            "tty": tty,
            "command": command,
        }
    return procs


# --- classification --------------------------------------------------------

def _is_connector(command: str) -> bool:
    return any(p in command for p in CONNECTOR_PATTERNS)


def _matched_pattern(command: str) -> str | None:
    for p in CONNECTOR_PATTERNS:
        if p in command:
            return p
    return None


def _is_client(command: str) -> bool:
    lowered = command.lower()
    return any(p in lowered for p in CLIENT_PATTERNS)


def ancestor_owner(
    pid: int, procs: dict[int, dict], max_depth: int = 64
) -> tuple[str, int | None, str | None]:
    """Walk the ancestor chain of `pid`.

    Returns (status, owner_pid, owner_command):
      "live_client" — an ancestor (possibly `pid` itself) matches a known
                       client pattern and is present in this snapshot.
      "orphan"      — the chain reaches pid 1 (init) without a client match.
      "unknown"     — the chain could not be resolved: a pid in it is
                       missing from the snapshot (vanished mid-walk, or the
                       walk cycled), or it exceeded max_depth. Never a basis
                       for reaping.
    """
    seen: set[int] = set()
    cur = pid
    for _ in range(max_depth):
        if cur in seen:
            return "unknown", None, None
        seen.add(cur)
        info = procs.get(cur)
        if info is None:
            return "unknown", None, None
        if _is_client(info["command"]):
            return "live_client", cur, info["command"]
        if cur == 1:
            return "orphan", None, None
        cur = info["ppid"]
    return "unknown", None, None


def orphan_key(pid: int, info: dict) -> str:
    """Stable key for grace-period tracking: pid + a hash of its full argv.

    Not pid alone. pid reuse is real on a long-running machine, and keying
    on pid alone would let a stale first-seen timestamp attach to a
    completely different process that happens to land on the same pid,
    making it instantly eligible for reaping. A different command on a
    reused pid always gets a fresh clock under this key.
    """
    h = hashlib.sha256(info["command"].encode("utf-8", "replace")).hexdigest()[:12]
    return f"{pid}:{h}"


# --- state / log / throttle -------------------------------------------------

def load_orphan_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_orphan_state(state: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def log(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {msg}\n")
    except Exception:
        pass


def emit(msg: str, also_print: bool) -> None:
    log(msg)
    if also_print:
        print(msg)


def throttled() -> bool:
    try:
        return time.time() - STAMP_PATH.stat().st_mtime < THROTTLE_HOURS * 3600
    except Exception:
        return False


def touch_stamp() -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        STAMP_PATH.write_text(str(int(time.time())), encoding="utf-8")
    except Exception:
        pass


def kill_pid(pid: int) -> bool:
    """SIGTERM, not SIGKILL: a graceful stop gives the connector a chance to
    flush/close cleanly. Reports success if the process is already gone."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return True
    except Exception:
        return False


# --- formatting --------------------------------------------------------------

def fmt_secs(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# --- report --------------------------------------------------------------------

def cmd_report() -> None:
    procs = snapshot_processes()
    if procs is None:
        print("could not enumerate processes (ps failed)")
        sys.exit(1)

    now = time.time()
    state = load_orphan_state()
    new_state: dict = {}

    # owner_pid -> list of connector rows; "__orphan__" / "__unknown__" for
    # the two buckets with no live owner.
    groups: dict[object, list[dict]] = {}
    total_rss_kb = 0
    n_live = n_orphan = n_unknown = 0
    live_client_pids: set[int] = set()
    root_group_count = 0  # connector trees whose direct parent isn't itself
                           # a connector — the doctrine's "process group"

    for pid in sorted(procs):
        info = procs[pid]
        cmd = info["command"]
        if not _is_connector(cmd):
            continue
        status, owner_pid, _owner_cmd = ancestor_owner(pid, procs)
        total_rss_kb += info["rss_kb"]
        parent_cmd = procs.get(info["ppid"], {}).get("command", "")
        is_root = not _is_connector(parent_cmd)

        row = {
            "pid": pid,
            "pattern": _matched_pattern(cmd) or "?",
            "etime": info["etime"],
            "rss_kb": info["rss_kb"],
            "command": redact(cmd),
        }

        if status == "live_client":
            n_live += 1
            live_client_pids.add(owner_pid)
            if is_root:
                root_group_count += 1
            groups.setdefault(owner_pid, []).append(row)
        elif status == "orphan":
            n_orphan += 1
            key = orphan_key(pid, info)
            first_seen = state.get(key, now)
            new_state[key] = first_seen
            row["orphan_age_s"] = now - first_seen
            row["eligible"] = (now - first_seen) >= GRACE_SECONDS
            groups.setdefault("__orphan__", []).append(row)
        else:
            n_unknown += 1
            groups.setdefault("__unknown__", []).append(row)

    save_orphan_state(new_state)

    n_total = n_live + n_orphan + n_unknown
    print(f"connector processes: {n_total}   total RSS: {total_rss_kb / 1024:.0f} MiB")
    print(f"owned by live clients: {n_live}   orphaned: {n_orphan}   unknown: {n_unknown}")
    if throttled():
        print("NOTE: reap is currently throttled — a live --reap would skip this pass.")
    if live_client_pids:
        ratio = root_group_count / len(live_client_pids)
        flag = "  <- duplicate-spawn" if ratio > 1.0 else ""
        print(
            f"connector groups per loaded client session: "
            f"{root_group_count}/{len(live_client_pids)} = {ratio:.1f}:1{flag}"
        )
    print()

    if n_total == 0:
        print("(no connector processes found)")
        return

    # Live-owned groups first (most actionable-by-inspection), then orphans,
    # then unknowns.
    ordered_keys = [k for k in groups if k not in ("__orphan__", "__unknown__")]
    ordered_keys.sort(key=lambda k: -len(groups[k]))
    for special in ("__orphan__", "__unknown__"):
        if special in groups:
            ordered_keys.append(special)

    for key in ordered_keys:
        items = groups[key]
        if key == "__orphan__":
            print(f"ORPHANED (owner process gone) — {len(items)} connector(s)")
        elif key == "__unknown__":
            print(f"UNKNOWN (ancestor unresolved) — {len(items)} connector(s)")
        else:
            oc = procs.get(key, {})
            print(
                f"client pid={key} tty={oc.get('tty', '?')} "
                f"uptime={fmt_secs(oc.get('etime', 0))} "
                f"cmd={truncate(oc.get('command', ''), 60)}"
            )
        for it in items:
            extra = ""
            if "orphan_age_s" in it:
                extra = (
                    f" orphan_age={fmt_secs(it['orphan_age_s'])} "
                    f"eligible={it['eligible']}"
                )
            print(
                f"    {it['pattern']:<30} pid={it['pid']} "
                f"uptime={fmt_secs(it['etime'])} rss={it['rss_kb'] // 1024}MiB{extra}"
            )
            print(f"      {it['command']}")


# --- reap ------------------------------------------------------------------

def cmd_reap(dry_run: bool) -> None:
    if os.environ.get("CONNECTOR_REAPER_OFF") == "1":
        return
    if OVERRIDE_FILE.exists():
        return
    if not dry_run and throttled():
        return

    procs = snapshot_processes()
    if procs is None:
        log("ps enumeration failed; skipping this pass")
        return

    now = time.time()
    state = load_orphan_state()
    new_state: dict = {}
    n_live = n_orphan = n_unknown = n_killed = n_kill_failed = 0

    for pid in sorted(procs):
        info = procs[pid]
        cmd = info["command"]
        if not _is_connector(cmd):
            continue
        status, owner_pid, _owner_cmd = ancestor_owner(pid, procs)
        redacted = redact(cmd)
        pattern = _matched_pattern(cmd) or "?"

        if status == "live_client":
            n_live += 1
            emit(
                f"skip pid={pid} pattern={pattern} reason=owned_by_live_client "
                f"owner_pid={owner_pid} cmd={redacted}",
                dry_run,
            )
            continue

        if status == "unknown":
            n_unknown += 1
            emit(
                f"skip pid={pid} pattern={pattern} reason=unknown_ancestor "
                f"cmd={redacted}",
                dry_run,
            )
            continue

        # status == "orphan"
        n_orphan += 1
        key = orphan_key(pid, info)
        first_seen = state.get(key, now)
        new_state[key] = first_seen
        age = now - first_seen

        if age < GRACE_SECONDS:
            emit(
                f"skip pid={pid} pattern={pattern} reason=grace_period "
                f"age={age:.0f}s<{GRACE_SECONDS}s cmd={redacted}",
                dry_run,
            )
            continue

        if dry_run:
            emit(
                f"would_kill pid={pid} pattern={pattern} "
                f"reason=orphan_grace_elapsed age={age:.0f}s "
                f"rss_kb={info['rss_kb']} cmd={redacted}",
                dry_run,
            )
            continue

        if kill_pid(pid):
            n_killed += 1
            log(
                f"killed pid={pid} pattern={pattern} age={age:.0f}s "
                f"rss_kb={info['rss_kb']} etime={info['etime']:.0f}s cmd={redacted}"
            )
        else:
            n_kill_failed += 1
            log(f"kill_failed pid={pid} pattern={pattern} cmd={redacted}")

    save_orphan_state(new_state)
    if not dry_run:
        touch_stamp()

    emit(
        f"summary connectors_seen={n_live + n_orphan + n_unknown} live={n_live} "
        f"orphan={n_orphan} unknown={n_unknown} killed={n_killed} "
        f"kill_failed={n_kill_failed} dry_run={dry_run}",
        dry_run,
    )


def main() -> None:
    argv = sys.argv[1:]
    dry_run = "--dry-run" in argv
    if "--reap" in argv:
        cmd_reap(dry_run=dry_run)
    else:
        cmd_report()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Process-argv guard — deny shell commands that print other processes' argv.

Local dev servers take secrets as arguments: `wrangler pages dev ... --binding
ANTHROPIC_API_KEY=...`. Any local user can read a process's argv, which is
a local exposure. A command that prints it into a session turns that into a
stored and shipped one: the transcript is persisted, ingested by Recall,
indexed, and archived to R2.

Measured 2026-10-07: two sessions about an hour apart each ran `pgrep -fl
...` during closeout and printed roughly seven live credentials from another
worktree's wrangler servers. A prose rule (a feedback memory) existed before
the second leak and did not prevent it, because that session had loaded its
memory index before the rule was written. A prose rule cannot reach a session
that is already running; a hook can.

What is denied, per pipeline:
  pgrep  with -l / -a in any short-option cluster (-fl, -lf, -la), --list-full,
         --list-name.  Bare `pgrep -f` prints pids only and passes.
  ps     unless its columns are restricted to ones without arguments: `-o` /
         `-O` columns that avoid args/command/cmd, or macOS `-c` (executable
         name only). An allowlist, not a flag blocklist: plain `ps -p <pid>`
         prints the full command line on macOS (verified 2026-10-07), and no
         list of bad flags catches that. Environment display (`e` / `-E`) is
         always denied.
  /proc/<pid>/cmdline or /environ read by any command or redirection.
  pstree and procs, which print full command lines by default (neither is
         installed here as of 2026-10-07; one brew install would open the hole).
A later stage in the same pipeline that is the redactor (`sed` with
REDACTED), `wc`, or `grep -c`/`-q` makes the pipeline safe.

Parsing, not substring matching: commands are split with shlex after heredoc
bodies are dropped, so a commit message, an echo, or a heredoc documenting the
rule passes (browse-profile-guard learned this on 2026-08-27). Wrappers are
stripped (sudo, env, xargs, timeout, watch, VAR=...), and `sh|bash|zsh -c`,
`eval`, `$(...)` and backticks are analysed as commands of their own. Codex's
code-mode `exec` tool (`tools.exec_command({cmd: ...})`) is read too; the
Codex adapter runs this file with `check`.

This is a guardrail against the reflexive check, not a sandbox: a python
heredoc calling subprocess, `ssh host ps aux`, or a deliberately obfuscated
command gets through.

Always deny, never deny-once: the measured failure is an agent that had the
rule in context and ran the command anyway, so the retry must not pass. The
way through is a safe form or the redactor.

Fail-open: unparseable payload, non-dict shapes, any exception => exit 0.
Escape hatch:
  touch ~/.claude/cache/process-argv-guard/.guard-off
"""
from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

OVERRIDE_FILE = Path.home() / ".claude" / "cache" / "process-argv-guard" / ".guard-off"

REDACTOR = (r"sed -E 's/(--binding [A-Z_]+=)[^ ]+/\1REDACTED/g; "
            r"s/(KEY|TOKEN|SECRET)=[^ ]+/\1=REDACTED/g'")

MAX_DEPTH = 4
OPS = ";&|()<>\n"
HEREDOC = re.compile(r"(<<-?\s*['\"]?(\w+)['\"]?)[^\n]*\n.*?\n\s*\2\b", re.S)
SUBST = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
SINGLE_QUOTED = re.compile(r"'[^']*'")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PROC_PATH = re.compile(r"^<?/proc/[^/\s]+/(cmdline|environ)$")

# Wrappers whose remaining words are the real command, with the options that
# take a separate value (so the value is not read as the command word).
WRAPPERS = {
    "sudo": {"-u", "-g", "-C", "-h", "-p", "-U", "-r", "-t"},
    "env": {"-u", "-P", "-S", "-C"},
    "xargs": {"-I", "-J", "-n", "-L", "-P", "-s", "-E", "-R", "-S", "-d", "-a"},
    "watch": {"-n", "-d"},
    "nice": {"-n"},
    "stdbuf": {"-i", "-o", "-e"},
    "command": set(), "exec": set(), "nohup": set(), "time": set(),
    "caffeinate": set(), "q": set(), "builtin": set(),
}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
SHELL_KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "!", "{", "}"}

ARG_COLUMNS = {"args", "command", "cmd"}
# ps short options that take a value (macOS and procps); the value may be attached.
PS_VALUE_OPTS = set("GgMNOopqtUu")
# pgrep short options that take a value.
PGREP_VALUE_OPTS = set("dFGgJMNPstUu")

# Codex code mode: `tools.exec_command({cmd:"..."})` inside an `exec` call.
JS_CMD = re.compile(r"""(?:\bcmd|["']cmd["'])\s*:\s*(["'`])((?:\\.|(?!\1).)*)\1""", re.S)
JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\n": ""}


def base(word: str) -> str:
    return word.rsplit("/", 1)[-1]


# --- parsing ---------------------------------------------------------------

def pipelines(command: str) -> list[list[list[str]]]:
    """Pipelines -> stages -> words. Redirection operators and their targets are
    dropped, except that an input redirection target stays as a `<path` word so
    a /proc read through `<` is still seen."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=OPS)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    toks = list(lex)  # ValueError on an unbalanced quote

    out: list[list[list[str]]] = []
    stages: list[list[str]] = []
    cur: list[str] = []
    in_comment = False
    i = 0
    while i < len(toks):
        t = toks[i]
        is_op = bool(t) and all(c in OPS for c in t)
        if in_comment:
            if is_op and "\n" in t:
                in_comment = False
            else:
                i += 1
                continue
        if not is_op:
            if t.startswith("#"):
                in_comment = True
            else:
                cur.append(t)
            i += 1
            continue
        if "<" in t or ">" in t:
            target = toks[i + 1] if i + 1 < len(toks) else ""
            if "<" in t and target:
                cur.append("<" + target)
            i += 2
            continue
        # A line break after `|` continues the pipeline. shlex merges an operator
        # run, so it arrives as "|\n", or as a lone "\n" when a space separates them.
        if t.strip("\n") == "" and i > 0 and toks[i - 1].replace("\n", "") in ("|", "|&"):
            i += 1
            continue
        if t.replace("\n", "") in ("|", "|&"):
            stages.append(cur)
        else:
            stages.append(cur)
            out.append(stages)
            stages = []
        cur = []
        i += 1
    stages.append(cur)
    out.append(stages)
    return [[s for s in p if s] for p in out if any(p)]


def crude_pipelines(command: str) -> list[list[list[str]]]:
    """Fallback for a command shlex cannot parse (an apostrophe in a comment, an
    unbalanced quote): lines and `;`/`&&`/`||` split pipelines, `|` splits stages."""
    out = []
    for chunk in re.split(r"\n|;|&&|\|\|", command):
        stages = [s.split() for s in re.split(r"\|", chunk)]
        stages = [s for s in stages if s and not s[0].startswith("#")]
        if stages:
            out.append(stages)
    return out


def unwrap(words: list[str]) -> list[str]:
    while words:
        w = words[0]
        if ASSIGNMENT.match(w) or w in SHELL_KEYWORDS:
            words = words[1:]
            continue
        name = base(w)
        if name in WRAPPERS:
            takes_value = WRAPPERS[name]
            words = words[1:]
            while words and words[0].startswith("-") and words[0] != "-":
                opt = words[0]
                words = words[1:]
                if opt in takes_value and words:
                    words = words[1:]
            continue
        if name == "timeout":
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[1:]
            words = words[1:]  # the duration
            continue
        if name == "script":  # script [-flags] [file [command ...]]
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[1:]
            words = words[1:]  # the typescript file
            continue
        break
    return words


# --- per-command decisions --------------------------------------------------

def split_cols(spec: str) -> list[str]:
    return [c.split("=", 1)[0].lower() for c in re.split(r"[,\s]+", spec) if c]


def ps_leak(args: list[str]) -> str | None:
    cols: list[str] = []
    env = names_only = has_o = False
    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if a.startswith("--"):
            name, eq, val = a[2:].partition("=")
            if name in ("format", "o"):
                if not eq:
                    val, i = nxt, i + 1
                cols += split_cols(val)
                has_o = True
        elif a.startswith("-") and len(a) > 1:
            cluster = a[1:]
            for j, ch in enumerate(cluster):
                if ch == "E":
                    env = True
                elif ch == "c":
                    names_only = True
                elif ch in PS_VALUE_OPTS:
                    val = cluster[j + 1:]
                    if not val:
                        val, i = nxt, i + 1
                    if ch in "oO":
                        cols += split_cols(val)
                        has_o = has_o or ch == "o"
                        if ch == "O":
                            cols.append("<default>")
                    break
        elif re.fullmatch(r"[A-Za-z]+", a):  # BSD-style cluster: aux, eww, axo
            for j, ch in enumerate(a):
                if ch == "e":
                    env = True
                elif ch == "c":
                    names_only = True
                elif ch in "oO":
                    val = a[j + 1:]
                    if not val:
                        val, i = nxt, i + 1
                    cols += split_cols(val)
                    has_o = has_o or ch == "o"
                    if ch == "O":
                        cols.append("<default>")
                    break
        i += 1
    if env:
        return "ps showing process environments"
    if ARG_COLUMNS & set(cols):
        return "ps printing the args/command column"
    if names_only:
        return None
    if has_o and "<default>" not in cols:
        return None
    return "ps printing full command lines"


def pgrep_leak(args: list[str]) -> str | None:
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--list-full", "--list-name"):
            return f"pgrep {a}"
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            cluster = a[1:]
            for j, ch in enumerate(cluster):
                if ch in "la":
                    return f"pgrep -{ch} prints process names or arguments"
                if ch in PGREP_VALUE_OPTS:
                    if not cluster[j + 1:]:
                        i += 1
                    break
        i += 1
    return None


def sanitizes(words: list[str]) -> bool:
    words = unwrap(words)
    if not words:
        return False
    name = base(words[0])
    if name == "wc":
        return True
    if name == "sed":
        return any("REDACTED" in w for w in words[1:])
    if name in ("grep", "egrep", "rg"):
        for w in words[1:]:
            if w in ("--count", "--quiet", "--silent"):
                return True
            if w.startswith("-") and not w.startswith("--") and ("c" in w or "q" in w):
                return True
    return False


def stage_leak(words: list[str], depth: int) -> str | None:
    if any(PROC_PATH.match(w) for w in words):
        return "reading /proc/<pid>/cmdline or environ"
    words = unwrap(words)
    if not words:
        return None
    name = base(words[0])
    args = words[1:]
    if name == "ps":
        return ps_leak(args)
    if name == "pgrep":
        return pgrep_leak(args)
    if name in ("pstree", "procs"):  # both print full command lines by default
        return f"{name} printing full command lines"
    if name in SHELLS:
        for k, a in enumerate(args):
            if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
                if k + 1 < len(args):
                    return analyze(args[k + 1], depth + 1)
                return None
        return None
    if name == "eval":
        return analyze(" ".join(args), depth + 1)
    return None


def analyze(command: str, depth: int = 0) -> str | None:
    if depth > MAX_DEPTH or not command.strip():
        return None
    command = re.sub(r"\\\r?\n", " ", command)
    command = HEREDOC.sub(r"\1", command)

    # Command substitutions run even inside double quotes; single quotes are data.
    for m in SUBST.finditer(SINGLE_QUOTED.sub("''", command)):
        hit = analyze(m.group(1) or m.group(2) or "", depth + 1)
        if hit:
            return hit

    flat = command.replace("`", " ; ")
    try:
        parsed = pipelines(flat)
    except ValueError:
        stripped = re.sub(r"(?m)(^|\s)#.*$", r"\1", flat)
        try:
            parsed = pipelines(stripped)
        except ValueError:
            parsed = crude_pipelines(stripped)

    for stages in parsed:
        for k, words in enumerate(stages):
            hit = stage_leak(words, depth)
            if hit and not any(sanitizes(later) for later in stages[k + 1:]):
                return hit
    return None


def exec_commands(code: str) -> list[str]:
    cmds = []
    for _, body in JS_CMD.findall(code):
        cmds.append(re.sub(r"\\(.)", lambda m: JS_ESCAPES.get(m.group(1), m.group(1)),
                           body, flags=re.S))
    return cmds


# --- hook protocol ----------------------------------------------------------

def read_payload() -> dict:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def deny(headline: str) -> None:
    lines = [
        f"Process-argv guard: BLOCKED ({headline}).",
        "",
        "Local dev servers pass secrets as argv (wrangler pages dev --binding KEY=value).",
        "This output would land in the session transcript, which is persisted,",
        "ingested by Recall, and archived to R2. On 2026-10-07 `pgrep -fl` leaked",
        "about seven live credentials this way, in two sessions.",
        "",
        "Check for a process without printing its arguments:",
        "  pgrep -f <pattern>               pids only",
        "  pgrep -f <pattern> | wc -l       a count",
        "  ps -o pid=,comm= -p <pid>        pid and executable name",
        "  ps -axc                          every process, names only (macOS)",
        "",
        "If the arguments really must be seen, pipe them through the redactor as a",
        "later stage of the same pipeline:",
        f"  ... | {REDACTOR}",
        "It covers --binding and *KEY/TOKEN/SECRET= forms only; check what remains.",
        "",
        f"Override once confirmed safe: touch {OVERRIDE_FILE}",
    ]
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "\n".join(lines),
        }
    }))
    sys.exit(0)


def check(payload: dict) -> None:
    if OVERRIDE_FILE.exists():
        return
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    if tool == "Bash":
        command = tool_input.get("command")
        commands = [command] if isinstance(command, str) else []
    elif tool == "exec":
        code = tool_input.get("code") or tool_input.get("input") or tool_input.get("command")
        commands = exec_commands(code) if isinstance(code, str) else []
    else:
        return
    for command in commands:
        hit = analyze(command)
        if hit:
            deny(hit)


def main() -> None:
    # argv mode ("check") is accepted and ignored; the Codex adapter passes it.
    try:
        check(read_payload())
    except SystemExit:
        raise
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()

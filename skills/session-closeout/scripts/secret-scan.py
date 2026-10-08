#!/usr/bin/env python3
"""Scan an agent session's transcript for credentials before it is archived.

Why this exists: a closeout replay test (2026-10-07) found that whether a
credential printed into a transcript gets caught depends on the model paying
attention. Opus flagged a token that `pgrep -fl` had printed; two Haiku runs
on the same evidence never mentioned it. A check that rides on attention is a
habit. This makes it a command.

What it reports, never the value or any part of it:
  prefix  a known provider key shape (the pre-commit hook's PATTERNS list)
  assign  NAME=value where NAME looks secret and value looks like a token
  bearer  an Authorization: Bearer header with a token-shaped value
  live    the value of a secret-named variable in this process's environment
          appears verbatim; this one is certain, rotate it

Exit codes: 0 clean, 1 findings, 2 could not scan. Exit 2 is never "clean".

Usage:
  secret-scan.py --session-id <id>     Claude or Codex session id
  secret-scan.py --transcript <path>   a specific file
  secret-scan.py                       guess the newest session for this cwd
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# One owner for provider prefixes: the global pre-commit hook. Resolve through
# the symlinks (~/.claude/skills -> dotfiles -> vendor submodule) to the repo.
REPO = Path(__file__).resolve().parents[3]
PATTERN_SOURCE = REPO / "git-hooks" / "pre-commit-secret-scan"

# KEY only as a whole word at the end (API_KEY, SERVICE_ROLE_KEY), so KEYRING_SERVICE is not a secret.
SECRET_NAME = r"[A-Z][A-Z0-9_]*?(?:(?:TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*|_KEY|APIKEY)"
# Matched against decoded text, so [ \t]* and not \s*: a real newline must not
# carry an empty NAME= onto the next line's value. The optional quote after the name covers JSON
# config ("API_KEY": "value"); the one before the value covers export X="value".
ASSIGN = re.compile(rf"(?<![A-Za-z0-9_])({SECRET_NAME})[\"']?[ \t]*[=:][ \t]*[\"']?([^\s\"'\\,;)}}\]`]+)")
BEARER = re.compile(r"Bearer[ \t]+([A-Za-z0-9._~+/=-]+)")
# Client-side keys that are public by design (Supabase publishable and anon keys,
# Turnstile site keys, NEXT_PUBLIC_/PUBLIC_ variables), and names that announce a test fixture.
PUBLIC_NAME = re.compile(r"^(?:NEXT_|VITE_)?PUBLIC_|PUBLISHABLE|ANON_KEY$|SITE_KEY$|FAKE|DUMMY|EXAMPLE")
PLACEHOLDER = re.compile(r"redacted|example|placeholder|changeme|your[-_]|xxxx|\*\*\*", re.I)


def load_patterns() -> list[tuple[str, re.Pattern]]:
    text = PATTERN_SOURCE.read_text()
    block = re.search(r"^PATTERNS=\($(.*?)^\)$", text, re.S | re.M)
    if not block:
        raise ValueError(f"no PATTERNS=( ... ) block in {PATTERN_SOURCE}")
    out = []
    for line in block.group(1).splitlines():
        m = re.match(r"\s*'([^']+)'\s*(?:#\s*(.*))?$", line)
        if m:
            out.append(((m.group(2) or m.group(1)).strip(), re.compile(m.group(1))))
    if not out:
        raise ValueError(f"PATTERNS block in {PATTERN_SOURCE} yielded no patterns")
    return out


def token_shaped(value: str) -> bool:
    """A value that looks like a real credential, not a name, path or placeholder."""
    if len(value) < 16 or value[0] in "$<{%/~." or value.startswith(("op://", "http")):
        return False
    if re.fullmatch(r"[A-Z0-9_]+", value) or PLACEHOLDER.search(value):
        return False
    return bool(re.search(r"[0-9]", value) and re.search(r"[A-Za-z]", value)) and len(set(value)) >= 10


def live_values() -> list[tuple[str, str]]:
    name = re.compile(rf"^{SECRET_NAME}$")
    return [(k, v) for k, v in os.environ.items() if name.match(k) and len(v) >= 16]


def session_files(main: Path) -> tuple[list[Path], list[Path]]:
    """The transcript plus what Claude stores beside it: subagent transcripts and
    large tool outputs that were saved to disk with only a preview kept inline.
    Also returns folders that could not be listed; rglob would skip them silently."""
    files, unlisted = [main], []
    side = main.with_suffix("")
    if side.is_dir():
        for root, _dirs, names in os.walk(side, onerror=lambda e: unlisted.append(Path(e.filename))):
            files += sorted(Path(root) / n for n in names)
    return files, unlisted


def resolve(session_id: str | None, transcript: str | None, cwd: Path) -> tuple[Path, str]:
    home = Path.home()
    if transcript:
        return Path(transcript).expanduser(), "given"
    if session_id and not session_id.startswith("${"):
        hits = list((home / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
        hits += list((home / ".codex" / "sessions").rglob(f"rollout-*-{session_id}.jsonl"))
        if hits:
            return max(hits, key=lambda p: p.stat().st_mtime), "session id"
        raise FileNotFoundError(f"no transcript for session id {session_id}")
    # Guess: the newest Claude or Codex transcript whose cwd is this one.
    candidates = []
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    candidates += list((home / ".claude" / "projects" / slug).glob("*.jsonl"))
    for p in sorted((home / ".codex" / "sessions").rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime)[-50:]:
        try:
            with p.open() as fp:
                meta = json.loads(fp.readline())
        except (OSError, ValueError):
            continue
        payload = meta.get("payload") if isinstance(meta, dict) else None
        if isinstance(payload, dict) and payload.get("cwd") == str(cwd):
            candidates.append(p)
    if not candidates:
        raise FileNotFoundError(f"no transcript found for cwd {cwd}; pass --session-id or --transcript")
    return max(candidates, key=lambda p: p.stat().st_mtime), "guessed from cwd (newest); confirm it is this session"


def strings(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        # Keep the key beside a string value: a tool call's input object
        # {"API_KEY": "..."} has to read as API_KEY: ... for the assign rule.
        return [f"{k}: {v}" if isinstance(v, str) else s
                for k, v in node.items() for s in ([v] if isinstance(v, str) else strings(v))]
    if isinstance(node, list):
        return [s for item in node for s in strings(item)]
    return []


def decoded(line: str) -> str:
    """A transcript line as the text it holds. JSONL stores a newline as the two
    characters backslash-n and a quote as backslash-quote, which hides
    `cat .env` output and quoted assignments from a raw-line match."""
    try:
        return "\n".join(strings(json.loads(line)))
    except ValueError:
        return line


def scan(files: list[Path], main_file: Path) -> tuple[list[tuple[str, str, Path, list[int]]], list[Path]]:
    patterns = load_patterns()
    live = live_values()
    found: dict[tuple[str, str, Path], list[int]] = {}

    def add(kind: str, name: str, path: Path, n: int) -> None:
        lines = found.setdefault((kind, name, path), [])
        if not lines or lines[-1] != n:
            lines.append(n)

    skipped = []
    for path in files:
        try:
            fp = path.open(errors="replace")
        except OSError:
            if path == main_file:
                raise  # an unread transcript is an error, never clean
            skipped.append(path)
            continue
        with fp:
            for n, raw in enumerate(fp, 1):
                line = decoded(raw)
                for label, rx in patterns:
                    # Skip low-variety filler (a prefix padded with one repeated letter) and placeholders.
                    if any(len(set(m.group(0))) >= 10 and not PLACEHOLDER.search(m.group(0))
                           for m in rx.finditer(line)):
                        add("prefix", label, path, n)
                for m in ASSIGN.finditer(line):
                    if not PUBLIC_NAME.search(m.group(1)) and token_shaped(m.group(2)):
                        add("assign", m.group(1), path, n)
                for m in BEARER.finditer(line):
                    if token_shaped(m.group(1)):
                        add("bearer", "Authorization: Bearer", path, n)
                for name, value in live:
                    if value in line:
                        add("live", name, path, n)
    return [(k, nm, p, ln) for (k, nm, p), ln in found.items()], skipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--session-id")
    ap.add_argument("--transcript")
    args = ap.parse_args()
    try:
        main_file, how = resolve(args.session_id, args.transcript, Path.cwd())
        if not main_file.is_file():
            raise FileNotFoundError(f"transcript not found: {main_file}")
        files, unlisted = session_files(main_file)
        findings, skipped = scan(files, main_file)
        skipped += unlisted
    except (OSError, ValueError, re.error) as exc:
        print(f"SECRET_SCAN_ERROR: {exc}. This is not a clean result.")
        return 2
    print(f"Scanned: {main_file} ({how}; {len(files) - len(skipped)} file(s))")
    for p in skipped:
        print(f"  could not read {p}; it was not scanned")
    if not findings and skipped:
        print("SECRET_SCAN_ERROR: some files could not be read. This is not a clean result.")
        return 2
    if not findings:
        print("SECRET_SCAN_CLEAN: no credential-shaped values found.")
        return 0
    order = {"live": 0, "prefix": 1, "assign": 2, "bearer": 3}
    print(f"SECRET_SCAN_FINDINGS: {len(findings)}. Values are never printed.")
    for kind, name, path, lines in sorted(findings, key=lambda f: (order[f[0]], f[1])):
        where = path.name if path == main_file else str(path.relative_to(main_file.with_suffix("")))
        shown = ",".join(map(str, lines[:5])) + (f" (+{len(lines) - 5} more)" if len(lines) > 5 else "")
        print(f"  {kind:<6} {name}  {where} line {shown}")
    if any(f[0] == "live" for f in findings):
        print("A 'live' finding is a current credential printed into this transcript: rotate it.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

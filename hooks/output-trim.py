#!/usr/bin/env python3
"""Output trim — cut oversized Bash output before the model sees it.

Replay over ~/.claude/projects (2026-09-18, 191,100 tool results): Bash carried
29.5M of the 32.8M chars a head/tail trim would have removed — 90% of the
saving — across 4,736 outputs. Read is the larger pile (112.7M) and is
read-guard.py's job; this hook never touches it. A tool result is re-sent on
every later turn, so trimming at the PostToolUse boundary compounds the same
way read-guard's does.

Wired to PostToolUse (matcher "Bash"). Two tiers, stdout only:

  scrub — output over 1k chars: strip ANSI escapes, collapse blank-line runs,
          collapse 4+ identical consecutive lines to one + "[repeated Nx]".
          Lossless.
  elide — output over MAX_CHARS: keep the first HEAD_LINES and last
          TAIL_LINES, rescue error-looking lines from the cut, and write the
          original to a spill file named in the marker so the middle can be
          grepped back instead of re-running the command. Over ~30k chars
          Claude Code has already capped stdout and saved the full text
          (persistedOutputPath); the marker names that file instead.

stderr is never touched. Bash only, on purpose: Read/Edit/Write output feeds
exact-match edits, and an Agent or WebFetch result keeps its substance in the
middle, which is exactly what head+tail removes.

The replacement must match Bash's output shape ({stdout, stderr, interrupted,
isImage}) or Claude Code silently ignores it, so the original response object
is copied and only `stdout` is replaced.

Fail-open by design: unparseable payload, unexpected shape, spill I/O error,
or a result that did not get smaller => emit nothing, original output stands.
Escape hatch:
  touch ~/.claude/cache/output-trim/.guard-off     (or OUTPUT_TRIM_OFF=1)

Mechanism adapted from JayPokale/Chisle's chisle-compress-output.js (MIT):
allowlist-not-blocklist, shape-preserving rewrite, error salvage, spill.

Pure stdlib. Wire via settings.json:
  PostToolUse (matcher "Bash") -> python3 ~/.claude/hooks/output-trim.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

SCRUB_MIN_CHARS = 1000
MAX_CHARS = 8000
HEAD_LINES = 60
TAIL_LINES = 40
MAX_SALVAGED = 12
MAX_SALVAGE_LINE = 300
SPILL_KEEP = 40

STATE_DIR = Path(os.environ.get("OUTPUT_TRIM_STATE_DIR")
                 or Path.home() / ".claude" / "cache" / "output-trim")

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
# error/exception match as a suffix too: a leading \b misses TypeError,
# ValueError, NullPointerException — the most common shape of the thing.
SALVAGE_RE = re.compile(
    r"(error|exception)\b|\b(err!|fail(ed|ure|ing)?|traceback|panic|fatal|"
    r"denied|refused|timed?[ _-]?out|assert(ion)?|segfault|"
    r"undefined reference|cannot find)\b", re.I)


def scrub(text: str) -> str:
    out: list[str] = []
    blank = False
    prev, run = None, 0

    def flush() -> None:
        if prev is None:
            return
        out.append(prev)
        if run >= 4:
            out.append(f"[repeated {run}x]")
        else:
            out.extend([prev] * (run - 1))

    for line in ANSI_RE.sub("", text).split("\n"):
        if not line.strip():
            if prev is not None:
                flush()
                prev, run = None, 0
            if not blank:
                out.append("")
            blank = True
            continue
        blank = False
        if line == prev:
            run += 1
            continue
        flush()
        prev, run = line, 1
    flush()
    return "\n".join(out)


def spill(original: str, tool_use_id: str) -> str | None:
    try:
        d = STATE_DIR / "spill"
        d.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_-]", "", tool_use_id or "")[:64] or str(os.getpid())
        path = d / f"{name}.txt"
        path.write_text(original)
        old = sorted(d.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in old[SPILL_KEEP:]:
            p.unlink(missing_ok=True)
        return str(path)
    except OSError:
        return None


def elide(text: str, original: str, tool_use_id: str, persisted: str = "") -> str:
    lines = text.split("\n")
    if len(lines) <= HEAD_LINES + TAIL_LINES:
        return text
    middle = lines[HEAD_LINES:-TAIL_LINES]
    salvaged = [ln[:MAX_SALVAGE_LINE] for ln in middle if SALVAGE_RE.search(ln)]
    extra = len(salvaged) - MAX_SALVAGED
    salvaged = salvaged[:MAX_SALVAGED]
    # Over ~30k chars Claude Code caps stdout before this hook runs and saves
    # the real full output itself; our copy would be the capped text, so point
    # at theirs instead of spilling a second, shorter one.
    path = persisted or spill(original, tool_use_id)
    marker = [f"[output-trim: {len(middle)} lines elided"
              + (f"; full output: {path}" if path else "") + "]"]
    if salvaged:
        marker.append("[error-like lines from the elided part:]")
        marker.extend(salvaged)
        if extra > 0:
            marker.append(f"[+{extra} more error-like lines in the full output]")
    return "\n".join(lines[:HEAD_LINES] + marker + lines[-TAIL_LINES:])


def transform(stdout: str, tool_use_id: str = "", persisted: str = "") -> str | None:
    """Trimmed stdout, or None to leave the original alone."""
    if len(stdout) <= SCRUB_MIN_CHARS:
        return None
    out = scrub(stdout)
    if len(out) > MAX_CHARS:
        out = elide(out, stdout, tool_use_id, persisted)
    return out if len(out) < len(stdout) else None


def main() -> int:
    if os.environ.get("OUTPUT_TRIM_OFF") == "1" or (STATE_DIR / ".guard-off").exists():
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        response = payload.get("tool_response")
        if payload.get("tool_name") != "Bash" or not isinstance(response, dict):
            return 0
        stdout = response.get("stdout")
        if not isinstance(stdout, str) or response.get("isImage"):
            return 0
        persisted = response.get("persistedOutputPath")
        trimmed = transform(stdout, str(payload.get("tool_use_id") or ""),
                            persisted if isinstance(persisted, str) else "")
        if trimmed is None:
            return 0
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": {**response, "stdout": trimmed},
        }}))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

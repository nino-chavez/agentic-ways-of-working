#!/usr/bin/env python3
"""Stop hook — catches end-of-turn hesitation in the assistant response.

Public, Poe-free variant. The live hook is
~/.dotfiles/files/home/.claude/hooks/anti-hesitation.py (private); it depends on the
Poe classifier and owns the detection logic. Changes land there first.

Reads the session transcript, finds the last assistant message, and blocks the
stop with a corrective reason if the closing reads as a permission-seeking
question — exactly the pattern the "decision bias" principle prohibits.

It also blocks a second shape of the same stop: a closing that hands the user a
shell command to run when nothing refused that command and the closing gives no
reason it is theirs to run (see classify_handoff). At most one block per user
turn: a rewrite that still trips a check is let through rather than looped.

Layered defense (the principle is durable; this hook is the enforcement):
  1. working-style.md '## Decision bias' section  — declarative rule
  2. THIS Stop hook                               — corrective enforcement post-response

This is the STANDALONE version: it carries its own classifier and has no
external dependencies, so it works in any environment. (The author's private
setup swaps in a corpus-trained classifier shared with a predictive
UserPromptSubmit layer; that coupling is intentionally stripped here.)

Hook input (stdin): {"transcript_path": "...", ...}
Hook output (stdout): JSON {"decision": "block", "reason": ...} to force another
turn, or nothing to allow the stop.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Hesitation closers: a question in the LAST stretch of the turn that asks
# permission to continue work already in scope. These are the shapes that read
# as timidity rather than care. Matched case-insensitively against the tail.
_HESITATION_PATTERNS = [
    r"\bwant me to\b[^?]*\?",
    r"\bwould you like me to\b[^?]*\?",
    r"\bshould i\b[^?]*\?",
    r"\bshall i\b[^?]*\?",
    r"\bdo you want me to\b[^?]*\?",
    r"\blet me know (?:if|whether)\b[^?.]*(?:\?|\.)",
    r"\bshould i (?:keep going|continue|proceed|stop)\b[^?]*\?",
    r"\b(?:keep going|continue) or (?:stop|pause)\b[^?]*\?",
    r"\bwant me to (?:keep going|continue|proceed)\b[^?]*\?",
    r"\bproceed\b[^?]*\?",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in _HESITATION_PATTERNS]

# Only inspect the tail of the message — a clarifying question mid-turn is fine;
# a permission-seeking question as the CLOSER is the violation.
_TAIL_CHARS = 600


def _tail(text: str) -> str:
    stripped = text.rstrip()
    return stripped[-_TAIL_CHARS:]


def classify_hesitation(text: str) -> str | None:
    """Return the matched closer if the turn ends on hesitation, else None."""
    tail = _tail(text)
    # The closer must actually end with a question mark — otherwise the trailing
    # text is a status sentence, not a request for permission.
    if not tail.rstrip().endswith("?"):
        return None
    for pat in _COMPILED:
        m = pat.search(tail)
        if m:
            return m.group(0)
    return None


# --- command hand-off ---------------------------------------------------------
# A turn that ends by giving the user a shell command to run, when nothing
# refused that command, is the same stop as a permission question with different
# wording. A hand-off after a refusal, or one that says why only the user can
# run it, is legitimate and is never blocked.
_SHELL_FENCE = re.compile(r"```(?:bash|sh|zsh|shell)[ \t]*\n(.+?)```", re.DOTALL)
_HANDOFF_WORDING = re.compile(
    r"\brun (?:this|these|it|them|that|both) (?:yourself|in your|from your)"
    r"|\brun the (?:command|commands|two|three|following)\b[^.\n]{0,24}(?:yourself|in your|from your)"
    r"|\byou(?:'ll| will)? (?:need to |have to )?run\b"
    r"|\byour own (?:shell|terminal)\b|\bfrom your (?:shell|terminal)\b"
    r"|\b(?:reply|tell me|say) [^.\n]{0,40}\b(?:ran|run|done)\b",
    re.IGNORECASE,
)
# The closing explains why the command is the user's to run: a refusal it met,
# a sign-in or password, something physical, a gate that stays with the user
# (production, publishing, sending, deploying), or a command for another
# machine. The list errs wide on purpose: a miss here makes the hook tell an
# agent to run a command it was right to hand over.
_HANDOFF_REASON_GIVEN = re.compile(
    r"refus|denied|declin|block|guard\b|classifier|permission|sandbox|not permitted"
    r"|sign[- ]?in|log[- ]?in|password|passcode|Touch ID|one-time code"
    r"|\b2FA\b|passkey|\bsudo\b|interactive|microphone|\brecord(?:ing)?\b"
    r"|production|irreversibl|destructive|cannot be undone|only you\b|your call"
    r"|publish|\bpost(?:s|ed|ing)?\b|\bsend(?:s|ing)?\b|\bsent\b|deploy"
    r"|\b(?:other|work|personal) (?:Mac|machine)\b",
    re.IGNORECASE,
)
# A tool result that shows the command, or one like it, was refused.
_REFUSAL = re.compile(
    r"hook error|BLOCKED|auto mode classifier|permission[^.\n]{0,30}denied"
    r"|doesn't want to proceed|was rejected|Operation not permitted|sandbox",
    re.IGNORECASE,
)
# How many user prompts back a refusal still explains a hand-off. Two: the
# refusal often lands one turn before the turn that restates the command.
_HANDOFF_LOOKBACK_PROMPTS = 2
# Marker the harness writes into the transcript when a Stop hook blocks.
_FEEDBACK_MARKER = "Stop hook feedback"


def classify_handoff(text: str, refusals: list[str]) -> str | None:
    """The first handed-over command if this closing is an unexplained hand-off.

    Returns None unless every one of these holds: the closing has a shell
    block, it tells the user to run it, it gives no reason the command is
    theirs to run, and no tool call in the recent turns was refused.
    """
    blocks = [b.strip() for b in _SHELL_FENCE.findall(text) if b.strip()]
    if not blocks or not _HANDOFF_WORDING.search(text):
        return None
    if _HANDOFF_REASON_GIVEN.search(text):
        return None
    if any(_REFUSAL.search(r) for r in refusals):
        return None
    return blocks[0].splitlines()[0][:160]


def _line_text(obj: dict) -> str:
    """The plain text of a transcript line; '' for a tool-result-only line."""
    content = obj.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _is_prompt(obj: dict, origins_marked: bool = True) -> bool:
    """A text-bearing user line the user typed, not one the harness wrote.

    Current transcripts mark a typed prompt with origin.kind "human". Skill
    bodies, slash-command echoes, task notifications, peer messages and hook
    feedback are user lines too, with another origin or none; counting them as
    prompts would end the lookback early and hide a refusal. When a transcript
    marks no line at all (older clients), any non-meta text line counts.
    """
    if obj.get("type") != "user" or obj.get("isMeta"):
        return False
    text = _line_text(obj).strip()
    if not text or _FEEDBACK_MARKER in text:
        return False
    origin = obj.get("origin")
    if isinstance(origin, dict):
        return origin.get("kind") == "human"
    return not origins_marked


def _lines(transcript_path: Path) -> list[str]:
    try:
        return transcript_path.read_text(errors="replace").splitlines()
    except Exception:
        return []


def recent_refusals(transcript_path: Path) -> list[str]:
    """Error tool results since the last _HANDOFF_LOOKBACK_PROMPTS prompts."""
    lines = _lines(transcript_path)
    found: list[str] = []
    prompts = 0
    origins_marked = any('"kind":"human"' in ln or '"kind": "human"' in ln for ln in lines)
    for line in reversed(lines):
        if '"is_error"' not in line and '"user"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "user":
            continue
        content = obj.get("message", {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    body = block.get("content")
                    found.append((body if isinstance(body, str) else json.dumps(body))[:800])
        if _is_prompt(obj, origins_marked):
            prompts += 1
            if prompts >= _HANDOFF_LOOKBACK_PROMPTS:
                break
    return found


def already_blocked_this_turn(transcript_path: Path) -> bool:
    """True if hook feedback appears after the last text-bearing user prompt.

    The stop being judged is then a rewrite of a turn this hook already
    blocked. Blocking it again is almost always a pattern problem, and one bad
    match otherwise burns turns in a block-rewrite-block loop.
    """
    for line in reversed(_lines(transcript_path)):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "user":
            continue
        text = _line_text(obj).strip()
        if not text:
            continue
        return _FEEDBACK_MARKER in text
    return False


def last_assistant_text(transcript_path: Path) -> str | None:
    """Return the text content of the most recent assistant message."""
    if not transcript_path.exists():
        return None
    last_text = None
    with transcript_path.open() as fp:
        for line in fp:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") != "assistant":
                continue
            content = obj.get("message", {}).get("content")
            if isinstance(content, str):
                last_text = content
            elif isinstance(content, list):
                texts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                if texts:
                    last_text = "\n".join(texts)
    return last_text


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    transcript_path = payload.get("transcript_path")
    if not transcript_path:
        sys.exit(0)

    text = last_assistant_text(Path(transcript_path))
    if not text:
        sys.exit(0)

    # One block per turn. The harness sets stop_hook_active when this stop is
    # already the continuation of a blocked one; the transcript marker covers
    # clients that do not send it.
    rewrite = payload.get("stop_hook_active") is True or already_blocked_this_turn(Path(transcript_path))

    evidence = classify_hesitation(text)
    if not evidence:
        command = classify_handoff(text, recent_refusals(Path(transcript_path)))
        if not command or rewrite:
            sys.exit(0)
        print(json.dumps({"decision": "block", "reason": (
            "You ended your turn by handing the user a shell command to run, and "
            "nothing in this turn refused that command. That is the same stop as "
            "a permission question, which the 'decision bias' principle prohibits. "
            "If the command is safe and yours to run, run it and report what it "
            "printed. Do not run it if it publishes, sends, deploys, writes to "
            "production, needs a sign-in or password, or belongs on another "
            "machine: keep the hand-off and say that reason in the closing. If a "
            "hook or the permission layer refuses it, hand it over then and say "
            f"which one refused it. (Handed-over command: {command!r})"
        )}))
        sys.exit(0)

    if rewrite:
        sys.exit(0)

    reason = (
        "You ended your turn with a hesitation question — exactly the pattern "
        "the 'decision bias' principle prohibits. "
        "An explicit prior 'go' / 'continue' / 'proceed' covers the entire "
        "logical thread, not just one step. "
        "Restate your closing as a status sentence describing the next move "
        "you're taking. The user can interrupt; do not require their permission. "
        "Example: instead of 'Want me to do X next?' say 'Doing X next — flag if "
        "you want to stop.' "
        f"(Matched closer: {evidence!r})"
    )

    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


if __name__ == "__main__":
    main()

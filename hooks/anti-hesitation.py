#!/usr/bin/env python3
"""Stop hook — catches end-of-turn hesitation in the assistant response.

Reads the session transcript, finds the last assistant message, and blocks the
stop with a corrective reason if the closing reads as a permission-seeking
question — exactly the pattern the "decision bias" principle prohibits.

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

    evidence = classify_hesitation(text)
    if not evidence:
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

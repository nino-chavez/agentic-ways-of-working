#!/usr/bin/env python3
"""UserPromptSubmit hook — inject Campsite Mode directive when enabled.

Campsite Mode ("leave the campsite better than you found it") is a per-project,
toggleable working stance. When enabled (flag file present in the project's
.claude/), this hook injects the directive into context each turn so the
assistant holds north-star standards: no shims, fix pre-existing defects in
files it touches, no deferring discovered issues.

Why a UserPromptSubmit injection and not a Stop-block (like anti-hesitation):
the campsite signal is judgment-laden ("is this a shim? was that defect left
unfixed?"), not regex-matchable, and a full project type-check on every turn-end
would add minutes of latency. Per-turn injection is the right tool for a
behavioral stance — it shapes the work as it happens, the same way the Poe
context layer does, at near-zero cost.

Toggle via the /campsite command, which creates/removes:
    <project>/.claude/campsite-mode   (JSON: {"enabled": bool}; presence alone = on)

Hook input (stdin):  {"cwd": "...", "hook_event_name": "UserPromptSubmit", ...}
Hook output (stdout): the directive text (injected as context), or nothing.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DIRECTIVE = """<campsite-mode>
Campsite Mode is ON for this project — leave every file better than you found it.
Non-negotiable while enabled:
- NORTH-STAR ONLY. No shims, stubs, temporary workarounds, or "fix it later"
  paths. If the architecturally-sound fix is larger, do the sound fix and say why.
- FIX-AS-DISCOVERED. Any pre-existing error, lint violation, or anti-pattern you
  encounter in a file you touch gets fixed in the same change — never deferred,
  never noted-and-skipped. Discovering it is the trigger to fix it.
- TOUCHED FILES END CLEAN. Before ending a turn that changed code, run the
  project's check/lint and resolve everything attributable to the files you
  modified. "0 new errors" is not the bar; "the files I touched are clean" is.
- NO INCREMENTALISM AS AVOIDANCE. "Do A now, B later" is only legitimate when B
  is genuinely separate scope — not when B is the correct version of the same fix.
Turn this off with `/campsite off` when you deliberately want scoped, surgical
changes instead.
</campsite-mode>"""


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    cwd = payload.get("cwd") or os.getcwd()
    flag = Path(cwd) / ".claude" / "campsite-mode"
    if not flag.exists():
        sys.exit(0)

    # Presence alone means ON; if the file holds JSON, honor an explicit
    # {"enabled": false} as an off-switch that preserves any other config.
    enabled = True
    try:
        raw = flag.read_text().strip()
        if raw:
            enabled = bool(json.loads(raw).get("enabled", True))
    except Exception:
        enabled = True  # malformed flag still means "someone turned it on"

    if enabled:
        print(DIRECTIVE)
    sys.exit(0)


if __name__ == "__main__":
    main()

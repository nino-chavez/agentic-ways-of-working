#!/usr/bin/env python3
"""Claude Code statusLine — session-hygiene at a glance.

Shows, in priority order: model display name, context-window usage
(tokens used + % of window, so a fattening session is visible before it
bites), then the current working directory basename.

Input (stdin): the statusLine JSON payload documented at
https://docs.claude.com/en/docs/claude-code/statusline — notably
`model.display_name` and `context_window.{total_input_tokens,
context_window_size, used_percentage}`.

Defensive by design: every field is read with .get()/isinstance checks and
missing fields are omitted rather than raising. Any unexpected failure
falls back to printing nothing rather than crashing (a crashing statusLine
shows nothing anyway, but must never error onto the user's screen).
"""

from __future__ import annotations

import json
import os
import sys

RESET = "\033[0m"
RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"


def format_tokens(n) -> str | None:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    if n >= 1000:
        return f"{n / 1000:.0f}k"
    return str(n)


def ctx_color(pct: float) -> str:
    if pct >= 80:
        return RED
    if pct >= 50:
        return YELLOW
    return GREEN


def build_model_segment(payload: dict) -> str | None:
    model = payload.get("model")
    if not isinstance(model, dict):
        return None
    name = model.get("display_name") or model.get("id")
    if not name:
        return None
    return str(name)


def build_context_segment(payload: dict) -> str | None:
    ctx = payload.get("context_window")
    if not isinstance(ctx, dict):
        return None

    total_tokens = ctx.get("total_input_tokens")
    token_str = format_tokens(total_tokens)
    if token_str is None:
        return None

    used_pct = ctx.get("used_percentage")
    if isinstance(used_pct, (int, float)):
        pct_int = round(used_pct)
        color = ctx_color(used_pct)
        return f"{color}ctx {token_str} ({pct_int}%){RESET}"

    # Percentage not derivable from the payload directly — try computing it
    # ourselves from total_input_tokens / context_window_size before
    # falling back to a bare token count.
    window_size = ctx.get("context_window_size")
    if isinstance(window_size, (int, float)) and window_size > 0:
        try:
            pct = (float(total_tokens) / float(window_size)) * 100
            pct_int = round(pct)
            color = ctx_color(pct)
            return f"{color}ctx {token_str} ({pct_int}%){RESET}"
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    return f"ctx {token_str}"


def build_cwd_segment(payload: dict) -> str | None:
    workspace = payload.get("workspace")
    current_dir = None
    if isinstance(workspace, dict):
        current_dir = workspace.get("current_dir")
    if not current_dir:
        current_dir = payload.get("cwd")
    if not current_dir or not isinstance(current_dir, str):
        return None
    base = os.path.basename(current_dir.rstrip("/"))
    return base or "/"


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(payload, dict):
        return

    segments = []
    try:
        model_seg = build_model_segment(payload)
        if model_seg:
            segments.append(model_seg)
    except Exception:
        pass

    try:
        ctx_seg = build_context_segment(payload)
        if ctx_seg:
            segments.append(ctx_seg)
    except Exception:
        pass

    try:
        cwd_seg = build_cwd_segment(payload)
        if cwd_seg:
            segments.append(cwd_seg)
    except Exception:
        pass

    if segments:
        print(" | ".join(segments))


if __name__ == "__main__":
    main()

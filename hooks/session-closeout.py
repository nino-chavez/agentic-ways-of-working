#!/usr/bin/env python3
"""Prompt hook that routes explicit closeout language to the closeout skill.

Also runs an advisory Blueprint `screen-composition-reviewer` pass when the
target repo has adopted the judged-screen pattern (`blueprint.yml` declares a
top-level `design_intent`). Codex otherwise runs none of the Blueprint
reviewer set — Claude Code sessions get it via `blueprint review` at stage
gates, but nothing wires it into a Codex session. Closeout is the one point
every Codex session reliably passes through, so this is where the advisory
rides along.

This is read-only and never blocking: a BLOCKED or WARN verdict from the
reviewer, a missing blueprint.mjs, a timeout, or any other failure all
degrade to a one-line note in the additionalContext rather than changing
this hook's exit behavior. The closeout-routing behavior below is unchanged
from before this was added; the reviewer pass is a pure addition to the
additionalContext when it has something to say, and adds nothing when the
pattern isn't adopted here.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


CLOSEOUT = re.compile(
    r"(?:\bclose[ -]?out\b|\bwrap (?:this )?(?:up|task up|session up)\b|"
    r"\b(?:prepare|make) (?:this )?(?:task|session) (?:for archive|archive[- ]ready)\b|"
    r"\bend (?:this )?(?:task|session)\b|\barchive[- ]ready\b)",
    re.IGNORECASE,
)

# The task's own hardcoded default — matches the real checkout on this
# machine (see workspace-taxonomy: tools/ holds reusable CLIs). BLUEPRINT_HOME
# overrides it, same convention as blueprint-session-start.py.
BLUEPRINT_CLI_DEFAULT = "~/Workspace/dev/tools/blueprint/bin/blueprint.mjs"
DESIGN_INTENTS = {"preserve", "refit", "rethink"}


def _review_timeout_seconds() -> int:
    # Overridable so tests don't have to wait out a real 20s timeout to
    # exercise the timeout path.
    raw = os.environ.get("BLUEPRINT_REVIEW_TIMEOUT_SECONDS", "20")
    try:
        return int(raw)
    except ValueError:
        return 20


def find_blueprint_yml(start: Path) -> Path | None:
    """Walk up from `start` looking for `blueprint.yml`. Return its directory.

    Same walk as blueprint-session-start.py's initiative-root resolution —
    "the current repo root" for a Blueprint consumer is the nearest ancestor
    declaring blueprint.yml, not the git root (a Blueprint initiative need not
    be its own git repo, e.g. apps/minder's worktrees).
    """
    for d in [start, *start.parents]:
        if (d / "blueprint.yml").is_file():
            return d
    return None


def read_design_intent(blueprint_yml: Path) -> str | None:
    """Top-level `design_intent:` scalar, or None if absent/unreadable."""
    try:
        text = blueprint_yml.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        m = re.match(r"^design_intent:\s*([^\s#]+)", line)
        if m:
            return m.group(1).strip().strip("\"'")
    return None


def resolve_blueprint_cli() -> Path:
    raw = os.environ.get("BLUEPRINT_HOME")
    if raw:
        return Path(raw).expanduser().resolve() / "bin" / "blueprint.mjs"
    return Path(BLUEPRINT_CLI_DEFAULT).expanduser().resolve()


def screen_composition_advisory(cwd: Path) -> str | None:
    """Best-effort, read-only, never raises.

    Returns an advisory block to fold into additionalContext, or None when
    the judged-screen pattern is not adopted at `cwd` (no blueprint.yml
    ancestor, or no recognized design_intent declared there) — in that case
    nothing is appended at all, not even a skip line, because there is
    nothing to be advisory about.
    """
    try:
        repo_root = find_blueprint_yml(cwd)
        if repo_root is None:
            return None
        design_intent = read_design_intent(repo_root / "blueprint.yml")
        if design_intent not in DESIGN_INTENTS:
            return None

        cli = resolve_blueprint_cli()
        if not cli.is_file():
            return f"screen-composition-reviewer: skipped (blueprint.mjs not found at {cli})"

        result = subprocess.run(
            ["node", str(cli), "review", "screen-composition-reviewer", f"--target={repo_root}"],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_review_timeout_seconds(),
        )
        output = (result.stdout or "").strip()
        if not output:
            stderr = (result.stderr or "").strip()
            reason = stderr.splitlines()[0] if stderr else f"no output; exit {result.returncode}"
            return f"screen-composition-reviewer: skipped ({reason})"
        return "Blueprint screen-composition-reviewer (advisory, never blocking):\n" + output
    except subprocess.TimeoutExpired:
        return f"screen-composition-reviewer: skipped (timed out after {_review_timeout_seconds()}s)"
    except Exception as exc:  # never let the advisory break closeout routing
        return f"screen-composition-reviewer: skipped ({exc})"


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not CLOSEOUT.search(prompt):
        return

    context = (
        "The user invoked task closeout. Use the $session-closeout skill now. "
        "Preserve durable lessons and emit its receipt, but do not archive or "
        "delete the task automatically."
    )

    try:
        cwd = Path(payload.get("cwd") or os.getcwd())
        advisory = screen_composition_advisory(cwd)
    except Exception:
        advisory = None  # defense in depth — closeout routing must never break

    if advisory:
        context = f"{context}\n\n{advisory}"

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))


if __name__ == "__main__":
    main()

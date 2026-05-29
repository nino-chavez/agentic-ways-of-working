#!/usr/bin/env python3
"""SessionStart hook — inject Blueprint canonical methodology into a fresh session.

Encoded response to the 2026-05-25 three-session failure mode: three live consumer
sessions reasoned from first principles instead of reading the canonical docs,
then disagreed about what Blueprint *is*. Failing to load the canonical sources
at session start is a direct violation of Blueprint's own first principle
applied to Blueprint itself.

This hook fires only when the cwd is a Blueprint initiative (detected by a
`blueprint.yml` in cwd or any ancestor). For non-Blueprint sessions, exit 0
silently — no injection.

Load order (codified, not arbitrary):
  1. METHODOLOGY.md            — what Blueprint is, the first principle, the pipeline
  2. docs/variant-selection.md — which pipeline shape applies to this initiative
  3. docs/portal-and-tier-ladder.md — which portal pattern (A/B) and which tier (0/1/2)

This order matches the question hierarchy any consumer session needs to answer
before proposing methodology changes from scratch.

Resolution: BLUEPRINT_HOME env var, else `~/Workspace/dev/wip/blueprint`.

Output protocol: Claude Code SessionStart hooks emit a JSON object on stdout
with a `hookSpecificOutput.additionalContext` field. The content of that field
is injected into the session as a user-visible system reminder.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


CANONICAL_DOCS = [
    "METHODOLOGY.md",
    "docs/variant-selection.md",
    "docs/portal-and-tier-ladder.md",
]


def find_blueprint_yml(start: Path) -> Path | None:
    """Walk up from `start` looking for `blueprint.yml`. Return its directory."""
    for d in [start, *start.parents]:
        if (d / "blueprint.yml").is_file():
            return d
    return None


def resolve_blueprint_home() -> Path:
    # Point BLUEPRINT_HOME at your local clone of the Blueprint methodology repo.
    # The default assumes a sibling `blueprint/` next to this checkout.
    raw = os.environ.get("BLUEPRINT_HOME", "~/blueprint")
    return Path(raw).expanduser().resolve()


def load_canonical(blueprint_home: Path) -> list[tuple[str, str]]:
    """Read each canonical doc; return [(label, content)]. Skip missing with a note."""
    out: list[tuple[str, str]] = []
    for rel in CANONICAL_DOCS:
        path = blueprint_home / rel
        if not path.is_file():
            out.append((rel, f"<MISSING — expected at {path}>"))
            continue
        try:
            out.append((rel, path.read_text(encoding="utf-8")))
        except Exception as exc:
            out.append((rel, f"<READ-FAILED — {exc}>"))
    return out


def main() -> int:
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except Exception:
        payload = {}

    cwd = Path(payload.get("cwd") or os.getcwd()).resolve()
    initiative_root = find_blueprint_yml(cwd)
    if initiative_root is None:
        return 0

    blueprint_home = resolve_blueprint_home()
    if not blueprint_home.is_dir():
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": (
                            f"# Blueprint canonical context unavailable\n\n"
                            f"BLUEPRINT_HOME resolved to `{blueprint_home}` but that "
                            f"directory does not exist. Set BLUEPRINT_HOME or clone the "
                            f"Blueprint methodology repo and point BLUEPRINT_HOME at it.\n"
                        ),
                    }
                }
            )
        )
        return 0

    docs = load_canonical(blueprint_home)

    header = (
        "# Blueprint canonical context (auto-loaded at SessionStart)\n\n"
        f"This initiative declares `blueprint.yml` at `{initiative_root}`. The three "
        "canonical Blueprint sources are inlined below in their codified order. Read "
        "them before reasoning about methodology shape, variant choice, or portal "
        "pattern — they exist precisely so consumer sessions stop re-deriving the "
        "methodology from first principles each time.\n\n"
        "**Source of truth**: these files at `$BLUEPRINT_HOME` (`"
        f"{blueprint_home}`). If you propose a change to Blueprint methodology, "
        "the change lands in those files, not in this consumer session's notes.\n\n"
    )

    body_parts: list[str] = [header]
    for label, content in docs:
        body_parts.append(f"---\n\n## `{label}`\n\n{content}\n\n")

    additional_context = "".join(body_parts)
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": additional_context,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

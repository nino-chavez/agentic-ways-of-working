# Operator preferences

> Adapter file for Claude Code. The canonical source is [`../principles/working-style.md`](../principles/working-style.md).
> This file exists only because Claude Code reads `CLAUDE.md` by convention. Keep it a pointer — do not duplicate rules here, or the two drift.

@../principles/working-style.md

## Claude Code specifics

- Skills live in `~/.claude/skills/`. The skills in this repo install there via `install.sh`.
- Hooks are wired in `~/.claude/settings.json` (`SessionStart`, `Stop`, `UserPromptSubmit`). See `../hooks/` and the install script.
- The `/campsite` command toggles Campsite Mode per-project (see `../commands/campsite.md`).

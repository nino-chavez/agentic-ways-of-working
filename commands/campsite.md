---
description: Toggle Campsite Mode — leave-it-better, north-star-only working stance — on/off for this project
argument-hint: on | off | status
---

# /campsite — Campsite Mode toggle

Campsite Mode ("leave the campsite better than you found it") makes the assistant
hold north-star standards while enabled: no shims or temp fixes, pre-existing
defects in any touched file get fixed in the same change, and touched files end
clean. It is enforced by a `UserPromptSubmit` hook (`~/.claude/hooks/campsite.py`)
that injects the directive into context every turn while the flag is present.

**Scope:** per-project. The flag lives at `<project>/.claude/campsite-mode` so each
repo can be in or out of the mode independently.

Argument: `$ARGUMENTS` — one of `on`, `off`, `status` (default `status`).

Do exactly this, then report the result in ONE line:

1. Resolve the project root: the current working directory's repo root
   (`git rev-parse --show-toplevel`), falling back to cwd.
2. Flag file: `<project>/.claude/campsite-mode`.
3. Dispatch on the argument:
   - **on** → write `{"enabled": true}` to the flag file (create `.claude/` if needed).
     Confirm: `Campsite Mode ON — north-star only, fix-as-discovered, touched files end clean.`
   - **off** → delete the flag file. Confirm: `Campsite Mode OFF — scoped/surgical changes.`
   - **status** / empty → report whether the flag exists and is enabled.

The mode takes effect on the **next** turn (the hook reads the flag at prompt
submit). Do not paraphrase or re-implement the directive here — the hook owns it.

# mods

Claude Code mods are plugins with a hooks module. Claude Code calls that module in its own process on events such as a tool call, a prompt, or a subagent spawn. They are the in-process twin of the settings hooks in [`hooks/`](../hooks/). Unlike those, a mod can draw a band above the prompt, a pane, or a button. One directory per mod, each a complete plugin.

| Mod | What it does | Hooks it handles |
|---|---|---|
| [`blast-radius/`](blast-radius/) | Holds a risky shell command and shows what it would change, with Proceed and Cancel. Covers `rm -rf`, `git reset --hard`, `git clean`, a force push, and migrations. Vendored from Anthropic's playground at a pinned commit. Local changes: where no surface is listed (as in the desktop app's Code tab when a command arrives), Claude Code's own question dialog holds the command; with no one to ask (`claude -p`), an immediate deny; a configurable hold limit; `rm` paths read the way the shell reads them; and a script run by path (`bash x.sh`, `./x.sh`, `source x.sh`) read from disk so its risky lines hold the command too. See its `UPSTREAM.md`. | `session.start`, `tool.call{tool=Bash}`, `ui.render` |
| [`correction-band/`](correction-band/) | Draws a band above the prompt in two cases. A prompt matches a correction shape: Log it or Not a correction. The recall context plane has memory candidates pending: Review. The matcher stays in `correction-nudge.py --match`; the mod holds no regex. | `session.start`, `prompt.submit`, `ui.render{component=AbovePrompt}` |

Operator's `spawn-router` mod enforces the routed model at `agent.spawn`. It lives in the Operator repository beside the routing code it calls.

## Load

Needs Claude Code 2.1.287 or later. For one terminal session:

```bash
claude --plugin-dir ./mods/blast-radius --plugin-dir ./mods/correction-band
```

The desktop app's Code tab takes no flag. For every session, set `CLAUDE_CODE_PLUGIN_DIRS` in the `env` block of `~/.claude/settings.json`. Join the mod directories with `:`. A `~` prefix expands. Measured 2026-10-08 on 2.1.288: the debug log shows the same "Loaded inline plugin" line for a `~` path and an absolute one. A session loads whatever is on disk at start, and `/reload-plugins` picks up edits.

## Before loading a mod from outside this repository

A mod runs with your permissions and is not sandboxed. One that handles `tool.check` answers after permission rules and PreToolUse hooks, and it can approve a call they refused. Run `claude plugin validate --strict <dir>` first. Read the `hooks:` and `calls:` lines, and refuse `tool.check`. Neither mod here handles it.

## Develop

- `claude plugin validate --strict mods/<name>`: checks the manifest and lists the events handled and the mods API calls made.
- `claude plugin test mods/<name>`: runs the `tests/*.test.ts` suite with no session or network.
- `claude plugin eval mods/<name> --runs 1 --ablation none --trust-plugin`: runs the eval cases under `evals/`. A case that grants Bash needs `--allow-tools Bash`; `blast-radius` also needs `--scaffold`.
- Claude Code writes `.claude-plugin/types/` and a `tsconfig.json` beside a mod when it loads. Both are ignored. The types are the authoritative API for the installed version.

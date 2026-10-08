# correction-band

A Claude Code mod that draws a band above the prompt in two cases.

1. The prompt you just submitted has a correction shape ("why did you build...", "we already have a tool for..."). The band asks whether to record it.
2. The recall context plane has memory candidates pending review. The band keeps the count in view instead of one scrolled-away SessionStart line.

The prompt always reaches the model unchanged. The mod never drops or rewrites it.

## Rows and buttons

```text
Correction? "why did you build"   [ Log it ] [ Not a correction ]
recall sync stale (10h ago)  Recall: 33 memory candidates pending review  [ Review ]
```

| Key | Hotkey | What it does |
| --- | --- | --- |
| `log` | 1 | Submits an instruction, as the mod, quoting the stored correction text and telling the agent to run `correction-log`. The row clears at the next operator prompt. Clears the row. |
| `dismiss` | 2 | Clears the row. Submits nothing. |
| `review` | 3 | Runs `/recall review` through `$.command.run`. If that rejects, fills the prompt box with `/recall review` (the host refuses a `$.prompt.submit` text that starts with `/`). Hides the row for the session. |

The stale prefix shows when the heartbeat is missing a `last_success`, unparsable there, or older than 6 hours, the rule `recall-review-nudge.py` uses. A stale heartbeat with zero candidates shows the stale line alone. Nothing pending draws nothing. The band never exceeds two rows or `maxRows`.

## What it reads and runs

- `~/.claude/recall-context-heartbeat.json`, read with `$.fs.read` at `session.start`.
- `python3 ~/.claude/hooks/correction-nudge.py --match`, with `{"prompt": "<text>"}` on stdin and a 1.5 s timeout. The matcher stays owned by that script; this mod holds no pattern.
- Prompts starting with `/`, `<`, `[Request interrupted`, `This session is being continued`, or `Caveat: The messages below`, and prompts over 1200 characters, are never sent to the matcher.

`claude plugin validate --strict` reports:

```text
hooks: session.start, prompt.submit, ui.render{component=AbovePrompt}
calls: $.command.run, $.env.get, $.fs.read, $.process.run, $.prompt.fill, $.prompt.submit, $.state.get, $.state.set, $.ui.invalidate, $.ui.resolve
```

A submitted prompt waits for the matcher up to 1.5 s (measured about 30 ms) and goes through unchanged if it times out. Any error, timeout, non-zero exit, or unparsable output fails open: the prompt flows and no band is drawn.

## The `--match` contract

`correction-nudge.py --match` reads the hook's stdin JSON, prints one JSON line `{"matched": "<pattern>" | null, "phrase": "<text>" | null}`, exits 0, writes no marker files, and ignores the per-session claim. The transport is stdin, which `$.process.run` supports through `init.stdin`, so no `--payload-file` flag is needed.

## Load it

```bash
claude --plugin-dir /path/to/agentic-ways-of-working/mods/correction-band
# or
CLAUDE_CODE_PLUGIN_DIRS=/path/to/agentic-ways-of-working/mods/correction-band claude
```

Off switch: `CORRECTION_BAND_OFF=1` skips the matcher; recall rows still draw.

## Tests

```bash
claude plugin test mods/correction-band
```

Twenty drawing and flow tests, in both the terminal and desktop surfaces for the rows and buttons. The test kit does not honor a plugin origin on `$.prompt.submit`, so the own-submission guard (`next.origin.plugin`) is not unit tested. The engine skips the calling plugin's own hooks for `$.prompt.submit`, and the canary covers the flow.

`evals/does-not-block-prompts` checks that a correction-shaped prompt still reaches the model with the mod loaded (`claude plugin eval`). The band itself is covered by the drawing tests, since an eval cannot press a button.

Tested on Claude Code 2.1.288. Canary:

```bash
claude -p "why did you build a new script for this, we already have a tool for it. Reply with exactly the word ACK." --plugin-dir mods/correction-band --max-turns 2
```

Headless sessions draw nothing, so the canary proves prompt flow only.

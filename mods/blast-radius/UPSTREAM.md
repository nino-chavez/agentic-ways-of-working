# Upstream

Vendored from Anthropic's claude-code-playground.

- Source: https://github.com/anthropics/claude-code-playground, path `claude-code/mods/blast-radius/`
- Commit: `e9ab132d4575390ecbadfc54649712432b1a3351` (2026-10-01)
- License: Apache-2.0, text in `LICENSE` beside this file (copied from the repository root at that commit)
- Not vendored: `screenshots/`. The README links to them at the pinned commit.
- CLI version tested: 2.1.288; 2.1.294 for local changes 3, 5 and 6

## Local changes

1. `.claude-plugin/plugin.json`: version `0.1.0+aww.2`.
2. `.gitignore`: adds `evals/results/` and the generated `tsconfig.json`.
3. `hooks/blast-radius.mjs`, where the person answers. A surface that draws gets
   the pane, as upstream. With none, the hold is the engine's own question dialog
   (`$.ui.ask`, with Proceed and Cancel), polled under the same limit and interrupt
   as the pane. Proceed runs the command. Cancel, a dismissal, or a typed answer
   refuses it, and a typed answer is quoted back to Claude. A question rejected
   because the session has no AskUserQuestion tool means nobody could be asked.
   Then the command is denied at once, or passed through with one log line when
   `BLAST_RADIUS_HEADLESS=allow` is in Claude Code's own environment. A question
   rejected before the first poll ends (0.25 s) is a dismissal straight away, or a
   host answering without a person: one that refuses questions, or an SDK script
   that approves every request with no answer. A person may be there, so it is
   always denied, and the setting does not apply. Such a script can answer the
   question with Proceed instead. The denies name the signals that decided:
   `isInteractive`, the drawing surfaces, and the question's rejection. A remote
   surface that places no pane falls back to the question too. A pane that opened
   unplaced is closed when the hold ends.

   Why: the first local version denied whenever `isInteractive` was false or no
   surface drew. That refused every risky command in the desktop app's Code tab,
   with the user present and approving in chat (observed 2026-10-08).
   Measured the same day in a stream-json host launched like the desktop app
   (`--permission-prompt-tool stdio`, `--permission-mode auto`):
   - `isInteractive` was false and `$.session.surfaces()` was empty.
   - `$.ui.ask` arrived as a `can_use_tool` request for AskUserQuestion, marked
     `requires_user_interaction`. The answer came back as the label chosen.
   - Under plain `claude -p` the ask rejected in 0 ms: "no tool named
     AskUserQuestion". Upstream instead waited the full hold limit there, still
     held at 120 s in an earlier canary.
   `evals/sdk-host-canary/` reproduces all of it. That host never asks to draw,
   which is why its surface list stayed empty.

   Then measured in the real desktop app, with a probe mod hot-loaded into a
   Code-tab session:
   - `isInteractive` was false at `session.start`.
   - `$.session.surfaces()` listed `desktop` when the probe loaded, was empty when
     a Bash call arrived, and listed `desktop` again after the probe drew.
   - `$.ui.open` came back placed, and the person saw the pane.
   - `$.ui.ask` reached the person, who answered in about 13 s.

   So in the desktop both paths reach the person: the pane when `desktop` is
   listed, the question when the list is empty. A second probe run gave the same
   readings. After the merge, a delete of a scratch folder in that session took
   13 s from call to result and then ran. That fits a hold answered with Proceed;
   the pane or question itself was not seen by the session.
4. `hooks/blast-radius.mjs`, hold limit. `BLAST_RADIUS_HOLD_SECONDS` (default 600,
   minimum 5) replaces the hard-coded 10 minutes. The deny says nobody answered,
   and for the question that it may still be open.
5. `hooks/blast-radius.mjs`, `rm` paths. `tokenize` reads words as the shell does:
   quoted and unquoted pieces of one word stay together. Upstream read
   `rm -rf "$DIR"/*` as the targets `$DIR` and `/*`. Outside single quotes it
   fills in a variable assigned a plain value earlier on the line, and `$HOME` at
   the start of a word becomes `~`. These are left as written and reported, never
   run:
   - a variable not set on the line, or set from a command or another variable;
   - one assigned in a subshell, before a pipe, or after `&&` or `||`;
   - an unquoted value with spaces, which the shell would split;
   - `$1` and `${X:-y}`;
   - `$(...)` and backticks.
   `measureRm` measures only what it could fill in. Its summary names the rest
   and why, where upstream measured the literal text and said "delete nothing".
6. `hooks/blast-radius.mjs`, the `tool.call` hook carries `.catch`. A throw outside
   the hold's own `try`, or an overrun budget, refuses the command. Upstream let it run.
7. `tests/blast-radius.test.ts`, `evals/holds-rm-rf/`, `evals/sdk-host-canary/` and
   `evals/desktop-probe/`: new.
8. `README.md`: screenshot links point at the pinned commit; "Local changes" added.

## Validate output

Upstream (`claude plugin validate --strict`):

    hooks: tool.call{tool=Bash}, ui.render{component=Pane}, ui.render{component=AbovePrompt}
    calls: $.clock.now, $.process.run, $.session.cwd, $.ui.close, $.ui.invalidate, $.ui.open, $.ui.resolve, $.ui.toast

Local (CLI 2.1.294):

    hooks: session.start, tool.call{tool=Bash}, ui.render{component=Pane}, ui.render{component=AbovePrompt}
    gating hook with .catch: tool.call{tool=Bash}
    calls: $.clock.now, $.env.get, $.process.run, $.session.cwd, $.session.surfaces, $.ui.ask, $.ui.close, $.ui.invalidate, $.ui.log, $.ui.open, $.ui.resolve, $.ui.toast
    env reads: BLAST_RADIUS_HEADLESS, BLAST_RADIUS_HOLD_SECONDS

## Proving it

- `claude plugin test mods/blast-radius` runs the unit tests: the pane, the
  question dialog's answers, the nobody-to-ask deny, the hold limit, and `rm`
  word splitting.
- `evals/sdk-host-canary/run.sh <mode>` runs Haiku in a stream-json host shaped
  like the desktop app's Code tab. The host answers the question as a person
  would, or plays plain `claude -p`. Modes:
  - `proceed`: the command runs.
  - `deny-late`: dismissed after 1.5 s; refused as dismissed.
  - `deny`: dismissed at once; refused, and never run even with the setting.
  - `none`: unanswered; refused at the limit. Use `BLAST_RADIUS_HOLD_SECONDS=5`.
  - `allow-all`: approved with no answer, as an SDK script that allows every
    request does; refused at once, and never run even with the setting.
  - `bare`: plain `claude -p`; refused at once with the signals named.
  It skips your settings and unsets `CLAUDE_CODE_PLUGIN_DIRS`, so only this folder
  loads.
- `evals/holds-rm-rf` is a `claude plugin eval` case for an unattended session.
  Not rerun after change 3: whether the eval host offers a question dialog is
  unknown. On the personal Mac, cases that grant Bash score 0 for an unrelated
  reason (Docker cli-plugins symlinks), so prove with the two above.
- The desktop app itself is the final check: after the change loads there, a
  risky command should raise the Blast Radius pane or question.
  `evals/desktop-probe/install.sh <session-id>` installs the probe above into a
  session's hot-reload folder. After the person enables hot reloading, a Bash call
  containing `BR_DESKTOP_PROBE` reports what that host gives a mod, with no restart.

## Re-vendoring

1. Clone the source repository into a scratch directory and check out the new commit.
2. Diff its `hooks/blast-radius.mjs` against this one. Local lines are marked
   "Local change" in comments.
3. Copy the upstream files over, reapply the local changes, bump the commit and
   date above, and keep `version` as `<upstream>+aww.<n>`.
4. Run `claude plugin validate --strict mods/blast-radius`,
   `claude plugin test mods/blast-radius`, and the canary's `proceed` and `bare` modes.

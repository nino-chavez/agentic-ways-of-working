# Upstream

Vendored from Anthropic's claude-code-playground.

- Source: https://github.com/anthropics/claude-code-playground, path `claude-code/mods/blast-radius/`
- Commit: `e9ab132d4575390ecbadfc54649712432b1a3351` (2026-10-01)
- License: Apache-2.0, text in `LICENSE` beside this file (copied from the repository root at that commit)
- Not vendored: `screenshots/`. The README links to them at the pinned commit.
- CLI version tested: 2.1.288; 2.1.294 for local changes 3, 5, 6, 9 and 10

## Local changes

1. `.claude-plugin/plugin.json`: version `0.1.0+aww.3`.
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
   fills in a variable assigned a plain value earlier on the line (`export`,
   `local` and `readonly` count), and `$HOME` at the start of a word, or of an
   assignment's value (`OUT="$HOME/x"`), becomes `~`. These are left as written
   and reported, never run:
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
9. `hooks/blast-radius.mjs`, a script run by path. `classify` takes options:
   `scripts` makes `classifySegment` return `{ kind: "script" }` for `bash x.sh`
   (also sh, zsh, dash, ksh, with options before the file; `-c` and `-s` run no
   file and return nothing), `. x.sh`, `source x.sh`, and a bare path with a `/`
   in it; the markers are returned, in line order, only when no segment is risky
   itself, so a risk written on the line still wins. The hook reads them in
   order and holds on the first risky one; a clean or skipped script moves on
   to the next, so `bash clean.sh && bash local-delete.sh` and
   `./node_modules/.bin/tsc && ./cleanup.sh` are held on the second script (the
   first version read only the first script, found by the commit review).
   An option group ending in `o` or `O` takes a value (`bash -euo pipefail x.sh`
   reads `x.sh`, not `pipefail`); the first draft took the value as the file,
   also found by the review. `scriptRisk` resolves a relative path against
   the session folder moved by any `cd` on the line, an absolute or `~` path
   as it is (`~` from `$HOME`), and reads
   the file with `$.fs.stat({ resolve: true })` and `$.fs.read` when it is a
   regular file of at most 256 KiB whose real path lies under the session folder,
   `$HOME` (each resolved the same way), or this session's scratchpad,
   `/tmp/claude-<uid>/<project>/<$.session.id()>/scratchpad/`. Measured
   2026-10-08: under `claude -p` a probe mod's `$.session.id()` was the folder name
   of the scratchpad that session was given, and in the desktop Code tab
   `CLAUDE_CODE_SESSION_ID` matched this session's scratchpad. A bare path is
   kept only when its name ends in `.sh`, `.bash`, `.zsh` or `.ksh`, or its
   shebang names one of those shells, `env` forms included. `riskyLines(text)` then returns every risky segment with its line number; it
   never looks for scripts, which keeps the read one level deep. The risk of
   kind `script` carries the real path, the name, the lines and the line count,
   and its label reads "rm -rf in local-delete.sh line 28 and 1 more risky line".
   `measureScript` measures the first five lines with `measure`, each from where
   the script runs moved by the script's own `cd` above the line, and composes
   "run local-delete.sh, where line 28 (rm -rf) would delete 3 files (about
   9.2 GB); line 29 (rm -rf) would ..." with the file's lines prefixed by their
   line number and a note saying where it was read from. A skipped read is
   logged to the transcript with its reason (too big, outside the three folders,
   missing, a folder, a path the line does not expand, a `cd` that needs a
   command to expand before a relative path); a clean script, and every skip of
   a bare path without a shell name, log to the debug sink only, because such a
   path is usually a program (`/usr/bin/git status`) and a transcript line on
   each call would bury the real skips (the review's second finding). A `cd`
   that cannot be resolved skips rather than holds, unlike an inline risk in the
   same case: there the `rm -rf` is on the line and certain, here nothing is
   known about the script yet, and holding every `cd "$(...)" && ./x.sh` would
   be a hold on nothing (the review's third finding, decided this way). An
   absolute or `~` script path after such a `cd` is still read; its risky lines
   then hold with "Couldn't find the folder", unmeasured, since where they run
   is unknown. The read itself is guarded: a file that stats but cannot be
   read (no permission, not text) is skipped with that reason, where the first
   draft let the rejection reach the hook's `.catch` and refuse the command with
   no Proceed (the third review round). Scripts on a held line that were not
   read, after the risky one or beside an inline risk, are named in the summary:
   "Proceed also runs wipe-db.sh, which was not read" (same round). Two of that
   round's suggestions were not taken, as decisions rather than oversights:
   reading from `/tmp` and `$TMPDIR` would widen the three-folder rule the task
   set (session folder, scratchpad, `$HOME`), so a script there still skips with
   its transcript line; and `mkdir out && cd out && ./run.sh` skips because the
   folder, and usually the script, does not exist when the hook runs, the same
   reasoning as the unresolvable `cd` above.

   Why: observed 2026-10-08 in the desktop Code tab. A scratchpad script holding
   two `rm -rf` lines (a Local Sites folder and its run dir, 9.2 GiB) ran at once
   as `bash local-delete.sh`, while the same deletions written inline in a
   heredoc earlier that day were held. The mod read the command text only, so a
   script by path was invisible to it. The user had asked for the delete and the
   script gated itself on an export's integrity check, so nothing was lost, but
   the guard offered no hold where it was designed to.

   Out of scope, said in the README: a script the script calls, `bash -c`,
   `python x.py` and other languages, control flow (an assignment or `cd` inside
   an `if` is used as if it ran), heredoc bodies (read as shell), and the file
   changing between the read and Proceed.
10. `hooks/blast-radius.mjs`, continued lines. The hook reads a command line,
    and `riskyLines` reads a script, twice: as written, and with each `\`-newline
    pair replaced by U+2028, a word break the splitter does not count as a new
    line. A risk in either reading holds; the joined one is reported, since it
    measures `rm -rf \` + `build` as one `rm`. So the joined reading only adds
    holds, such as `rm \` + `-rf build`, which upstream missed. Its naive join
    misreads `echo foo\\` at a line end, and the reading as written still holds
    that line.

    Why two readings and no comment stripping: an earlier draft of this change
    removed `#` comments before reading. Four commit-review rounds each traced a
    way it hid a real `rm -rf` on the command line: an apostrophe in a heredoc
    body, quotes nested in `"$(...)"`, and `$'it\'s'` put its quote state out of
    step, so a later quoted `#` swallowed `; rm -rf build`. Each was a rewrite
    removing a hold. It was taken out rather than patched a fifth time, since a
    comment line's first word is `#` and was never read as a command anyway.
    Comments now read as upstream: `# done; rm -rf old` is held. (The draft's
    README said upstream held `echo done # rm -rf /`. It did not: the command is
    `echo`.) `evals/no-weaker/` checks the property this rests on.

## Validate output

Upstream (`claude plugin validate --strict`):

    hooks: tool.call{tool=Bash}, ui.render{component=Pane}, ui.render{component=AbovePrompt}
    calls: $.clock.now, $.process.run, $.session.cwd, $.ui.close, $.ui.invalidate, $.ui.open, $.ui.resolve, $.ui.toast

Local (CLI 2.1.294):

    hooks: session.start, tool.call{tool=Bash}, ui.render{component=Pane}, ui.render{component=AbovePrompt}
    gating hook with .catch: tool.call{tool=Bash}
    calls: $.clock.now, $.env.get, $.fs.read (via scriptRisk), $.fs.stat (via localRoots, scriptRisk), $.process.run, $.session.cwd, $.session.id (via scriptRisk), $.session.surfaces, $.ui.ask, $.ui.close, $.ui.invalidate, $.ui.log, $.ui.open, $.ui.resolve, $.ui.toast
    env writes: nothing
    env reads: BLAST_RADIUS_HEADLESS, BLAST_RADIUS_HOLD_SECONDS, HOME

`$.fs.read`, `$.fs.stat` and `$.session.id` are the script read (change 9):
`$.session.id` names this session's scratchpad. `HOME` resolves `~` and is one
of the three folders a script may be read from.

## Proving it

- `claude plugin test mods/blast-radius` runs the unit tests (70): the pane, the
  question dialog's answers, the nobody-to-ask deny, the hold limit, `rm` word
  splitting, scripts run by path (each invocation form, the three folders, a
  link that lands outside, the size cap, a read that rejects, one level,
  several scripts on one line and the unread ones named, a risk on the line
  winning, continued lines, this session's scratchpad and not another's,
  `cd` on the line and in the script, the question's text), and four inputs
  that review traced to a removed comment stripper, each still held. The
  file system is stubbed: `fs.stat` answers from a map of paths to real paths,
  sizes and kinds, `fs.read` from the same map.
- The scratchpad rule, through the real engine (CLI 2.1.294, 2026-10-08, by
  hand with `sdk_host.py bare`): Haiku wrote `delete-build.sh` with the Write
  tool into its own scratchpad, `/private/tmp/claude-501/<project>/<session
  id>/scratchpad/`, outside its working folder and `$HOME`, then ran `bash`
  on that path. The deny said "run delete-build.sh, where line 2 (rm -rf) would
  delete 3 files (about 12 KB)", and the three files stayed. This is the shape
  observed that afternoon.
- `evals/no-weaker/run.sh [ref]` loads the classifier at `ref` (default `main`)
  and this folder's, and runs every string literal in the test file plus the
  shapes review traced: each one the old holds, the new must hold. 2026-10-08:
  46 held by `main`, 0 no longer held; with the reading as written switched
  off, it reported the escaped-backslash line, so it can fail.
- `evals/sdk-host-canary/run.sh <mode> [inline|script]` runs Haiku in a
  stream-json host shaped like the desktop app's Code tab. The host answers the
  question as a person would, or plays plain `claude -p`. With `script`, the
  prompt is `bash delete-build.sh`, a script written beside `build/` with the
  `rm -rf` on its line 4 under a comment that also says rm -rf; the hold must
  name line 4 only. This is the check that the engine's `$.fs.stat` answers
  `realPath` and `size` as the stubs assume. Measured on CLI 2.1.294,
  2026-10-08: `bare script` denied at 3.1 s with "run delete-build.sh, where
  line 4 (rm -rf) would delete 3 files (about 12 KB)", and build/ kept its three
  files; `proceed script` asked "Blast Radius held `bash delete-build.sh`. It
  would run delete-build.sh, where line 4 (rm -rf) would delete 3 files (about
  12 KB). That includes line 4: build/c.txt, ... Read from /private/var/.../
  delete-build.sh (5 lines); risky line: 4. Line 4: Paths: build. Run it?",
  took Proceed and passed the call, and the auto-mode classifier then denied
  that Bash call as Irreversible Local Destruction, below the mod, so build/
  stayed; `proceed inline` the same minute ran `rm -rf build` and emptied
  build/. The classifier's verdict is the engine's permission layer, not this
  mod's. Modes:
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

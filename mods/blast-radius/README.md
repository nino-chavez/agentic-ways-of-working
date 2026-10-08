# Blast Radius

A Claude Code mod that holds a risky shell command and shows you what it would change before it runs. We're sharing it as a small, complete example of a mod that pauses a tool call and asks you to decide.

## What this shows

When Claude calls Bash with one of the commands below, Blast Radius stops the call, works out what the command would touch, and opens a pane with two buttons: **Proceed** runs the command, **Cancel** refuses it. Claude sees the refusal and the reason.

| Command | What the pane shows |
|---|---|
| `rm -r`, `rm -f`, `rm -rf` | The files it would delete, with the count and total size. Globs and `~` are expanded. |
| `git reset --hard` | The files with uncommitted changes, from `git status --porcelain`, and `git diff --shortstat`. |
| `git checkout -- .`, `git restore .` | The files with unstaged changes. |
| `git clean` | The untracked paths it would remove, from `git clean -n` with the same flags. |
| `git push --force` (also `-f`, `--force-with-lease`, `+ref`) | The commits on the remote branch that your HEAD doesn't have, which the push would drop. |
| `manage.py migrate`, `db:migrate`, `alembic upgrade`, `prisma migrate` | The pending migrations, from the tool's own status command. |
| any other `migrate` | A note that it can't list the pending migrations for that tool. |
| `bash x.sh` (also `sh`, `zsh`, `dash`, `ksh`), `. x.sh`, `source x.sh`, `./x.sh` | Local change: the script is read from disk and each risky line in it is held and measured as if it had been typed inline, named by file and line. |

Every other command runs as normal. If the command line moves first, with `cd dir &&`, `pushd`/`popd` or `git -C dir`, Blast Radius measures in that folder. A `cd` inside `( ... )` only applies inside the parentheses, as in the shell.

The patterns it demonstrates:

- Holding a `tool.call` until the user answers, and returning `{ deny }` with a reason Claude can act on.
- Drawing the same report in a `Pane`, or in the `AbovePrompt` band when the terminal is too narrow for a pane.
- Measuring with `$.process.run`, passing paths as arguments so nothing in them runs as shell.

## Demo

`rm -rf build` held, with the files it would delete:

![Blast Radius holding rm -rf build in a pane](https://github.com/anthropics/claude-code-playground/blob/e9ab132d4575390ecbadfc54649712432b1a3351/claude-code/mods/blast-radius/screenshots/blast-radius-pane.png)

After Cancel, Claude reports that nothing was deleted:

![Claude reports the command was cancelled](https://github.com/anthropics/claude-code-playground/blob/e9ab132d4575390ecbadfc54649712432b1a3351/claude-code/mods/blast-radius/screenshots/blast-radius-cancelled.png)

The second try, after Proceed:

![Claude reports the folder was deleted after Proceed](https://github.com/anthropics/claude-code-playground/blob/e9ab132d4575390ecbadfc54649712432b1a3351/claude-code/mods/blast-radius/screenshots/blast-radius-proceeded.png)

`git reset --hard` in a 120-column terminal, where the report is drawn in the band above the prompt:

![Blast Radius showing git reset --hard in the band above the prompt](https://github.com/anthropics/claude-code-playground/blob/e9ab132d4575390ecbadfc54649712432b1a3351/claude-code/mods/blast-radius/screenshots/blast-radius-band.png)

## How it was built

- **Model:** built with Claude in Claude Code. The test runs and screenshots used Claude Sonnet 5. The mod itself doesn't call a model.
- **Prompt(s):** the mod started as one of ten ideas Claude wrote for mods. This is the idea as written:

  > **Blast Radius.** _See what a command will touch, before it runs._ When Claude runs a risky shell command, like `rm`, `git reset`, or a migration, the mod opens a pane. The pane lists the files and branches the command would change. The developer presses Proceed, or Cancel. It uses `tool.call` to hold the call, and `ui.render` on `Pane`, with `Button` elements. It adds a safety check, and it doesn't remove any, so it's safe to show. _A bigger project,_ because each command needs its own dry run.

  The build prompt, which picked this idea and two others by number:

  > implement 1,2,7. give me zips for them. test them in claude code and get me screenshots of what they look like when used.

- **Transcript:** not shared. The build ran in an internal workspace.
- **Iterations:**
  - A hook has a 10-second budget for its own code, which is far too short to wait for a person. Time spent inside a `$` call doesn't count, so the hold is a loop that waits on `$.process.run(["sleep", "0.25"])` until a button's `onPress` sets the decision.
  - A pane needs room. In a narrow terminal Claude Code doesn't place it, so the mod checks `isPlaced` and draws the report in the `AbovePrompt` band instead.
  - Cancel has the focus when the pane opens, so pressing Enter refuses the command rather than running it.
  - An independent review before release found two problems in the measuring step, both fixed. A file named like a `find` action (for example `-delete`) that a glob matched could be read as an action while measuring, so relative paths now go to `find` with `./` in front. And a `cd` earlier on the command line was ignored, so the wrong folder was measured; the mod now follows `cd` and `git -C`. The same review led to smaller fixes: overlapping holds are queued, and the buttons always answer the command shown; an error while holding refuses the command; `git clean -e`, `src:dst` and `HEAD` force pushes are measured correctly; and sizes use `du -k`, which works on macOS as well as Linux.
  - Tested in Claude Code, before those fixes: `rm -rf build` was held for more than 30 seconds, Cancel kept the files and Proceed deleted them; `git reset --hard` listed the two changed files and Cancel kept them; `git clean -fdx` was held. Force pushes and migrations were checked against the command classifier only, not run. The fixes are covered by tests of the classifier, the measuring step and the hold queue, run with Node against a stand-in for Claude Code; they haven't been re-run in a live session.

## Run it

**Requirements:**

- Claude Code 2.1.287 or later, where mods load by default. The mod was built and tested on 2.1.280, and `claude plugin validate` passes on 2.1.285.
- `bash`, `git`, `find` and `du` on your `PATH`. For migrations, the project's own tool (for example `python3 manage.py showmigrations`).
- For the side pane, a terminal about 144 columns wide or more. Narrower terminals get the band above the prompt.

No environment variables or configuration.

**Steps:**

1. Clone this repository and go to this folder's parent:

   ```bash
   git clone https://github.com/anthropics/claude-code-playground.git
   cd claude-code-playground/claude-code/mods
   ```

2. Check the plugin:

   ```bash
   claude plugin validate ./blast-radius
   ```

3. Try it for one session:

   ```bash
   claude --plugin-dir ./blast-radius
   ```

   Or install it, with the other mods here, from the local marketplace in this folder (see the [mods README](../README.md)):

   ```bash
   claude plugin marketplace add ./
   claude plugin install blast-radius@claude-code-playground-mods --scope user
   ```

   Remove it with `claude plugin uninstall blast-radius@claude-code-playground-mods --scope user`.

4. Ask Claude to run something risky in a throwaway repo, for example "delete the build folder with rm -rf build".

**Using the pane:**

- Press `1` for Proceed or `2` for Cancel. You can also click a button, or Tab to it and press Enter.
- In the band above the prompt, `1` or `2` works while the input is empty.
- If nobody answers within 10 minutes, the command is refused.
- If you interrupt the turn (Esc), the command is refused.

## Notes / limitations

- It reads the command text, and (a local change) a script the command runs by path. It doesn't parse shell fully: `$(...)`, aliases, `eval`, `bash -c "..."`, `xargs rm`, `find -delete`, and wrappers such as `timeout 5 rm`, `doas rm`, `time -p rm` or `env -i rm` aren't caught. A script that a script calls is not read, nor is a program in another language (`python x.py` and a `shutil.rmtree` inside it are out of scope).
- Only the first risky part of a command line is measured, and the pane shows the command on one line, cut off if it's long. Proceed runs the whole line as written.
- It follows `cd`, `pushd`, `popd` and `git -C` on the same line. `cd -`, and a folder that doesn't exist, can't be measured; the pane says so and still holds the command. Otherwise it starts from the session's working folder.
- In a narrow terminal the report is drawn in the band above the prompt, which only one mod can use at a time. If another mod that draws there (such as Replay Theater or Token Weather in this folder) takes the band, the Proceed and Cancel buttons may not show, and the command is refused after 10 minutes. Use a wider terminal, or turn the other mod off, when you rely on Blast Radius.
- One command is held at a time. A second risky call, from a subagent for example, waits until the first is answered.
- It only watches the Bash tool. File edits and other tools aren't held.
- The `rm` count is approximate: a path matched twice is counted twice, and a file name with a line break is not counted. The list shows the first 10 files. Counts come from `find` and sizes from `du -k`, so a size is the space on disk, to the nearest kilobyte. A very large tree can take a few seconds to measure.
- The force-push list uses your last fetch of the remote branch. Without one, it can't list the dropped commits, and it says so. When the push names no remote, it assumes `origin`.
- To list pending migrations, it runs the tool's own status command (for example `python3 manage.py showmigrations`), which loads your project's code before you choose Proceed or Cancel. If that fails, the pane says it couldn't list them.
- A few harmless commands are held too, such as a commit whose message contains `; rm -rf`.
- It checks one command at a time. It holds a call, but it doesn't sandbox it: after you press Proceed, the command runs as written.
- This is a safety net, not a permission system. Use [permission rules](https://code.claude.com/docs/en/settings) for a hard block.

## Dependencies

| Name | Version | License (SPDX) | Source |
| --- | --- | --- | --- |
| None | | | |

The mod has no packages to install. It calls tools that are already on the machine (listed under Requirements).

## Third-party notices

Git is a trademark of Software Freedom Conservancy. Python is a registered trademark of the Python Software Foundation. npm is a trademark of npm, Inc. Django is a registered trademark of the Django Software Foundation. Rails and Ruby on Rails are trademarks of David Heinemeier Hansson. Prisma is a trademark of Prisma Data, Inc. Alembic is an open-source project of the SQLAlchemy authors. Use of these names here is descriptive and implies no endorsement.

## Local changes

This copy is vendored at upstream commit `e9ab132` and tested with Claude Code CLI 2.1.288, and 2.1.294 for the question-dialog hold and the script read. See `UPSTREAM.md` for the full list.

- **Where no surface is listed, Claude Code's own question dialog holds the command.** In the desktop app's Code tab the mod sees `isInteractive=false`, and the list of drawing surfaces can be empty at the moment a command arrives. When it lists `desktop`, you get the pane, which the desktop draws. When it is empty, the command is put to you as a question with Proceed and Cancel. It includes the command, what it would delete, and the first few paths. Proceed runs it. Cancel, dismissing the question, or typing your own answer refuses it, and a typed answer is passed back to Claude.
- **Sessions with no one to ask still deny at once.** Under `claude -p` there is no question dialog, so the question fails straight away and the command is refused. The refusal names the signals that decided: `isInteractive`, the drawing surfaces, and why the question failed. To let such runs delete, start Claude Code with `BLAST_RADIUS_HEADLESS=allow` in its own environment; one line is logged. A `VAR=value` prefix on the Bash command does not reach the mod. The setting applies only where no question dialog exists. It never skips a question, and never runs a command whose question was refused.
- **The hold has a configurable limit.** `BLAST_RADIUS_HOLD_SECONDS` (default 600, minimum 5) sets how long a hold waits before refusing. This replaces the fixed 10 minutes above. A question nobody answers is refused at the limit; it may stay open, and answering it then does nothing.
- **`rm` paths are read the way the shell reads them.** The pieces of one word stay together, so `"$DIR"/*` is one path, not `$DIR` and the filesystem root `/*`. A variable set to a plain value earlier on the same line is filled in (`export`, `local` and `readonly` count), and `$HOME` at the start of a word or of an assignment's value becomes `~`. Anything else is not measured, and the summary says why instead of "delete nothing". That covers a variable from the shell's environment, `$(...)`, and an unquoted value with spaces. Nothing is run to find a value.
- **A script run by path is read, and its risky lines hold the command.** Upstream read only the command text, so `bash local-delete.sh` ran at once while the same `rm -rf` lines typed inline were held (observed 2026-10-08). Now `bash x.sh`, `sh`, `zsh`, `dash`, `ksh` with options before the file, `. x.sh`, `source x.sh`, and a bare path such as `./x.sh` or `~/bin/x` make the mod read the file and check its lines the way it checks a command line, so a `cd` above a line moves where that line is measured, and a plain assignment above it fills in a variable; `$1`, a value from a command, and the rest are said to be unmeasured. The hold names the file and the lines: "rm -rf in local-delete.sh line 28 and 1 more risky line", and the summary says what each line would do. The first five risky lines are measured; the rest are named. Comments are not removed. A comment line starts with `#`, which is not a command, so `# rm -rf old` is not held, but `# done; rm -rf old` is, because the line is split at `;` first. A bare path is read only when it has a shell shebang or a `.sh`, `.bash`, `.zsh` or `.ksh` name; `#!/usr/bin/env node` passes with a debug line.
  - The file must be a regular file of 256 KiB or less, and must land, every link followed, inside the session folder, your home folder, or this session's scratchpad (`/tmp/claude-<uid>/<project>/<session id>/scratchpad/`; another session's is not read). Anything else is not read, and a transcript line says what was skipped and why: too big, outside those folders, missing, a folder, a path from a variable the line does not set, a relative path after a `cd` that needs a command to expand, or `bash -c`. For a bare path without a shell name (`/usr/bin/git status`, `.venv/bin/python x.py`), which is usually a program, that line goes to the debug log instead.
  - Every script on the line is read, in order, until one is risky; a clean or skipped one does not end the check. One level: a script that the script runs is not read. A risk written on the command line itself wins, and no script is then read. Scripts after the held one, or beside an inline risk, are named in the summary ("Proceed also runs wipe-db.sh, which was not read"), so Proceed is not taken as approving only what was shown. A script skipped earlier on the line (outside the folders, too big, unreadable) is not named there yet; its skip is logged on its own. A file that cannot be read is skipped with that reason, never refused as a failure. The script is read when the command is held; it may differ by the time Proceed runs it. Control flow is not modelled: an assignment or `cd` inside an `if` that would not run is still used, and the shell text inside a heredoc body is read as commands.
- **A continued line is read both ways.** A command or script is read as written and again with each line ending in `\` joined to the next, and it is held if either reading finds a risk. So `rm -rf \` followed by `build` is held and measured as `rm -rf build`, and `rm \` followed by `-rf build`, which upstream missed, is held. The joined reading can only add a hold, never remove one; `evals/no-weaker/run.sh` checks that against `main`.
- **A hook failure refuses the command.** Upstream let it run.
- To load it for every session, add its folder to `CLAUDE_CODE_PLUGIN_DIRS`.

---

Shared as-is as part of claude-code-playground. Not an official Anthropic product; no support or maintenance is implied. See the root README and LICENSE.

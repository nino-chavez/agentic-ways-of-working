# Claude review on meaningful commits

A local git `post-commit` hook that runs a focused Claude Code review **after**
a commit — but only when the commit is big enough or touches sensitive paths.
Async, no PR required, and billed to your **Claude subscription** (no API tokens).

It's the local-first counterpart to a PR-gated GitHub Action: instead of
reviewing in CI on every pull request, it reviews on the commits that actually
warrant it, right on your machine.

This folder is the only copy of these hooks. Consumers symlink into it (a
dotfiles repo through a submodule, for instance) rather than carrying their
own; the ownership decision is in the repository README under "Git hooks". If
you are reading this through a symlink, edit the file in the repository that
owns it.

## Requirements

- [Claude Code](https://claude.com/claude-code) CLI on your `PATH`, logged in to
  a Claude subscription (Pro/Max). The hook `unset`s `ANTHROPIC_API_KEY` before
  calling `claude`, so it always bills the subscription, never a metered API key.
  (If you'd rather use an API key, comment out that one line in
  `claude-review-run.sh`.)
- git, bash. macOS (`osascript`) or Linux (`notify-send`) for desktop
  notifications — optional; the review is always written to a file regardless.

## Install

```sh
# Global — runs in every repo that doesn't define its own core.hooksPath:
./install.sh

# Or just one repo:
./install.sh --repo /path/to/repo     # default: current dir

# Remove the global setting:
./install.sh --uninstall
```

## How it works

On each commit, `post-commit`:

1. Chains to any repo-local `.git/hooks/post-commit` (nothing is silently disabled).
2. Skips merge commits and commits replayed during rebase/cherry-pick.
3. Triggers a review if **either** holds:
   - a changed file matches a `paths` glob, **or**
   - net changed lines (minus `skip_paths`) ≥ `min_changed_lines` (default 80).
4. Launches the review detached. The commit returns instantly; the review lands
   in `<git-dir>/last-review.md` (~15–90s later) with a desktop notification.

Read the latest review:

```sh
cat "$(git rev-parse --git-dir)/last-review.md"
```

## Configure per repo

Copy `claude-review.conf.example` into a repo as `.claude-review.conf` and edit
`min_changed_lines`, `paths`, `skip_paths`, or `enabled`. With no config file,
the defaults apply (≥80 changed lines, no path triggers).

## Per-commit overrides

```sh
REVIEW=1 git commit -m "..."   # force a review on a small commit
REVIEW=0 git commit -m "..."   # skip a big one
git commit --no-verify ...     # bypass hooks entirely
```

Set `enabled = false` in a repo's `.claude-review.conf` to turn it off there.

## Test the wiring without spending quota

```sh
CLAUDE_REVIEW_DRY=1 REVIEW=1 git commit -m "wiring test"
cat "$(git rev-parse --git-dir)/last-review.md"   # shows a dry-run stub
```

## The verdict line

The first body line of `last-review.md` (the line after the header) is exactly
`VERDICT: <WORD> <full commit sha>`, so a script can gate on it and on the
commit it belongs to:

| First body line | Meaning | Notification | Exit |
|---|---|---|---|
| `VERDICT: PASS <sha>` | no significant issues | looks good | 0 |
| `VERDICT: CHANGES <sha>` | findings follow, cited as `file:line` | review ready | 0 |
| `VERDICT: INVALID <sha>` | the reviewer answered but did not start with a verdict; its text is kept below the line | INVALID | 2 |
| `VERDICT: FAILED <sha>` | the reviewer did not finish (auth, network, crash); whatever it wrote is kept below | FAILED | 1 |

The runner writes exactly one line starting with `VERDICT:`; a verdict-shaped
line inside the reviewer's own text is quoted as `> VERDICT: ...`, so a reply
that got as far as `VERDICT: PASS` before the CLI died still reads as FAILED. A
dry run writes no `VERDICT` line. The sha is on the line because the hook skips
a commit that lands while an earlier review is still running (single-flight), so
the file can describe an older commit than `HEAD`. Gate on the current commit
with:

```sh
grep -qx "VERDICT: PASS $(git rev-parse HEAD)" "$(git rev-parse --git-dir)/last-review.md"
```

Exit 0 means `HEAD` itself was reviewed and passed. Treat `CHANGES`, `INVALID`,
`FAILED`, a different sha and no line as not finished.

A different sha is the common case after a fix: the hook reviews only commits
that cross the size threshold or touch a sensitive path, skips a commit that
lands while another review is running, and skips merge and replayed commits.
It leaves `last-review.md` alone for every one of those on purpose, so the last
real review's findings stay readable until the next review replaces them. To
finish on `HEAD`, review it by hand:

```sh
~/.config/git-hooks/claude-review-run.sh "$(git rev-parse --show-toplevel)" "$(git rev-parse HEAD)"
```

or commit the fix with `REVIEW=1` so the hook reviews it regardless of size.
Added 2026-10-21; before it the only machine-readable signal was a grep for the
phrase "Looks good to me", so a review with findings and a review that ignored
the format looked the same to every script downstream.

## Regression test for failure signalling and the verdict line

```sh
bash git-hooks/claude-review-run.test.sh
```

Stubs `claude` and `osascript`, spends no quota, and asserts that a failed
review says so and that every reply lands on one of the three verdict lines. It
exists because the script used to notify "review ready" on *any* outcome other
than "Looks good to me" — including an auth failure — so on 2026-08-30
twenty-four unreviewed commits looked exactly like reviewed ones for sixteen
hours. If you touch `notify()`, the `claude` invocation or the prompt, run this.
`RUNNER=<path> bash git-hooks/claude-review-run.test.sh` runs the same
assertions against another copy of the runner.

## When a review fails

`last-review.md.err` holds the error, the notification says **FAILED** rather
than "review ready", and the file opens `VERDICT: FAILED <sha>`. A reply that
ignored the verdict format is a different case: the notification says
**INVALID**, the file opens `VERDICT: INVALID <sha>`, and the review text is
still there to read. The common cause is an expired login: the runner
unsets `ANTHROPIC_API_KEY` on purpose to force subscription billing, so an
expired OAuth session leaves no fallback. Check and fix with:

```sh
claude auth status      # {"loggedIn": false, ...} means every review is failing
claude auth login       # or: claude setup-token, for a long-lived token
```

## Agent-Session trailer on agent commits

`prepare-commit-msg` adds one trailer per agent session in the environment
when the commit is made, so any commit leads back to the session behind it:

```
Agent-Session: claude:<CLAUDE_CODE_SESSION_ID>
Agent-Session: codex:<CODEX_THREAD_ID>
```

Both lines appear when one harness runs inside the other (a Codex worker
launched from a Claude session inherits the Claude id). A commit you make by
hand, outside any agent, gets nothing. Only the id goes into the commit; the
transcript never leaves the machine. Find it with:

```sh
git log -1 --format='%(trailers:key=Agent-Session,valueonly)'
ls ~/.claude/projects/*/<id>.jsonl                         # claude
find ~/.codex/sessions -name "rollout-*-<id>.jsonl"        # codex
```

Rebase and cherry-pick replays get no trailer, so a replayed commit keeps the
original's attribution. An amend in the same session adds nothing; an amend
in another session adds that session too. An id that is not a plain
`[A-Za-z0-9._-]` token is ignored rather than written into the message. A
repo-local `prepare-commit-msg` in the common git dir runs first, and if it
fails the commit fails, as it would without this hook. Opt out per commit
with `AGENT_SESSION_TRAILER=0 git commit ...`. Note that `--no-verify` does
not skip `prepare-commit-msg`.

Regression test (throwaway repos, no network):

```sh
bash git-hooks/prepare-commit-msg.test.sh
```

Added 2026-10-26, after comparing Entire's CLI, which writes an
`Entire-Checkpoint:` trailer but pushes the whole transcript to the remote.
This keeps the link and leaves the transcript local.

## Note on global `core.hooksPath`

Git honors exactly one `hooksPath`. Repos that set their own (Husky, lefthook)
override the global one — they keep working but won't get these hooks. Repos
with a hand-written `.git/hooks/post-commit` or `prepare-commit-msg` and no
`core.hooksPath` are covered: each hook chains to that local script. Other
local hook types are not chained, so if
a repo relies on hand-written non-Husky hooks, install per-repo there instead.

## Files

| File | Purpose |
|------|---------|
| `post-commit` | The hook: heuristic trigger + detached launch |
| `prepare-commit-msg` | Adds the `Agent-Session:` trailer to agent commits |
| `prepare-commit-msg.test.sh` | Regression test for the trailer |
| `claude-review-run.sh` | Background worker that calls `claude` and writes the review |
| `claude-review.conf.example` | Per-repo config template |
| `install.sh` | Global or per-repo installer |

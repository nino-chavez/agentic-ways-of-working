# Claude review on meaningful commits

A local git `post-commit` hook that runs a focused Claude Code review **after**
a commit — but only when the commit is big enough or touches sensitive paths.
Async, no PR required, and billed to your **Claude subscription** (no API tokens).

It's the local-first counterpart to a PR-gated GitHub Action: instead of
reviewing in CI on every pull request, it reviews on the commits that actually
warrant it, right on your machine.

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

## Regression test for failure signalling

```sh
bash git-hooks/claude-review-run.test.sh
```

Stubs `claude` and `osascript`, spends no quota, and asserts that a failed
review says so. It exists because the script used to notify "review ready" on
*any* outcome other than "Looks good to me" — including an auth failure — so on
2026-08-30 twenty-four unreviewed commits looked exactly like reviewed ones for
sixteen hours. If you touch `notify()` or the `claude` invocation, run this.

## When a review fails

`last-review.md.err` holds the error and the notification says **FAILED**
rather than "review ready". The common cause is an expired login: the runner
unsets `ANTHROPIC_API_KEY` on purpose to force subscription billing, so an
expired OAuth session leaves no fallback. Check and fix with:

```sh
claude auth status      # {"loggedIn": false, ...} means every review is failing
claude auth login       # or: claude setup-token, for a long-lived token
```

## Note on global `core.hooksPath`

Git honors exactly one `hooksPath`. Repos that set their own (Husky, lefthook)
override the global one — they keep working but won't get this hook. Repos with
a hand-written `.git/hooks/post-commit` and no `core.hooksPath` are covered: the
hook chains to that local script. Other local hook types are not chained, so if
a repo relies on hand-written non-Husky hooks, install per-repo there instead.

## Files

| File | Purpose |
|------|---------|
| `post-commit` | The hook: heuristic trigger + detached launch |
| `claude-review-run.sh` | Background worker that calls `claude` and writes the review |
| `claude-review.conf.example` | Per-repo config template |
| `install.sh` | Global or per-repo installer |

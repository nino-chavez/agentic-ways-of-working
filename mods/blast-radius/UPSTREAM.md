# Upstream

Vendored from Anthropic's claude-code-playground.

- Source: https://github.com/anthropics/claude-code-playground, path `claude-code/mods/blast-radius/`
- Commit: `e9ab132d4575390ecbadfc54649712432b1a3351` (2026-10-01)
- License: Apache-2.0, text in `LICENSE` beside this file (copied from the repository root at that commit)
- Not vendored: `screenshots/`. The README links to them at the pinned commit.
- CLI version tested: 2.1.288

## Local changes

1. `.claude-plugin/plugin.json`: version `0.1.0+aww.1`.
2. `.gitignore`: adds `evals/results/` and the generated `tsconfig.json`.
3. `hooks/blast-radius.mjs`, headless deny. A new `session.start` hook records
   `isInteractive`. Before opening a pane, the hold checks that the session is
   interactive and `$.session.surfaces()` is non-empty. If not, it denies at once
   with a reason Claude can act on. `BLAST_RADIUS_HEADLESS=allow` passes the
   command through instead and logs one line. Upstream waited for a button no
   one could press (measured: `claude -p` canary still held at 120 s).
4. `hooks/blast-radius.mjs`, hold limit. `BLAST_RADIUS_HOLD_SECONDS` (default 600,
   minimum 5) replaces the hard-coded 10 minutes, and the deny now says nobody
   answered.
5. `tests/blast-radius.test.ts` and `evals/holds-rm-rf/`: new.
6. `README.md`: screenshot links point at the pinned commit; "Local changes" added.

## Validate output

Upstream (`claude plugin validate --strict`):

    hooks: tool.call{tool=Bash}, ui.render{component=Pane}, ui.render{component=AbovePrompt}
    calls: $.clock.now, $.process.run, $.session.cwd, $.ui.close, $.ui.invalidate, $.ui.open, $.ui.resolve, $.ui.toast

Local:

    hooks: session.start, tool.call{tool=Bash}, ui.render{component=Pane}, ui.render{component=AbovePrompt}
    calls: $.clock.now, $.env.get, $.process.run, $.session.cwd, $.session.surfaces, $.ui.close, $.ui.invalidate, $.ui.log, $.ui.open, $.ui.resolve, $.ui.toast
    env reads: BLAST_RADIUS_HEADLESS, BLAST_RADIUS_HOLD_SECONDS

## Eval

`evals/holds-rm-rf` exercises the headless path, because an eval session has no
drawing surface. Run it with `claude plugin eval` and `--scaffold`, granting Bash.

## Re-vendoring

1. Clone the source repository into a scratch directory and check out the new commit.
2. Diff its `hooks/blast-radius.mjs` against this one. Local lines are marked
   "Local change" in comments.
3. Copy the upstream files over, reapply the local changes, bump the commit and
   date above, and keep `version` as `<upstream>+aww.<n>`.
4. Run `claude plugin validate --strict mods/blast-radius` and
   `claude plugin test mods/blast-radius`.

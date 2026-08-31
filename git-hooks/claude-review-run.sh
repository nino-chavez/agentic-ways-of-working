#!/usr/bin/env bash
# Background worker: reviews a single commit with Claude Code, billed to the
# logged-in Claude subscription, and writes the result to <git-dir>/last-review.md.
#
# Args: <repo-toplevel> <commit-sha> [lock-dir]
#
# Env:
#   CLAUDE_REVIEW_DRY=1   skip the claude call (wiring test, no quota spend)

set -uo pipefail

REPO="${1:?repo required}"
SHA="${2:?sha required}"
LOCK="${3:-}"

cleanup() { [ -n "$LOCK" ] && rmdir "$LOCK" 2>/dev/null; return 0; }
trap cleanup EXIT

cd "$REPO" || exit 1

# Force subscription billing: never let an API key leak into this run.
# (If you WANT to use a metered API key instead, comment the next line out.)
unset ANTHROPIC_API_KEY

# Tell worktree-guard this session cannot contend for the checkout. The
# allowlist below is git show/diff/log/blame + Read/Glob/Grep — no commit, no
# branch switch, no edit. Without this the reviewer's session lock made every
# commit block the next one in the same repo, because reviewing commit N is
# still in flight when commit N+1 runs.
export CLAUDE_GUARD_ROLE=readonly

GIT_DIR="$(git rev-parse --git-dir)"
OUT="$GIT_DIR/last-review.md"
SHORT="${SHA:0:7}"
SUBJECT="$(git log -1 --format='%s' "$SHA")"

notify() {  # title, body — best-effort, cross-platform
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"$2\" with title \"$1\" sound name \"Pop\"" >/dev/null 2>&1 || true
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send "$1" "$2" >/dev/null 2>&1 || true
  fi
}

{
  echo "# Claude review — ${SHORT}"
  echo
  echo "> ${SUBJECT}"
  echo ">"
  echo "> commit \`${SHA}\` · $(git log -1 --format='%ci' "$SHA")"
  echo
} > "$OUT"

if [ -n "${CLAUDE_REVIEW_DRY:-}" ]; then
  echo "_(dry run: would review ${SHORT} — no claude call made)_" >> "$OUT"
  exit 0
fi

read -r -d '' PROMPT <<EOF || true
Review a single git commit in the repository "$(basename "$REPO")".

Commit: $SHA
Subject: $SUBJECT

Run \`git show $SHA\` to see the full diff. Read surrounding files for context as needed.

Focus only on issues that meaningfully affect correctness or safety:
- Bugs, logic errors, unhandled edge cases
- Security: injection, auth bypass, secret/PII exposure, unsafe input handling
- Data/query correctness: parameterized queries, migration safety, race conditions
- Violations of this repo's conventions (read CLAUDE.md / AGENTS.md if present)

Ignore style nits, formatting, and anything with no real impact.
Cite findings as file:line. Be specific and concise.
If there are no significant issues, respond with exactly: Looks good to me!
Output GitHub-flavored markdown only — no preamble.
EOF

if claude -p "$PROMPT" \
     --permission-mode default \
     --allowedTools "Bash(git show:*),Bash(git diff:*),Bash(git log:*),Bash(git blame:*),Read,Glob,Grep" \
     >> "$OUT" 2>>"$OUT.err"; then
  rm -f "$OUT.err"
else
  # A failed review must not be able to look like a finished one. It could, and
  # did: an expired OAuth session on 2026-08-30 produced a "review ready"
  # notification for 24 consecutive commits across four repos, because the only
  # notification that said anything specific was the "Looks good to me" one and
  # everything else — including total failure — fell through to "review ready".
  # Nothing surfaced until someone opened last-review.md by hand ~16 hours later.
  # The exit status was already captured here; it was thrown away at the one
  # point a human would have seen it.
  # The CLI reports an auth failure on STDOUT, not stderr, so $OUT.err is
  # created empty while the reason sits in $OUT. Verified 2026-08-31 against the
  # real binary: exit 1, stderr empty, "Failed to authenticate ..." on stdout.
  # Read .err first, fall back to the body, and never point a human at a
  # zero-byte file.
  ERRLINE="$(grep -m1 -v '^[[:space:]]*$' "$OUT.err" 2>/dev/null | cut -c1-160)"
  if [ -z "$ERRLINE" ]; then
    ERRLINE="$(grep -v '^[[:space:]]*$' "$OUT" 2>/dev/null \
               | grep -vE '^(#|>)' | head -1 | cut -c1-160)"
  fi
  if [ -s "$OUT.err" ]; then
    WHERE="see ${OUT}.err"
  else
    rm -f "$OUT.err"
    WHERE="reason above"
  fi
  {
    echo
    echo "_(review failed — ${WHERE})_"
  } >> "$OUT"
  case "$ERRLINE" in
    *uthenticat*|*OAuth*|*oauth*|*"ogged out"*|*"og in"*)
      # Machine state, not a bad commit. Every later commit fails the same way
      # until a human re-authenticates, so name the remedy in the notification.
      notify "Claude review FAILED — not signed in" \
             "$(basename "$REPO") ${SHORT} was NOT reviewed. Run: claude auth login" ;;
    *)
      notify "Claude review FAILED" \
             "$(basename "$REPO") ${SHORT} was NOT reviewed. ${ERRLINE:-see ${OUT}.err}" ;;
  esac
  # nohup'd and disowned by post-commit, so this is invisible to git. It matters
  # when the script is run directly, including by its own test.
  exit 1
fi

if grep -q "Looks good to me" "$OUT" 2>/dev/null; then
  notify "Claude review" "$(basename "$REPO") ${SHORT}: looks good"
else
  notify "Claude review" "$(basename "$REPO") ${SHORT}: review ready"
fi

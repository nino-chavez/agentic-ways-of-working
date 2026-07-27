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
  {
    echo
    echo "_(review failed; see ${OUT}.err)_"
  } >> "$OUT"
fi

if grep -q "Looks good to me" "$OUT" 2>/dev/null; then
  notify "Claude review" "$(basename "$REPO") ${SHORT}: looks good"
else
  notify "Claude review" "$(basename "$REPO") ${SHORT}: review ready"
fi

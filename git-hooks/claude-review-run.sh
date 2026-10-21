#!/usr/bin/env bash
# Background worker: reviews a single commit with Claude Code, billed to the
# logged-in Claude subscription, and writes the result to <git-dir>/last-review.md.
#
# Args: <repo-toplevel> <commit-sha> [lock-dir]
#
# Env:
#   CLAUDE_REVIEW_DRY=1   skip the claude call (wiring test, no quota spend)
#
# Contract for consumers: the first body line of last-review.md is exactly
#   VERDICT: <WORD> <full commit sha>
# where WORD is one of
#   PASS      no significant issues                          exit 0
#   CHANGES   findings follow                                exit 0
#   INVALID   the reviewer answered but gave no verdict line exit 2
#   FAILED    the reviewer did not finish (auth, crash, ...) exit 1
# This script writes exactly one line starting with "VERDICT:"; any such line
# inside the reviewer's own text is quoted ("> VERDICT: ..."). A dry run writes
# no VERDICT line. The sha is on the line because post-commit skips a commit
# that lands while an earlier review is still running, so the file may describe
# an older commit than HEAD. Gate on HEAD with:
#   grep -qx "VERDICT: PASS $(git rev-parse HEAD)" "$(git rev-parse --git-dir)/last-review.md"

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
BODY="$OUT.body"          # the reviewer's raw reply; merged into $OUT below
SHORT="${SHA:0:7}"
SUBJECT="$(git log -1 --format='%s' "$SHA")"

notify() {  # title, body — best-effort, cross-platform
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"$2\" with title \"$1\" sound name \"Pop\"" >/dev/null 2>&1 || true
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send "$1" "$2" >/dev/null 2>&1 || true
  fi
}

# The reviewer's text, with any line of its own that looks like a verdict quoted,
# so the one VERDICT line this script writes is the only one a grep can find.
quoted_body() { sed 's/^VERDICT:/> VERDICT:/' "$BODY"; }

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

The very first line of your reply must be exactly one of these two lines, with
nothing before it:
VERDICT: PASS
VERDICT: CHANGES
Use PASS when there are no significant issues; nothing more is required after it.
Use CHANGES when there is at least one finding, and list the findings after a
blank line. Output GitHub-flavored markdown only — no preamble.
EOF

: > "$BODY"
if claude -p "$PROMPT" \
     --permission-mode default \
     --allowedTools "Bash(git show:*),Bash(git diff:*),Bash(git log:*),Bash(git blame:*),Read,Glob,Grep" \
     >> "$BODY" 2>>"$OUT.err"; then
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
  # created empty while the reason sits in the body. Verified 2026-08-31 against
  # the real binary: exit 1, stderr empty, "Failed to authenticate ..." on stdout.
  # Read .err first, fall back to the body, and never point a human at a
  # zero-byte file.
  ERRLINE="$(grep -m1 -v '^[[:space:]]*$' "$OUT.err" 2>/dev/null | cut -c1-160)"
  if [ -z "$ERRLINE" ]; then
    ERRLINE="$(grep -v '^[[:space:]]*$' "$BODY" 2>/dev/null | head -1 | cut -c1-160)"
  fi
  if [ -s "$OUT.err" ]; then
    WHERE="see ${OUT}.err"
  else
    rm -f "$OUT.err"
    WHERE="reason above"
  fi
  # The reply is kept for the human, but a reply that got as far as "VERDICT:
  # PASS" before the CLI died must not read as PASS to a script: FAILED goes
  # first and the reply's own verdict line is quoted.
  {
    echo "VERDICT: FAILED ${SHA}"
    echo
    quoted_body
    echo
    echo "_(review failed — ${WHERE})_"
  } >> "$OUT"
  rm -f "$BODY"
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

# The verdict is the first non-blank line of the reply, and nothing else. A
# reply that opens with anything else is INVALID: the review text is kept for
# the human, but no consumer may read it as PASS. Before this contract the only
# machine-readable signal was a grep for the phrase "Looks good to me", so a
# review with findings and a review that ignored the format were the same
# "review ready" to every script downstream.
FIRST="$(grep -m1 -v '^[[:space:]]*$' "$BODY" 2>/dev/null)"
case "$FIRST" in
  "VERDICT: PASS")    VERDICT=PASS ;;
  "VERDICT: CHANGES") VERDICT=CHANGES ;;
  *)                  VERDICT=INVALID ;;
esac

if [ "$VERDICT" = INVALID ]; then
  {
    echo "VERDICT: INVALID ${SHA}"
    echo
    echo "_(the reviewer gave no \`VERDICT: PASS\` or \`VERDICT: CHANGES\` first line; treat this review as not finished)_"
    echo
    quoted_body
  } >> "$OUT"
  rm -f "$BODY"
  notify "Claude review INVALID — no verdict line" \
         "$(basename "$REPO") ${SHORT}: review text saved, but it cannot be read as PASS"
  exit 2
fi

# PASS or CHANGES. This script's line, with the sha, replaces the reviewer's bare
# verdict line; the rest of the reply follows with any stray verdict lines quoted.
{
  echo "VERDICT: ${VERDICT} ${SHA}"
  sed '/./,$!d' "$BODY" | tail -n +2 | sed 's/^VERDICT:/> VERDICT:/'
} >> "$OUT"
rm -f "$BODY"

if [ "$VERDICT" = PASS ]; then
  notify "Claude review" "$(basename "$REPO") ${SHORT}: looks good (VERDICT: PASS)"
else
  notify "Claude review" "$(basename "$REPO") ${SHORT}: review ready (VERDICT: CHANGES)"
fi

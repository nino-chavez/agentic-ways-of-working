#!/usr/bin/env bash
# Regression test for the Agent-Session trailer in prepare-commit-msg.
#
#   bash git-hooks/prepare-commit-msg.test.sh
#
# Builds throwaway repos whose core.hooksPath holds only this hook, commits
# under different session environments, and checks the trailers that land.
# HOOK=<path> runs the same assertions against another copy of the hook.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="${HOOK:-$HERE/prepare-commit-msg}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

HOOKS="$TMP/hooks"
mkdir -p "$HOOKS"
cp "$HOOK" "$HOOKS/prepare-commit-msg"
chmod +x "$HOOKS/prepare-commit-msg"
ln -s prepare-commit-msg "$HOOKS/commit-msg"

CID="11111111-2222-3333-4444-555555555555"
XID="019a0000-aaaa-7bbb-8ccc-dddddddddddd"
fails=0
pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

# Run git with no inherited session ids; each case adds its own.
g() { env -u CLAUDE_CODE_SESSION_ID -u CODEX_THREAD_ID -u AGENT_SESSION_TRAILER \
        GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 "$@"; }

new_repo() {
  local r="$TMP/$1"
  g git init -q -b main "$r"
  g git -C "$r" config core.hooksPath "$HOOKS"
  g git -C "$r" config user.name t
  g git -C "$r" config user.email t@example.com
  g git -C "$r" commit -q --allow-empty -m root
  echo "$r"
}
trailers() { git -C "$1" log -1 --format='%(trailers:key=Agent-Session,valueonly)' | sed '/^$/d'; }

R="$(new_repo plain)"

g git -C "$R" commit -q --allow-empty -m none
[ -z "$(trailers "$R")" ] && pass "no session env: no trailer" || fail "no session env: no trailer"

g CLAUDE_CODE_SESSION_ID="$CID" git -C "$R" commit -q --allow-empty -m claude
[ "$(trailers "$R")" = "claude:$CID" ] && pass "claude session id" || fail "claude session id: got '$(trailers "$R")'"

g CODEX_THREAD_ID="$XID" git -C "$R" commit -q --allow-empty -m codex
[ "$(trailers "$R")" = "codex:$XID" ] && pass "codex thread id" || fail "codex thread id: got '$(trailers "$R")'"

g CODEX_THREAD_ID="$XID" CLAUDE_CODE_SESSION_ID="$CID" git -C "$R" commit -q --allow-empty -m nested
[ "$(trailers "$R" | wc -l | tr -d ' ')" = 2 ] && pass "nested harness: both ids" || fail "nested harness: both ids"

g CLAUDE_CODE_SESSION_ID="$CID" git -C "$R" commit -q --allow-empty -m body -m "Some body."
subj="$(git -C "$R" log -1 --format=%s)"; body="$(git -C "$R" log -1 --format=%b)"
[ "$subj" = body ] && printf '%s' "$body" | grep -q '^Some body\.$' && pass "subject and body untouched" \
  || fail "subject and body untouched"

g CLAUDE_CODE_SESSION_ID="$CID" git -C "$R" commit -q --allow-empty --amend --no-edit
[ "$(trailers "$R" | wc -l | tr -d ' ')" = 1 ] && pass "amend, same session: no duplicate" || fail "amend, same session: no duplicate"

g CODEX_THREAD_ID="$XID" git -C "$R" commit -q --allow-empty --amend --no-edit
[ "$(trailers "$R" | wc -l | tr -d ' ')" = 2 ] && pass "amend, other session: added" || fail "amend, other session: added"

g CLAUDE_CODE_SESSION_ID="$CID" AGENT_SESSION_TRAILER=0 git -C "$R" commit -q --allow-empty -m optout
[ -z "$(trailers "$R")" ] && pass "AGENT_SESSION_TRAILER=0 opts out" || fail "AGENT_SESSION_TRAILER=0 opts out"

g CLAUDE_CODE_SESSION_ID=$'x\nSigned-off-by: evil' git -C "$R" commit -q --allow-empty -m inject
[ -z "$(trailers "$R")" ] && ! git -C "$R" log -1 --format=%B | grep -q evil \
  && pass "malformed id rejected" || fail "malformed id rejected"

# A message written in the editor gets the trailer (commit-msg's pass), and
# so does -m under --no-verify (prepare-commit-msg's pass).
printf '#!/bin/sh\nprintf "from editor\\n" > "$1"\n' > "$TMP/editor"; chmod +x "$TMP/editor"
g CLAUDE_CODE_SESSION_ID="$CID" GIT_EDITOR="$TMP/editor" git -C "$R" commit -q --allow-empty
[ "$(git -C "$R" log -1 --format=%s)" = "from editor" ] && [ "$(trailers "$R")" = "claude:$CID" ] \
  && pass "editor-composed message" || fail "editor-composed message: got '$(trailers "$R")'"
g CLAUDE_CODE_SESSION_ID="$CID" git -C "$R" commit -q --allow-empty --no-verify -m noverify
[ "$(trailers "$R")" = "claude:$CID" ] && pass "-m with --no-verify" || fail "-m with --no-verify"
g CLAUDE_CODE_SESSION_ID="$CID" git -C "$R" commit -q --allow-empty -v -m verbose
[ "$(trailers "$R")" = "claude:$CID" ] && ! git -C "$R" log -1 --format=%B | grep -q '>8' \
  && pass "-v scissors template" || fail "-v scissors template"

# An empty message still aborts, as it does without the hook.
before="$(git -C "$R" rev-parse HEAD)"
g CLAUDE_CODE_SESSION_ID="$CID" git -C "$R" commit -q --allow-empty -m "" 2>/dev/null
g CLAUDE_CODE_SESSION_ID="$CID" GIT_EDITOR=true git -C "$R" commit -q --allow-empty 2>/dev/null
echo change > "$R/f"; g git -C "$R" add f
g CLAUDE_CODE_SESSION_ID="$CID" GIT_EDITOR=true git -C "$R" commit -q -v 2>/dev/null
g git -C "$R" reset -q --hard
[ "$(git -C "$R" rev-parse HEAD)" = "$before" ] && pass "empty message still aborts (incl. -v with a diff)" \
  || fail "empty message still aborts"

# Rebase replay keeps the original commit's attribution.
g git -C "$R" checkout -q -b side
g git -C "$R" commit -q --allow-empty -m "side work"
g git -C "$R" checkout -q main
g git -C "$R" commit -q --allow-empty -m "main moves"
g git -C "$R" checkout -q side
g CLAUDE_CODE_SESSION_ID="$CID" git -C "$R" rebase -q --keep-empty main 2>/dev/null
[ "$(git -C "$R" log -1 --format=%s)" = "side work" ] && [ -z "$(trailers "$R")" ] \
  && pass "rebase replay: no trailer" || fail "rebase replay: no trailer"
g git -C "$R" checkout -q main

# Linked worktree, with a repo-local hook in the common dir that must still run.
W="$TMP/wt"
g git -C "$R" worktree add -q "$W" -b wt
printf '#!/bin/sh\necho local-ran >> "%s/local.log"\n' "$TMP" > "$R/.git/hooks/prepare-commit-msg"
printf '#!/bin/sh\necho local-cm-ran >> "%s/local.log"\n' "$TMP" > "$R/.git/hooks/commit-msg"
chmod +x "$R/.git/hooks/commit-msg"
chmod +x "$R/.git/hooks/prepare-commit-msg"
g CLAUDE_CODE_SESSION_ID="$CID" git -C "$W" commit -q --allow-empty -m "in worktree"
[ "$(trailers "$W")" = "claude:$CID" ] && pass "linked worktree" || fail "linked worktree"
grep -q local-ran "$TMP/local.log" 2>/dev/null && grep -q local-cm-ran "$TMP/local.log" \
  && pass "chains to repo-local hooks of both names" || fail "chains to repo-local hooks of both names"
rm -f "$R/.git/hooks/commit-msg"

# A repo-local hook that rejects the commit still rejects it.
printf '#!/bin/sh\nexit 1\n' > "$R/.git/hooks/prepare-commit-msg"
before="$(git -C "$R" rev-parse HEAD)"
g CLAUDE_CODE_SESSION_ID="$CID" git -C "$R" commit -q --allow-empty -m rejected 2>/dev/null
[ "$(git -C "$R" rev-parse HEAD)" = "$before" ] && pass "local hook failure aborts commit" \
  || fail "local hook failure aborts commit"
rm -f "$R/.git/hooks/prepare-commit-msg"

echo
[ "$fails" -eq 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }

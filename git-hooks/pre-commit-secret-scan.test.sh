#!/usr/bin/env bash
# Regression test for the pre-commit secret scan.
#
#   bash git-hooks/pre-commit-secret-scan.test.sh
#
# Builds a throwaway repo whose core.hooksPath holds only this hook, stages one
# case at a time and checks whether the commit is refused.
# HOOK=<path> runs the same assertions against another copy of the hook.
#
# Every credential-shaped value is assembled at run time from random
# characters. No key-shaped literal may land in this file: the hook itself and
# GitGuardian would both flag one.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="${HOOK:-$HERE/pre-commit-secret-scan}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

HOOKS="$TMP/hooks"
mkdir -p "$HOOKS"
cp "$HOOK" "$HOOKS/pre-commit"
chmod +x "$HOOKS/pre-commit"

fails=0
pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

# Run git with no global or system config, so the machine's own hooks, rename
# settings and ignore files stay out of the result.
g() { env GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 "$@"; }

R="$TMP/repo"
g git init -q -b main "$R"
g git -C "$R" config core.hooksPath "$HOOKS"
g git -C "$R" config user.name t
g git -C "$R" config user.email t@example.com
g git -C "$R" commit -q --allow-empty -m root

# rnd <length> <tr character set>: a random string.
rnd() { LC_ALL=C tr -dc "$2" < /dev/urandom | head -c "$1"; }
AN='A-Za-z0-9'             # letters and digits
HEX='0-9a-f'
URL='A-Za-z0-9_-'          # base64url
B58='1-9A-HJ-NP-Za-km-z'   # base58: no 0, O, I or l
known_key() { printf 'ghp_%s' "$(rnd 36 "$AN")"; }   # a shape the hook has always known

n=0
OUT=""
# stage <text>: write the text to a new file and stage it. Sets $FILE.
stage() { n=$((n + 1)); FILE="case$n.txt"; printf '%s\n' "$1" > "$R/$FILE"; g git -C "$R" add -- "$FILE"; }
tidy() { g git -C "$R" reset -q --hard; g git -C "$R" clean -qfd; }
status_letter() { g git -C "$R" diff --cached --name-status | cut -c1; }

# The case is already staged. Both leave the work tree clean and the hook's
# output in $OUT.
expect_block() {
  if OUT="$(g git -C "$R" commit -q -m "$1" 2>&1)"; then
    fail "$1: committed, expected a block"
    g git -C "$R" reset -q --hard HEAD~1   # so a missed block cannot change later cases
  elif printf '%s' "$OUT" | grep -q 'BLOCKED'; then
    pass "$1"
  else
    fail "$1: the commit failed without the hook's message"
  fi
  tidy
}
expect_pass() {
  if OUT="$(g git -C "$R" commit -q -m "$1" 2>&1)"; then
    pass "$1"
  else
    fail "$1: refused"
    printf '%s\n' "$OUT" | grep '→' | sed 's/^/     /'
  fi
  tidy
}
blocks() { stage "API_KEY=$2"; expect_block "$1"; }
passes() { stage "$2"; expect_pass "$1"; }

# --- Control: the harness can see a block, and what the block prints. ---------
key="$(known_key)"
stage "API_KEY=$key"
file="$FILE"
expect_block "control: a GitHub token is blocked"
printf '%s' "$OUT" | grep -qF "$file" && pass "block message names the file" \
  || fail "block message names the file"
printf '%s' "$OUT" | grep -qF "${key:12}" && fail "block message prints the key" \
  || pass "block message shows only the start of the key"

# --- One key per provider added on 2026-11-19. --------------------------------
blocks "Google API key"                    "AIza$(rnd 35 "$URL")"
blocks "Google key bound to a service account" "AQ.$(rnd 50 "$URL")"
blocks "OpenRouter"                        "sk-or-v1-$(rnd 64 "$HEX")"
blocks "Perplexity"                        "pplx-$(rnd 48 "$AN")"
blocks "Resend"                            "re_$(rnd 8 "$B58")_$(rnd 24 "$B58")"
blocks "Supabase secret key"               "sb_secret_$(rnd 22 "$URL")_$(rnd 8 "$URL")"
blocks "Stripe restricted live key"        "rk_live_$(rnd 99 "$AN")"
blocks "OpenAI legacy key"                 "sk-$(rnd 48 "$AN")"
blocks "OpenAI service-account key"        "sk-svcacct-$(rnd 60 "$URL")"
blocks "OpenAI admin key"                  "sk-admin-$(rnd 60 "$URL")"

# --- Staged paths the scan used to skip. --------------------------------------
# A key added to a file git records as renamed.
seq 1 40 | sed 's/^/line /' > "$R/before.txt"
g git -C "$R" add before.txt
g git -C "$R" commit -q -m "a file to rename"
g git -C "$R" mv before.txt after.txt
printf 'API_KEY=%s\n' "$(known_key)" >> "$R/after.txt"
g git -C "$R" add after.txt
[ "$(status_letter)" = R ] || fail "setup: git did not record a rename, so the next case proves nothing"
expect_block "key added to a renamed file"

g git -C "$R" mv before.txt moved.txt
[ "$(status_letter)" = R ] || fail "setup: git did not record a rename, so the next case proves nothing"
expect_pass "rename with no key"

# A symlink replaced by a regular file (git status T).
ln -s moved.txt "$R/link"
g git -C "$R" add link
g git -C "$R" commit -q -m "a symlink"
rm "$R/link"
printf 'API_KEY=%s\n' "$(known_key)" > "$R/link"
g git -C "$R" add link
[ "$(status_letter)" = T ] || fail "setup: git did not record a type change, so the next case proves nothing"
expect_block "key in a symlink replaced by a file"

# Names git prints in quoted form unless asked for -z.
named() {  # named <case name> <file name>
  printf 'API_KEY=%s\n' "$(known_key)" > "$R/$2"
  g git -C "$R" add -- "$2"
  expect_block "$1"
}
named "key in a file whose name has a space" "with space.txt"
named "key in a file whose name is not ASCII" "résumé.env"
named "key in a file whose name has a quote and a tab" $'quote"and\ttab.txt'

# --- What must still pass. ----------------------------------------------------
passes "line carrying the allowlist pragma" "FAKE=$(known_key)  # pragma: allowlist secret"

stage "FAKE=$(known_key)  # pragma: allowlist secret
REAL=$(known_key)"
expect_block "the pragma exempts only its own line"

# Key-shaped strings that are not keys. Each is one edit away from a pattern.
passes "not a key: word ending in re_ before a Resend-shaped tail" "id=store_$(rnd 8 "$B58")_$(rnd 24 "$B58")"
passes "not a key: Resend-shaped head with a longer tail"          "id=re_$(rnd 8 "$B58")_$(rnd 32 "$B58")"
passes "not a key: word ending in sk- before 48 characters"        "ref=task-$(rnd 48 "$HEX")"
passes "not a key: FAQ. before a long token"                       "see FAQ.$(rnd 50 "$URL")"
passes "not a key: Stripe test-mode key"                           "STRIPE=sk_test_$(rnd 99 "$AN")"
passes "not a key: Supabase publishable key"                       "KEY=sb_publishable_$(rnd 22 "$URL")_$(rnd 8 "$URL")"
passes "not a key: masked Supabase secret key"                     "KEY=sb_secret_$(rnd 5 "$AN")··························"
passes "not a key: short placeholder after a provider prefix"      "PERPLEXITY_API_KEY=pplx-$(rnd 16 "$HEX")"
passes "not a key: commit id and UUID"                             "sha=$(rnd 40 "$HEX") id=$(uuidgen)"

# Removing a key is never refused.
printf 'API_KEY=%s\n' "$(known_key)" > "$R/leaked.txt"
g git -C "$R" add leaked.txt
g git -C "$R" commit -q --no-verify -m "a key that got past the hook"
g git -C "$R" rm -q leaked.txt
expect_pass "deleting a file that holds a key"

echo
[ "$fails" -eq 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }

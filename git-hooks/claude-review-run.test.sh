#!/usr/bin/env bash
# Regression test for claude-review-run.sh's failure signalling.
#
# Exists because the script could not previously tell a human that a review had
# failed: every outcome except "Looks good to me" produced the same "review
# ready" notification, so 24 consecutive unreviewed commits on 2026-08-30 looked
# exactly like 24 reviewed ones.
#
# Stubs `claude` (the thing that fails) and `osascript` (where a human would see
# it), then asserts on the notification text and the exit status.
#
#   bash git-hooks/claude-review-run.test.sh

set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUNNER="$HERE/claude-review-run.sh"
pass=0; fail=0

setup() {                       # $1 = stub body for `claude`
  TMP="$(mktemp -d)"
  mkdir -p "$TMP/bin" "$TMP/repo"
  printf '%s\n' '#!/usr/bin/env bash' "$1" > "$TMP/bin/claude"
  # Record notifications instead of showing them.
  printf '%s\n' '#!/usr/bin/env bash' 'printf "%s\n" "$*" >> "$NOTIFY_LOG"' \
    > "$TMP/bin/osascript"
  chmod +x "$TMP/bin/claude" "$TMP/bin/osascript"
  export NOTIFY_LOG="$TMP/notify.log"; : > "$NOTIFY_LOG"
  git -C "$TMP/repo" init -q
  git -C "$TMP/repo" -c user.email=t@t -c user.name=t \
      -c core.hooksPath="$TMP/nohooks" commit -q --allow-empty -m "test commit"
  SHA="$(git -C "$TMP/repo" rev-parse HEAD)"
}
teardown() { rm -rf "$TMP"; }

_run() {
  PATH="$TMP/bin:$PATH" bash "$RUNNER" "$TMP/repo" "$SHA" >/dev/null 2>&1
  RC=$?
  MD="$(cat "$TMP/repo/.git/last-review.md" 2>/dev/null)"
  NOTE="$(cat "$NOTIFY_LOG")"
}

_assert() {                     # name, haystack, want, label, want-rc
  if [[ "$2" == *"$3"* ]] && [ "$RC" -eq "$5" ]; then
    echo "  PASS  $1"; pass=$((pass+1))
  else
    echo "  FAIL  $1"
    echo "        want in $4: $3   (exit $5)"
    echo "        got: ${2:-<none>}   (exit $RC)"
    fail=$((fail+1))
  fi
}

# assert on what the human is shown
check()    { _run; _assert "$1" "$NOTE" "$2" "notification" "$3"; }
# assert on what the human opens
check_md() { _run; _assert "$1" "$MD"   "$2" "last-review.md" "$3"; }

echo "claude-review-run.sh — failure signalling"

setup 'echo "Failed to authenticate: OAuth session expired and could not be refreshed" >&2; exit 1'
check "auth failure names the remedy"        "claude auth login"  1
teardown

setup 'echo "Failed to authenticate: OAuth session expired" >&2; exit 1'
check "auth failure is not called ready"     "FAILED"             1
teardown

# The real CLI reports auth failures on stdout with stderr empty (verified
# 2026-08-31), so .err is created zero-byte. The first version of this test
# stubbed stderr only, and passed against a fix that could not read the reason.
setup 'echo "Failed to authenticate: OAuth session expired and could not be refreshed"; exit 1'
check "auth error on STDOUT still names remedy" "claude auth login"  1
teardown

setup 'echo "Failed to authenticate: OAuth session expired"; exit 1'
check_md "empty .err is not cited to the reader" "reason above"     1
teardown

setup 'echo "boom: model overloaded" >&2; exit 1'
check "generic failure reports the reason"   "model overloaded"   1
teardown

setup 'echo "boom: model overloaded" >&2; exit 1'
check_md "a non-empty .err IS cited"         "last-review.md.err" 1
teardown

setup 'echo "Looks good to me!"; exit 0'
check "clean review still says looks good"   "looks good"         0
teardown

setup 'echo "src/a.ts:12 — off-by-one"; exit 0'
check "review with findings says ready"      "review ready"       0
teardown

echo
echo "  ${pass} passed, ${fail} failed"
[ "$fail" -eq 0 ]

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

check() {                       # name, expected-substring, expected-exit
  local name="$1" want="$2" want_rc="$3"
  PATH="$TMP/bin:$PATH" bash "$RUNNER" "$TMP/repo" "$SHA" >/dev/null 2>&1
  local rc=$?
  local got; got="$(cat "$NOTIFY_LOG")"
  if [[ "$got" == *"$want"* ]] && [ "$rc" -eq "$want_rc" ]; then
    echo "  PASS  $name"; pass=$((pass+1))
  else
    echo "  FAIL  $name"
    echo "        want substring: $want   (exit $want_rc)"
    echo "        got notification: ${got:-<none>}   (exit $rc)"
    fail=$((fail+1))
  fi
}

echo "claude-review-run.sh — failure signalling"

setup 'echo "Failed to authenticate: OAuth session expired and could not be refreshed" >&2; exit 1'
check "auth failure names the remedy"        "claude auth login"  1
teardown

setup 'echo "Failed to authenticate: OAuth session expired" >&2; exit 1'
check "auth failure is not called ready"     "FAILED"             1
teardown

setup 'echo "boom: model overloaded" >&2; exit 1'
check "generic failure reports the reason"   "model overloaded"   1
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

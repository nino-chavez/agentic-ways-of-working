#!/usr/bin/env bash
# Regression test for claude-review-run.sh's signalling: failures and verdicts.
#
# Exists because the script could not previously tell a human that a review had
# failed: every outcome except "Looks good to me" produced the same "review
# ready" notification, so 24 consecutive unreviewed commits on 2026-08-30 looked
# exactly like 24 reviewed ones.
#
# Extended 2026-10-21 for the verdict contract: the first body line of
# last-review.md is exactly `VERDICT: <PASS|CHANGES|INVALID|FAILED> <sha>`, so
# a script downstream can gate on it and on the commit it belongs to. A reply
# that ignores the format is INVALID (exit 2), never PASS; a run that did not
# finish is FAILED (exit 1) even when the reply got as far as "VERDICT: PASS".
# The script writes exactly one VERDICT line; verdict-shaped lines inside the
# reviewer's text are quoted. Before the contract the only machine-readable
# signal was a grep for the phrase "Looks good to me".
#
# Stubs `claude` (the thing that fails) and `osascript` (where a human would see
# it), then asserts on the notification text, the file and the exit status.
#
#   bash git-hooks/claude-review-run.test.sh
#   RUNNER=/path/to/other/claude-review-run.sh bash git-hooks/claude-review-run.test.sh
#       runs the same assertions against another copy of the runner; used once
#       to prove the verdict cases fail against the pre-contract script.

set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUNNER="${RUNNER:-$HERE/claude-review-run.sh}"
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
  # First non-blank line after the header (the header ends with the "> commit" line).
  FIRSTBODY="$(awk 'hdr && NF {print; exit} /^> commit/ {hdr=1}' "$TMP/repo/.git/last-review.md" 2>/dev/null)"
  # How many lines a naive consumer grep would match.
  NVERDICT="$(grep -c '^VERDICT:' <<<"$MD")"
  NPASS="$(grep -c '^VERDICT: PASS' <<<"$MD")"
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

_assert_eq() {                  # name, got, want, label, want-rc
  if [ "$2" = "$3" ] && [ "$RC" -eq "$5" ]; then
    echo "  PASS  $1"; pass=$((pass+1))
  else
    echo "  FAIL  $1"
    echo "        want $4: $3   (exit $5)"
    echo "        got:  ${2:-<none>}   (exit $RC)"
    fail=$((fail+1))
  fi
}

# assert on what the human is shown
check()    { _run; _assert "$1" "$NOTE" "$2" "notification" "$3"; }
# assert on what the human opens
check_md() { _run; _assert "$1" "$MD"   "$2" "last-review.md" "$3"; }
# assert on what a script reads: the first body line, exactly
check_verdict() { _run; _assert_eq "$1" "$FIRSTBODY" "$2" "first body line" "$3"; }
# assert on how many lines a consumer grep for '^VERDICT:' / '^VERDICT: PASS' finds
check_counts()  { _run; _assert_eq "$1" "$NVERDICT/$NPASS" "$2" "verdict-lines/pass-lines" "$3"; }

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

setup 'echo "boom: model overloaded" >&2; exit 1'
check_verdict "a failed run is VERDICT: FAILED <sha>" "VERDICT: FAILED $SHA" 1
teardown

# The reply got as far as a verdict, then the CLI died (crash after output, a
# turn or hook error). Found by the post-commit review of 3b3d4c2: the first
# version copied the reply verbatim, so this run read as PASS to a grep.
setup 'echo "VERDICT: PASS"; exit 1'
check_verdict "a reply then a crash is FAILED, not PASS" "VERDICT: FAILED $SHA" 1
teardown

setup 'echo "VERDICT: PASS"; exit 1'
check_counts "a crashed PASS reply leaves no bare PASS line" "1/0" 1
teardown

echo
echo "claude-review-run.sh — verdict contract"

setup 'echo "VERDICT: PASS"; exit 0'
check "PASS is called looks good"            "looks good"         0
teardown

setup 'echo "VERDICT: PASS"; exit 0'
check_verdict "PASS carries the commit sha"  "VERDICT: PASS $SHA" 0
teardown

setup 'echo "VERDICT: PASS"; exit 0'
check_counts "the reviewer's bare verdict line is replaced, not duplicated" "1/1" 0
teardown

setup 'printf "VERDICT: CHANGES\n\n- src/a.ts:12 off-by-one\n"; exit 0'
check "CHANGES is called review ready"       "review ready"       0
teardown

setup 'printf "VERDICT: CHANGES\n\n- src/a.ts:12 off-by-one\n"; exit 0'
check_verdict "CHANGES carries the commit sha" "VERDICT: CHANGES $SHA" 0
teardown

setup 'printf "VERDICT: CHANGES\n\n- src/a.ts:12 off-by-one\n"; exit 0'
check_md "CHANGES keeps the findings"        "off-by-one"         0
teardown

setup 'printf "\n\nVERDICT: PASS\n"; exit 0'
check_verdict "leading blank lines do not hide a verdict" "VERDICT: PASS $SHA" 0
teardown

# The pre-contract sentinel. It is review text without a verdict, so a script
# must not read it as PASS, and the human must be told the format was ignored.
setup 'echo "Looks good to me!"; exit 0'
check "legacy 'Looks good to me' is INVALID, not PASS" "INVALID"  2
teardown

setup 'echo "Looks good to me!"; exit 0'
check_verdict "INVALID carries the commit sha" "VERDICT: INVALID $SHA" 2
teardown

setup 'echo "Looks good to me!"; exit 0'
check_md "INVALID keeps the review text"     "Looks good to me!"  2
teardown

setup 'printf "Here is my review.\nVERDICT: PASS\n"; exit 0'
check_verdict "a preamble before the verdict is INVALID" "VERDICT: INVALID $SHA" 2
teardown

setup 'printf "Here is my review.\nVERDICT: PASS\n"; exit 0'
check_counts "a verdict buried in the text is quoted, not matchable" "1/0" 2
teardown

setup 'echo "VERDICT: pass"; exit 0'
check_verdict "wrong case is INVALID"        "VERDICT: INVALID $SHA" 2
teardown

setup 'echo "src/a.ts:12 — off-by-one"; exit 0'
check "findings with no verdict line are INVALID" "INVALID"       2
teardown

echo
echo "  ${pass} passed, ${fail} failed"
[ "$fail" -eq 0 ]

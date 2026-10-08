#!/usr/bin/env bash
# Runs the SDK-host canary once: a fresh scratch folder holding build/ with three
# files, Haiku asked to delete it, and the host answering Blast Radius's question
# as <mode> says. Prints the question, the tool result, and what is left of build/.
#
# usage: run.sh <proceed|deny-late|deny|none|allow-all|bare>
#   none waits for the hold limit; run it with BLAST_RADIUS_HOLD_SECONDS=5.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mod="$(cd "$here/../.." && pwd)"
dir="$(mktemp -d "${TMPDIR:-/tmp}/blast-radius-canary.XXXXXX")"
mkdir "$dir/build"
for name in a b c; do echo "$name" > "$dir/build/$name.txt"; done
python3 "$here/sdk_host.py" "$1" "$mod" "$dir" "$here/prompt.txt"
left=$({ find "$dir/build" -type f 2>/dev/null || true; } | wc -l | tr -d ' ')
echo "build/ afterwards: $left of 3 files"

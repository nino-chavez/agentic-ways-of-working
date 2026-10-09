#!/usr/bin/env bash
# Runs the SDK-host canary once: a fresh scratch folder holding build/ with three
# files, Haiku asked to delete it, and the host answering Blast Radius's question
# as <mode> says. Prints the question, the tool result, and what is left of build/.
#
# usage: run.sh <proceed|deny-late|deny|none|allow-all|bare> [inline|script]
#   none waits for the hold limit; run it with BLAST_RADIUS_HOLD_SECONDS=5.
#   script asks for `bash delete-build.sh` instead of the inline rm: the script,
#   written beside build/, carries the rm -rf on its line 4 under a comment that
#   also says rm -rf. The hold must name line 4 and nothing else.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mod="$(cd "$here/../.." && pwd)"
dir="$(mktemp -d "${TMPDIR:-/tmp}/blast-radius-canary.XXXXXX")"
mkdir "$dir/build"
for name in a b c; do echo "$name" > "$dir/build/$name.txt"; done
prompt="$here/prompt.txt"
if [ "${2:-inline}" = "script" ]; then
  printf '%s\n' '#!/bin/bash' '# Removes the build output with rm -rf. The comment is not a command.' 'set -e' 'rm -rf build' 'echo removed' > "$dir/delete-build.sh"
  prompt="$here/prompt-script.txt"
fi
python3 "$here/sdk_host.py" "$1" "$mod" "$dir" "$prompt"
left=$({ find "$dir/build" -type f 2>/dev/null || true; } | wc -l | tr -d ' ')
echo "build/ afterwards: $left of 3 files"

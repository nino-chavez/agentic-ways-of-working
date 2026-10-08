#!/usr/bin/env bash
# Compares this folder's classifier with the one at <ref> (default main): every
# command the old one holds must still be held. Run it before amending the parser.
# usage: run.sh [ref]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mod="$(cd "$here/../.." && pwd)"
base="$(mktemp "${TMPDIR:-/tmp}/blast-radius-base.XXXXXX")"
trap 'rm -f "$base"' EXIT
git -C "$mod" show "${1:-main}:mods/blast-radius/hooks/blast-radius.mjs" > "$base"
node "$here/check.mjs" "$base" "$mod/hooks/blast-radius.mjs" "$mod/tests/blast-radius.test.ts"

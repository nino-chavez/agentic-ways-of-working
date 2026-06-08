#!/usr/bin/env bash
# install.sh — set up the Claude post-commit review hook.
#
# Usage:
#   ./install.sh                 install globally (sets git core.hooksPath)
#   ./install.sh --repo [PATH]   install into one repo only (default: cwd)
#   ./install.sh --uninstall     remove the global core.hooksPath setting
#   ./install.sh --help
#
# Global install runs the hook in every repo that doesn't define its own
# core.hooksPath. Repos using Husky/lefthook set their own, so they keep
# working but won't get this hook (git honors only one hooksPath).

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="global"; REPO=""

while [ $# -gt 0 ]; do
  case "$1" in
    --global)    MODE="global"; shift ;;
    --repo)      MODE="repo"; REPO="${2:-$(pwd)}"; [ "${2:-}" ] && shift 2 || shift ;;
    --uninstall) MODE="uninstall"; shift ;;
    -h|--help)   sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

command -v git >/dev/null 2>&1 || { echo "ERROR: git not found." >&2; exit 1; }
command -v claude >/dev/null 2>&1 \
  || echo "WARN: 'claude' CLI not on PATH. Install Claude Code and log in to your subscription before the hook can run."

install_files() {  # $1 = destination dir
  local dest="$1"
  mkdir -p "$dest"
  cp "$SRC/post-commit" "$SRC/claude-review-run.sh" "$dest/"
  chmod +x "$dest/post-commit" "$dest/claude-review-run.sh"
}

case "$MODE" in
  global)
    DEST="${XDG_CONFIG_HOME:-$HOME/.config}/git-hooks"
    install_files "$DEST"
    cur="$(git config --global --get core.hooksPath || true)"
    if [ -n "$cur" ] && [ "$cur" != "$DEST" ]; then
      echo "NOTE: global core.hooksPath is already '$cur' — NOT overwriting."
      echo "      To enable this hook, either merge the scripts into that dir,"
      echo "      or run: git config --global core.hooksPath \"$DEST\""
    else
      git config --global core.hooksPath "$DEST"
      echo "Installed globally. core.hooksPath = $DEST"
    fi
    echo "Optional: copy claude-review.conf.example into a repo as .claude-review.conf to tune triggers."
    ;;
  repo)
    git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || { echo "not a git repo: $REPO" >&2; exit 1; }
    local_hp="$(git -C "$REPO" config --get core.hooksPath || true)"
    HOOKS="$(git -C "$REPO" rev-parse --git-path hooks)"
    [ -d "$REPO/.git" ] && HOOKS="$REPO/.git/hooks"
    install_files "$HOOKS"
    echo "Installed into $HOOKS"
    if [ -n "$local_hp" ]; then
      echo "WARN: core.hooksPath is set to '$local_hp' (repo-local or global, e.g. Husky),"
      echo "      which overrides .git/hooks — the hook won't fire from here. Place the two"
      echo "      scripts under '$local_hp' instead, or unset that core.hooksPath."
    fi
    ;;
  uninstall)
    cur="$(git config --global --get core.hooksPath || true)"
    if [ -n "$cur" ]; then
      git config --global --unset core.hooksPath
      echo "Removed global core.hooksPath (was '$cur')."
    else
      echo "No global core.hooksPath was set."
    fi
    echo "Per-repo installs: delete post-commit + claude-review-run.sh from that repo's hooks dir."
    ;;
esac

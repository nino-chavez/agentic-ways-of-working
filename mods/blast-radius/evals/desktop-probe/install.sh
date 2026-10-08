#!/usr/bin/env bash
# Installs the desktop probe into a Claude Code session's hot-reload folder.
# The session must have loaded the plugin-authoring skill; the person then answers
# "Enable hot reloading for this session?". After that, a Bash call containing
# BR_DESKTOP_PROBE is refused with what the host gives a mod: isInteractive, the
# surface list at load, at the call and after, whether a pane was placed, and the
# answer to a question that asks the person whether they can see that pane.
#
# The trigger is matched anywhere in the command text, a heredoc included, so keep
# it out of other commands while the probe is loaded. Move the folder out of
# ~/.claude/dev-mods/<session-id>/ to unload it when the next turn ends.
#
# usage: install.sh <session-id>
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
dest="$HOME/.claude/dev-mods/$1/desktop-probe"
mkdir -p "$dest/.claude-plugin" "$dest/hooks"
printf '%s\n' '{ "name": "desktop-probe", "version": "0.1.0", "description": "Reports what this host gives a mod: surfaces, pane placement, and the question dialog." }' > "$dest/.claude-plugin/plugin.json"
printf '%s\n' '{ "modules": ["./register.ts"] }' > "$dest/hooks/hooks.json"
cp "$here/register.ts" "$dest/hooks/register.ts"
echo "installed: $dest"

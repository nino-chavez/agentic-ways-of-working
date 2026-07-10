#!/usr/bin/env bash
#
# install.sh — install these ways of working into a Claude Code setup.
#
# What it does (all idempotent, all reversible):
#   1. Symlinks skills/  -> ~/.claude/skills/
#   2. Symlinks commands/ -> ~/.claude/commands/
#   3. Symlinks hooks/    -> ~/.claude/hooks/
#   4. Registers the hooks in ~/.claude/settings.json (backed up first)
#   5. Tells you how to point your harness's rules file at principles/working-style.md
#
# Symlinks (not copies) so `git pull` updates everything in place. Pass --copy
# to copy instead. Nothing here touches secrets. Hooks are stdlib-only; the one
# platform-specific piece is read-guard's image branch (macOS `sips`), which
# fails open on other platforms.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
MODE="link"   # link | copy
[[ "${1:-}" == "--copy" ]] && MODE="copy"

say()  { printf '  %s\n' "$1"; }
head() { printf '\n== %s ==\n' "$1"; }

place() {  # place <src> <dest>
  local src="$1" dest="$2"
  mkdir -p "$(dirname "$dest")"
  if [[ -e "$dest" || -L "$dest" ]]; then rm -rf "$dest"; fi
  if [[ "$MODE" == "copy" ]]; then cp -R "$src" "$dest"; else ln -s "$src" "$dest"; fi
  say "$(basename "$dest")"
}

head "Skills -> $CLAUDE_DIR/skills"
for d in "$REPO_DIR"/skills/*/; do
  place "$d" "$CLAUDE_DIR/skills/$(basename "$d")"
done

head "Commands -> $CLAUDE_DIR/commands"
for f in "$REPO_DIR"/commands/*; do
  place "$f" "$CLAUDE_DIR/commands/$(basename "$f")"
done

head "Hooks -> $CLAUDE_DIR/hooks"
for f in "$REPO_DIR"/hooks/*.py; do
  place "$f" "$CLAUDE_DIR/hooks/$(basename "$f")"
done

head "Statusline -> $CLAUDE_DIR/statusline.py"
place "$REPO_DIR/statusline.py" "$CLAUDE_DIR/statusline.py"

head "Wiring hooks into settings.json"
SETTINGS="$CLAUDE_DIR/settings.json"
[[ -f "$SETTINGS" ]] || echo '{}' > "$SETTINGS"
cp "$SETTINGS" "$SETTINGS.bak.$(date +%s)"
say "backed up to $SETTINGS.bak.*"

python3 - "$SETTINGS" "$CLAUDE_DIR" <<'PY'
import json, sys
settings_path, claude_dir = sys.argv[1], sys.argv[2]
with open(settings_path) as f:
    cfg = json.load(f)

hooks = cfg.setdefault("hooks", {})

def ensure(event, command, timeout, matcher=""):
    blocks = hooks.setdefault(event, [])
    # find/create the block for this matcher (matcher "" = all tools)
    block = next((b for b in blocks if b.get("matcher", "") == matcher), None)
    if block is None:
        block = {"matcher": matcher, "hooks": []}
        blocks.append(block)
    inner = block.setdefault("hooks", [])
    if any(h.get("command") == command for h in inner):
        return False
    inner.append({"type": "command", "command": command, "timeout": timeout})
    return True

def drop(event, command, matcher=""):
    """Remove a previously-wired command from a given matcher block (migration)."""
    for b in hooks.get(event, []):
        if b.get("matcher", "") == matcher:
            b["hooks"] = [h for h in b.get("hooks", []) if h.get("command") != command]

GUARD_MATCHER = "Bash|Write|Edit|MultiEdit"
# Migrate legacy narrow install: the guard used to gate only matcher="Bash";
# it now also gates file edits. Drop the old block's command before re-adding.
drop("PreToolUse", f"python3 {claude_dir}/hooks/worktree-guard.py check", matcher="Bash")

added = []
if ensure("SessionStart",     f"python3 {claude_dir}/hooks/blueprint-session-start.py", 10): added.append("SessionStart:blueprint-session-start")
if ensure("Stop",             f"python3 {claude_dir}/hooks/anti-hesitation.py", 5):         added.append("Stop:anti-hesitation")
if ensure("UserPromptSubmit", f"python3 {claude_dir}/hooks/campsite.py", 3):                added.append("UserPromptSubmit:campsite")
# Worktree isolation guard: register heartbeat at session boundaries + gate git
# ops (Bash) and file edits (Write/Edit/MultiEdit) against shared main checkouts.
if ensure("SessionStart",     f"python3 {claude_dir}/hooks/worktree-guard.py register", 5):                       added.append("SessionStart:worktree-guard")
if ensure("PreToolUse",       f"python3 {claude_dir}/hooks/worktree-guard.py check", 5, matcher=GUARD_MATCHER):   added.append("PreToolUse[edit+bash]:worktree-guard")
if ensure("SessionEnd",       f"python3 {claude_dir}/hooks/worktree-guard.py unregister", 5):                     added.append("SessionEnd:worktree-guard")
# Read-cost guard: bounce oversized images (with a downscaled copy) and
# redundant re-reads of unchanged files. Image branch needs macOS sips;
# fails open elsewhere.
if ensure("PreToolUse",       f"python3 {claude_dir}/hooks/read-guard.py", 15, matcher="Read"):                   added.append("PreToolUse[Read]:read-guard")

# Context-usage statusline (session-hygiene visibility). Only set if the user
# has no statusline configured — never clobber an existing one.
if "statusLine" not in cfg:
    cfg["statusLine"] = {"type": "command", "command": f"python3 {claude_dir}/statusline.py"}
    added.append("statusLine:context-usage")

with open(settings_path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")

print("  added: " + (", ".join(added) if added else "nothing new (already wired)"))
PY

head "Harness rules file (manual, one line)"
cat <<EOF
  Point your harness at the canonical principles doc:

    Claude Code :  ln -sf "$REPO_DIR/adapters/CLAUDE.md"  ~/.claude/CLAUDE.md
    Codex       :  ln -sf "$REPO_DIR/adapters/AGENTS.md"  <repo>/AGENTS.md
    Gemini CLI  :  ln -sf "$REPO_DIR/principles/working-style.md"  ~/.gemini/GEMINI.md

  (Left manual on purpose — these overwrite an existing rules file.)

Done. Restart your Claude Code session so the hooks load.
EOF

#!/usr/bin/env python3
"""Read-cost guard — cut token waste from oversized images and redundant re-reads.

Session-log audit (2026-07, 60 days) showed two Read patterns dominating input
tokens: (1) full-resolution screenshots/images fed to high-res vision models at
up to ~4.8k tokens each when a ~1000px copy carries the same information for
~900, and (2) 50% of all Reads re-reading a file already read in-session with
unchanged mtime (worst case: one file read 184x in a session). Every wasted
token is then re-read on each subsequent turn via prompt cache (~41x observed
multiplier), so trimming at the Read boundary compounds.

One subcommand (check), wired to PreToolUse (matcher "Read"). Two branches:

  image  — target is a large raster image: downscale a copy (sips -Z, macOS
           native) into a cache dir and DENY once, pointing at the copy. If
           full resolution is genuinely needed, the immediate retry of the
           same path is ALLOWED (pending-retry escape).
  text   — target was already read this session and its mtime is unchanged
           within a freshness window: DENY once with a cite-from-context
           reminder. The immediate retry is ALLOWED — post-compaction re-reads
           are legitimate (the content really is gone from context), so this
           is friction for the reflexive re-read, not a wall.

Fail-open by design: missing file, sips failure, unparseable payload, state
I/O error => allow. Bounded reads (offset/limit) and PDFs are exempt — an
agent already reading surgically is not the problem. Escape hatch:
  touch ~/.claude/cache/read-guard/.guard-off

Pure stdlib + sips. Portability: the image branch shells out to `sips`
(macOS-native); on platforms without it the subprocess call fails and the
branch fails open -- the text branch (redundant re-reads) is fully portable.
Wire via settings.json (the repo's install.sh does this):
  PreToolUse (matcher "Read") -> python3 ~/.claude/hooks/read-guard.py
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CACHE_DIR = Path.home() / ".claude" / "cache" / "read-guard"
STATE_DIR = CACHE_DIR / "sessions"
OVERRIDE_FILE = CACHE_DIR / ".guard-off"

MAX_EDGE = 1000              # px; downscale target (~1.0MP => ~1.3k tokens max)
TRIGGER_EDGE = 1400          # only deny above this — below it the saving vs the
                             # downscaled copy is too small to justify a bounce
SIZE_PROBE_BYTES = 200_000   # skip dimension probe for images smaller than this
REREAD_WINDOW = 10 * 60      # only guard re-reads this recent; older => likely
                             # compacted away, allow silently
STATE_MAX_AGE = 48 * 3600    # prune session state files older than this
IMAGE_CACHE_MAX_AGE = 7 * 24 * 3600

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".heic"}
EXEMPT_EXTS = {".pdf"}       # Read uses a pages param; range varies per call


# --- plumbing ------------------------------------------------------------------

def read_payload() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def allow() -> None:
    sys.exit(0)


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


# --- per-session state (path -> last-read record) --------------------------------

def state_path(session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "unknown"
    return STATE_DIR / f"{safe}.json"


def load_state(session_id: str) -> dict:
    try:
        return json.loads(state_path(session_id).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(session_id: str, state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        p = state_path(session_id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def prune_old_files(directory: Path, max_age: float) -> None:
    now = time.time()
    try:
        for f in directory.iterdir():
            try:
                if f.is_file() and now - f.stat().st_mtime > max_age:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass


# --- image branch ----------------------------------------------------------------

def image_dims(path: str) -> tuple[int, int] | None:
    try:
        r = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    w = h = None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("pixelWidth:"):
            w = int(line.split(":")[1])
        elif line.startswith("pixelHeight:"):
            h = int(line.split(":")[1])
    return (w, h) if w and h else None


def downscaled_copy(src: str, mtime: float) -> Path | None:
    """Downscale src to MAX_EDGE long edge in the cache dir; reuse if present."""
    ext = os.path.splitext(src)[1].lower()
    if ext == ".heic":
        ext = ".jpg"  # normalize; sips converts on the fly via -s format
    key = hashlib.sha1(f"{src}:{int(mtime)}:{MAX_EDGE}".encode()).hexdigest()[:16]
    dst = CACHE_DIR / f"{key}{ext}"
    if dst.exists():
        return dst
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        prune_old_files(CACHE_DIR, IMAGE_CACHE_MAX_AGE)
        args = ["sips", "-Z", str(MAX_EDGE)]
        if os.path.splitext(src)[1].lower() == ".heic":
            args += ["-s", "format", "jpeg"]
        r = subprocess.run(
            [*args, src, "--out", str(dst)],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return None
    return dst if r.returncode == 0 and dst.exists() else None


def image_deny_reason(src: str, dims: tuple[int, int], dst: Path) -> str:
    w, h = dims
    approx_full = min((w * h), 2576 * 1456) // 750
    return "\n".join([
        "Read-cost guard: oversized image.",
        "",
        f"  {src} is {w}x{h} (~{approx_full:,} image tokens at this size).",
        f"  A {MAX_EDGE}px downscaled copy has been written to:",
        "",
        f"    {dst}",
        "",
        "Read that copy instead — for screenshots and layout/design review it carries",
        "the same information at a fraction of the token cost, and it stays in context",
        "for the rest of the session.",
        "",
        "If full-resolution detail is genuinely required (fine texture, small text,",
        "pixel-level QC), re-run this exact Read — the retry will be allowed.",
    ])


# --- text branch -------------------------------------------------------------------

def text_deny_reason(path: str, last_ts: float) -> str:
    ago = max(1, int((time.time() - last_ts) // 60))
    return "\n".join([
        "Read-cost guard: redundant re-read.",
        "",
        f"  {path} was already read {ago} minute(s) ago in this session and has not",
        "  changed on disk since (mtime identical). Its contents should still be in",
        "  your context — cite from what you already have.",
        "",
        "If you need a specific section of a large file, use offset/limit instead of",
        "re-reading the whole file. If the content is genuinely no longer in context",
        "(e.g. dropped by compaction), re-run this exact Read — the retry will be",
        "allowed.",
    ])


# --- check -------------------------------------------------------------------------

def cmd_check(payload: dict) -> None:
    if payload.get("tool_name") != "Read":
        allow()
    if OVERRIDE_FILE.exists():
        allow()

    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path")
    if not isinstance(path, str) or not path:
        allow()
    path = os.path.abspath(os.path.expanduser(path))

    # Reads of our own downscaled copies are always allowed.
    try:
        if Path(path).parent == CACHE_DIR:
            allow()
    except Exception:
        pass

    try:
        st = os.stat(path)
    except OSError:
        allow()  # missing file => let Read produce its own error

    ext = os.path.splitext(path)[1].lower()
    if ext in EXEMPT_EXTS:
        allow()

    session_id = payload.get("session_id") or "unknown"
    state = load_state(session_id)
    entry = state.get(path) or {}
    now = time.time()

    # Pending-retry escape (both branches): the immediately-prior attempt on this
    # path was denied and the file is unchanged => this is the deliberate retry.
    if entry.get("pending_retry") and entry.get("mtime") == st.st_mtime:
        entry.update({"ts": now, "pending_retry": False})
        state[path] = entry
        save_state(session_id, state)
        allow()

    if ext in IMAGE_EXTS:
        # Small files can't be over the edge threshold at screenshot-like aspect
        # ratios; skip the dimension probe entirely.
        if st.st_size < SIZE_PROBE_BYTES:
            allow()
        dims = image_dims(path)
        if dims is None or max(dims) <= TRIGGER_EDGE:
            allow()
        dst = downscaled_copy(path, st.st_mtime)
        if dst is None:
            allow()  # sips failed => fail open
        state[path] = {"mtime": st.st_mtime, "ts": now, "pending_retry": True}
        save_state(session_id, state)
        deny(image_deny_reason(path, dims, dst))

    # Text branch. Surgical reads are exempt — offset/limit callers are already
    # doing the right thing.
    if "offset" in tool_input or "limit" in tool_input:
        entry.update({"mtime": st.st_mtime, "ts": now, "pending_retry": False})
        state[path] = entry
        save_state(session_id, state)
        allow()

    prev_mtime = entry.get("mtime")
    prev_ts = entry.get("ts") or 0
    if prev_mtime == st.st_mtime and (now - prev_ts) < REREAD_WINDOW:
        state[path] = {"mtime": st.st_mtime, "ts": prev_ts, "pending_retry": True}
        save_state(session_id, state)
        deny(text_deny_reason(path, prev_ts))

    # First read, changed file, or stale-enough re-read (likely compacted away).
    state[path] = {"mtime": st.st_mtime, "ts": now, "pending_retry": False}
    save_state(session_id, state)
    prune_old_files(STATE_DIR, STATE_MAX_AGE)
    allow()


def main() -> None:
    cmd_check(read_payload())


if __name__ == "__main__":
    main()

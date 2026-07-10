#!/usr/bin/env python3
"""token-audit — measure where an agent harness actually spends tokens.

Reads Claude Code session logs (~/.claude/projects/**/*.jsonl) and reports,
per the audit-discipline principle: measure the canonical source before
optimizing. Agent-token intuitions are routinely wrong — on the corpus this
tool was built against, the suspected offender (web fetches) was innocent
and the real costs (oversized images, redundant re-reads, session length)
were invisible until measured.

What it reports:
  - API usage totals: fresh input, cache writes, cache reads, output — plus
    the REPLAY MULTIPLIER (cache reads / tokens written), the single most
    important number: every token that lands in context is re-read on every
    subsequent API call in the session, so a payload's true cost is its size
    times that multiplier.
  - Per-model usage (caches are per-model; a wide model spread means cold
    cache on every switch).
  - Tool RESULT payloads by tool — what gets fed back into context.
  - Tool INPUT payloads — what the model generates (output tokens).
  - Read analysis: bytes by extension, top files, redundant re-read rate
    (same file, same session), bounded (offset/limit) vs whole-file reads.
  - WebFetch content quality (raw HTML leaking through vs filtered).
  - Hook-injected context volume.
  - Image results (billed by pixel dimensions, NOT by payload chars — a
    full-res screenshot costs up to ~4.8k tokens on high-res vision models,
    ~1.6k on older ones; chars/4 does not apply to images).

Caveats: text token estimates use chars/4 — good for relative comparison,
not billing. Records are read stream-wise; a 2+ GB corpus takes a minute.

Usage:
  python3 token-audit.py [--days 60] [--projects-dir ~/.claude/projects] [--top 20]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import time
from collections import defaultdict


def content_len(c) -> int:
    if c is None:
        return 0
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        n = 0
        for b in c:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    n += len(b.get("text", ""))
                elif b.get("type") == "image":
                    n += 0  # counted separately; char length is meaningless for images
                else:
                    n += len(json.dumps(b))
            else:
                n += len(str(b))
        return n
    return len(str(c))


def pct(sizes: list[int], p: float) -> int:
    if not sizes:
        return 0
    s = sorted(sizes)
    return s[min(len(s) - 1, int(len(s) * p))]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--projects-dir", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    cutoff = time.time() - args.days * 86400
    files = [f for f in glob.glob(os.path.join(args.projects_dir, "**/*.jsonl"), recursive=True)
             if os.path.getmtime(f) > cutoff]

    usage = defaultdict(int)
    model_usage = defaultdict(lambda: defaultdict(int))
    tool_stats = defaultdict(lambda: {"count": 0, "chars": 0, "sizes": []})
    tool_input_chars = defaultdict(int)
    hook_chars, hook_counts = defaultdict(int), defaultdict(int)
    img_results = defaultdict(int)
    webfetch = {"html_like": 0, "clean": 0}
    ext_stats = defaultdict(lambda: [0, 0])
    file_stats = defaultdict(lambda: [0, 0])
    session_file_reads = defaultdict(int)
    bounded_chars = unbounded_chars = 0
    id2name, id2read = {}, {}
    sessions = set()

    for f in files:
        try:
            fh = open(f, "r", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                sid = rec.get("sessionId")
                if sid:
                    sessions.add(sid)

                att = rec.get("attachment")
                if isinstance(att, dict) and att.get("type") in ("hook_success", "hook_failure"):
                    hn = att.get("hookName", "?")
                    hook_chars[hn] += len(att.get("stdout") or "") + len(att.get("content") or "")
                    hook_counts[hn] += 1

                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                t = rec.get("type")

                if t == "assistant":
                    u = msg.get("usage") or {}
                    mdl = msg.get("model", "?")
                    for k in ("input_tokens", "output_tokens",
                              "cache_creation_input_tokens", "cache_read_input_tokens"):
                        v = u.get(k) or 0
                        usage[k] += v
                        model_usage[mdl][k] += v
                    for b in msg.get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            name = b.get("name", "?")
                            id2name[b.get("id")] = name
                            inp = b.get("input") or {}
                            tool_input_chars[name] += len(json.dumps(inp))
                            if name == "Read":
                                path = inp.get("file_path", "?")
                                id2read[b.get("id")] = (path, "limit" in inp or "offset" in inp)
                                session_file_reads[(sid, path)] += 1

                elif t == "user":
                    c = msg.get("content")
                    if not isinstance(c, list):
                        continue
                    for b in c:
                        if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                            continue
                        tid = b.get("tool_use_id")
                        name = id2name.get(tid, "?unknown")
                        sz = content_len(b.get("content"))
                        s = tool_stats[name]
                        s["count"] += 1
                        s["chars"] += sz
                        s["sizes"].append(sz)
                        bc = b.get("content")
                        if isinstance(bc, list) and any(
                                isinstance(x, dict) and x.get("type") == "image" for x in bc):
                            img_results[name] += 1
                        if name == "WebFetch" and sz:
                            txt = bc if isinstance(bc, str) else json.dumps(bc)[:20000]
                            key = "html_like" if re.search(r"<(div|span|script|nav|href=|class=)", txt) else "clean"
                            webfetch[key] += 1
                        if tid in id2read:
                            path, bounded = id2read[tid]
                            ext = os.path.splitext(path)[1].lower() or "(none)"
                            ext_stats[ext][0] += 1
                            ext_stats[ext][1] += sz
                            file_stats[path][0] += 1
                            file_stats[path][1] += sz
                            if bounded:
                                bounded_chars += sz
                            else:
                                unbounded_chars += sz

    print(f"files={len(files)} sessions={len(sessions)} window={args.days}d\n")

    print("== API usage (tokens) ==")
    for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
        print(f"  {k:32s} {usage[k]:>16,}")
    written = usage["input_tokens"] + usage["cache_creation_input_tokens"]
    if written:
        print(f"  {'REPLAY MULTIPLIER':32s} {usage['cache_read_input_tokens'] / written:>15.1f}x  "
              "(each context token re-read this many times on average)")

    print("\n== per-model (caches are per-model) ==")
    for mdl, u in sorted(model_usage.items(), key=lambda x: -sum(x[1].values())):
        if sum(u.values()) < 1_000_000:
            continue
        print(f"  {mdl:42s} in={u['input_tokens']:>11,} cache_w={u['cache_creation_input_tokens']:>13,} "
              f"cache_r={u['cache_read_input_tokens']:>15,} out={u['output_tokens']:>11,}")

    print("\n== tool RESULT payloads (chars fed back as input; ~4 chars/token for text) ==")
    rows = sorted(tool_stats.items(), key=lambda x: -x[1]["chars"])
    total = sum(s["chars"] for _, s in rows) or 1
    print(f"  {'tool':40s} {'calls':>7s} {'total':>9s} {'share':>6s} {'p50':>8s} {'p90':>9s}")
    for name, s in rows[:args.top]:
        print(f"  {name[:40]:40s} {s['count']:>7,} {s['chars']/1e6:>8.1f}M {s['chars']/total*100:>5.1f}% "
              f"{pct(s['sizes'], .5):>8,} {pct(s['sizes'], .9):>9,}")

    print("\n== tool INPUT payloads (model-generated => output tokens) ==")
    for name, ch in sorted(tool_input_chars.items(), key=lambda x: -x[1])[:8]:
        print(f"  {name[:40]:40s} {ch/1e6:>8.1f}M chars")

    print("\n== Read: bytes by extension ==")
    for ext, (n, ch) in sorted(ext_stats.items(), key=lambda x: -x[1][1])[:12]:
        note = "  <- images billed by DIMENSIONS, not chars" if ext in (".png", ".jpg", ".jpeg", ".webp") else ""
        print(f"  {ext:10s} {n:>7,} reads {ch/1e6:>8.1f}M chars{note}")

    print(f"\n== Read: top {args.top} files by total chars ==")
    for path, (n, ch) in sorted(file_stats.items(), key=lambda x: -x[1][1])[:args.top]:
        print(f"  {ch/1e6:>7.2f}M {n:>5}x  {path}")

    rr = [v for v in session_file_reads.values() if v > 1]
    total_reads = sum(session_file_reads.values())
    wasted = sum(v - 1 for v in rr)
    print("\n== Read: redundant re-reads (same file, same session) ==")
    print(f"  total reads: {total_reads:,}; redundant: {wasted:,} "
          f"({wasted / max(total_reads, 1) * 100:.0f}%)")
    for (sid, path), n in sorted(session_file_reads.items(), key=lambda x: -x[1])[:8]:
        print(f"  {n:>4}x  {path}  [session {str(sid)[:8]}]")

    print("\n== Read: whole-file vs bounded ==")
    print(f"  unbounded: {unbounded_chars/1e6:.1f}M chars   bounded (offset/limit): {bounded_chars/1e6:.1f}M chars")

    print("\n== WebFetch content quality ==")
    print(f"  html-like: {webfetch['html_like']}   clean: {webfetch['clean']}"
          "   (html-like > 0 => raw pages leaking through unfiltered)")

    print("\n== image tool results ==")
    for name, n in sorted(img_results.items(), key=lambda x: -x[1])[:6]:
        print(f"  {name[:40]:40s} {n:>6,}")

    print("\n== hook-injected context ==")
    for hn, ch in sorted(hook_chars.items(), key=lambda x: -x[1])[:8]:
        print(f"  {hn[:44]:44s} {hook_counts[hn]:>7,}x {ch/1e6:>7.1f}M chars")


if __name__ == "__main__":
    main()

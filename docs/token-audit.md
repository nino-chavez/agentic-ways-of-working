# Token audit — measure before you optimize

Agent-token intuitions are routinely wrong. The way to find out where an agent
harness actually spends tokens is to measure the canonical source — the session
logs — not to reason from vibes about which tool "feels expensive." This is the
[audit-discipline principle](../principles/working-style.md) applied to agent
operations: an artifact's claim (or your hunch) is a hypothesis until the data
is pulled.

## Run it

```sh
python3 tools/token-audit.py --days 60
```

Reads `~/.claude/projects/**/*.jsonl` (override with `--projects-dir`). Streams
records, so a multi-GB corpus finishes in a minute or two. No dependencies.

## The numbers that matter

**Replay multiplier** — cache reads ÷ tokens written. The API is stateless:
every tool call replays the whole conversation, so a payload's true cost is its
size × the API calls that follow it in the session. This makes session cost
roughly quadratic in turn count, which is why the biggest lever is almost never
payload trimming — it's session hygiene (`/clear` between serial tasks, ending
sessions instead of idling them) and delegating exploration to subagents whose
context dies with them.

**Redundant re-read rate** — same file, same session, unchanged content.
A high rate is usually a *symptom* of long sessions: compaction drops tool
results, the agent re-reads what it lost, context grows, compaction again.
Fix the session length; add friction at the re-read
([`hooks/read-guard.py`](../hooks/read-guard.py)) as the backstop.

**Image reads** — billed by pixel dimensions, not payload size. A
full-resolution screenshot costs up to ~4.8k tokens on high-res vision models
where a 1000px copy of the same screen costs ~900. Char-based shares in the
report *understate* nothing here — images are broken out separately because
chars/4 doesn't apply to them.

**Cache-write ratio** — cache writes ÷ fresh input. Writes cost 1.25× input
price and happen on TTL expiry (5 min idle) and on every subagent spawn. A high
ratio means fat sessions being resumed after idle gaps: each resume rewrites
the entire context at premium rate.

**Per-model spread** — caches are per-model. A mid-workflow model switch starts
cold.

## What one 60-day corpus showed

Findings from the corpus this tool was built against (2,335 session files,
529 sessions), as a calibration reference:

- Replay multiplier: **41×**. Session length dominated everything else.
- The suspected offender — web fetches dumping raw HTML — was **innocent**:
  0 of 873 fetches contained raw HTML (the harness already filters them).
- Images were 78% of Read bytes; screenshots were captured full-page at full
  resolution and re-captured/re-read 25×+ per design-loop session.
- 50% of all Reads were redundant (worst single case: one file, 184 reads,
  one session) — compaction-driven.
- Hook-injected context, suspected of bloat, measured under 0.5% of cache
  reads: injection *position* (stable prefix / appended) matters more than
  injection size.

Three of four hypotheses held only after inversion. Measure first; build
guards second.

## What to do with findings

| Finding | Response |
|---|---|
| High replay multiplier | Session hygiene: `/clear` between tasks, front-load the first prompt, batch independent tool calls, delegate exploration (>~3 reads for a conclusion) to subagents |
| Oversized image reads | [`hooks/read-guard.py`](../hooks/read-guard.py) image branch; element-level screenshot capture instead of full-page |
| High redundant-read rate | read-guard text branch as friction; shorter sessions as the fix |
| One tool dominating results | Add a digest step at the source (e.g. pre-extract the 20 fields you need from a raw JSON artifact instead of re-reading the raw file) |
| High cache-write ratio | Don't resume fat idle sessions casually; end finished sessions |
| Raw HTML in WebFetch results | Your harness isn't filtering fetches — add a readability/markdown pass |

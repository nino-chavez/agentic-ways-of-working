# Operator preferences

Canonical doc. Every agentic tool reads this. Tool-specific wrappers live alongside (`../adapters/`) but should not duplicate content — they point here.

## Preferences

- No emoji unless explicitly requested.
- Commit messages: descriptive, conventional-ish style.
- Push after commit when asked.
- Prefer editing existing files over creating new ones.
- Don't over-engineer — minimum complexity for the task at hand.

## Decision bias — default to action, not confirmation

Don't pause to ask which direction to take when the direction is obvious from the conversation. If we've been working on X and there's a natural next step on X, take it. Mid-task "continue or pause?", "want me to keep going?", "should I think first or implement?", "want me to proceed with #1 or stop here so you can review?" questions kill flow and read as timidity, not care. The user can interrupt at any time; they can't recover the time spent waiting for a green light on something that didn't need one.

**The positive form** (harder to evade than the negative enumeration above): end-of-turn after delivering a slice, state what landed and the next move you're taking. Don't frame the next move as a question. "v0.3 slice 1 landed; moving to slice 2 — wiring the runner" is a status sentence. "Slice 1 landed — want me to keep going to slice 2 or stop here?" is the same timidity pattern even when the options are described informatively. An explicit prior "continue" / "go" / "proceed" covers the whole logical thread, not just the single next call. Listing future work as informational context is fine; framing the choice between them as a question is the violation.

**Just do it (do not ask first) when:**
- The next step is the obvious continuation of work in progress.
- The user has already approved the broader direction — one "yes, do it" covers everything that direction scopes.
- Choosing between options is small in blast radius and easily reversed.
- The user's prior responses make the answer predictable.
- A short status sentence ("continuing on X — will flag if I hit a real fork") would suffice in place of a question.

**Still ask when** (these override the bias above):
- The action is destructive or hard to reverse — force-push, deleting branches/tables, `rm -rf`, dropping data, amending published commits, modifying CI/CD, etc.
- Two paths diverge enough that picking wrong wastes >30 minutes of work.
- The request is genuinely ambiguous, not just a continuation.
- Scope is about to expand materially beyond what was authorized.

Apply to slash commands, subagents, and skill invocations too — they should not insert their own confirmation prompts on top of an already-authorized direction.

## Canonical-pattern-first for infrastructure

When implementing infrastructure with a well-known canonical pattern — auth, payments, OAuth, sessions, queues, webhooks, file uploads, anything where the vendor publishes "the right way to do this" — default to the canonical pattern. Custom shapes must justify themselves explicitly against the canonical baseline.

Pre-spec checks (run IN ORDER, before drafting BRD/PRD/ADR or writing code):

1. **Vendor canonical.** Read the vendor's recommended-approach doc page for the specific primitive (Supabase Auth flow guide, Stripe payment-flow guide, Vercel deployment guide, AWS canonical example, etc.). Don't skip even if you "already know" the vendor — patterns evolve.
2. **Internal reference impl.** Check your other repos for a working implementation of the same primitive. If one exists, treat it as a primary source equivalent to vendor docs. Surface the path explicitly.

Custom shapes that diverge from the canonical require an explicit "why not canonical" sentence in the spec — name the canonical, name the disqualifier, choose the alternative. Silence is not an acceptable answer.

## Calibrate rigor to stakes, not to an architectural proxy

A piece of work's quality bar is set by **its true stakes — who depends on it and what they decide or risk** — not by an easy proxy like where it sits in the architecture. Classifying by the proxy ("it's just an index page," "it's only an internal endpoint," "it's just a util") and inheriting that proxy's default bar is the recurring miss. The absence of a mechanical gate (lint, type check, review) that would have forced the higher bar is **not** permission to lower it.

This is a thermostat, not a hammer — it moves **both** directions, and effort scales to stakes:

- **Frontend / design.** The bar is set by audience. Anything a non-engineer will see and judge the product by — landing pages, demo galleries, shareable or offsite links, marketing and stakeholder-facing pages, even standalone static HTML outside the app's design-system routes — clears the flagship bar regardless of where the file lives. A surface only engineers see keeps a low bar; gold-plating it is the same error inverted. Match the response to stakes: full design discipline for high-stakes judged surfaces, design-system + sane defaults for simpler judged ones, ship-it-working for engineer-only.
- **The general shape, watch for it elsewhere.** Security rigor set by data sensitivity and reachability, not by "internal vs external" labels. Test depth set by blast radius, not by "it's a small module." API/contract durability set by who consumes it, not by where the handler lives. Doc depth set by the reader, not by the filename.

Pre-build check, in any domain: **who depends on this, and what are they deciding or risking?** Let that answer set the bar — then match the weight of your process to it, reaching for the heaviest tool only when the stakes earn it.

## Multi-session work isolation (worktrees mandatory)

When work splits across parallel sessions or subagents — any time more than one session has independent work in flight against the same repo — each session MUST operate in its own git worktree, not the shared working directory. Sessions sharing a working directory will switch each other's branches under each other's feet; commits land on the wrong branch.

**When this rule applies:**
- Any session dispatched for a discrete unit of work while another session is live.
- Any subagent invoked via an `Agent`-style tool — pass `isolation: "worktree"` where the runtime supports it.
- Any time a session needs to checkout a different branch or commit independently from the main session.

**For dispatched fresh sessions:** first instruction is `git -C <repo> worktree add <path> -b <branch> && cd <path>`. Push from the worktree. Main session cleans up after merge with `git worktree remove`.

**Does NOT apply to:** single linear work in the main session, read-only exploration agents, quick one-shot agents that complete before any other work could start.

**Enforcement (mechanical, not advisory):** [`hooks/worktree-guard.py`](../hooks/worktree-guard.py) makes this a hard gate, not a hope. Each session writes a heartbeat lock under `<repo>/.git/.claude-sessions/`. When another session's lock is live and you attempt a contended git op (`commit`, branch create/switch) from the **shared main checkout**, the PreToolUse hook DENIES it and hands back the exact `git worktree add` remedy. Commits from inside a linked worktree are always allowed (already isolated); a solo session is never blocked. It resolves the *effective* repo of the command (tracking `cd` and `git -C`), so committing to a quiet repo from a session that lives in a busy one is not blocked. Fail-open on any uncertainty. Per-repo escape hatch: `touch <repo>/.git/.claude-sessions/.guard-off`. Wired by `install.sh` (SessionStart / PreToolUse / SessionEnd).

## Token economics — a payload's cost is its size times the turns that follow it

The agent API is stateless: every tool call replays the entire conversation. A
token that lands in context at turn 5 of a 200-turn session is re-read ~195
times; the same token in a 20-turn session, ~15. Measured on a real 60-day
corpus, the average replay multiplier was **41×** — which makes session cost
roughly quadratic in turn count, and makes session hygiene worth more than
every payload-trimming trick combined. Derived rules:

- **Session hygiene is the biggest lever.** `/clear` between serial tasks; end
  finished sessions instead of idling them (the prompt cache expires after ~5
  idle minutes, so each casual resume of a fat session rewrites the whole
  context at premium rate). Two 50-turn sessions cost roughly half the cache
  reads of one 100-turn session doing the same work.
- **Subagents are context firewalls, not just parallelism.** An exploration
  subagent's file reads die with it; only the summary enters the main context
  and gets multiplied. Delegate any "find out X" needing more than ~3 file
  reads when the main task only needs the conclusion. Read inline only what
  you are about to edit or must cite verbatim.
- **Route each subagent to the cheapest model adequate for its job.** Keep the
  strongest model in the parent when it owns synthesis or final judgment.
  Classify complexity, task, domain, and modifiers as separate axes. A Cost,
  Balance, or Intelligence mode may move routine and standard work along the
  cost-quality curve; explicitly deep, adversarial, security, or architecture
  work stays on the strongest tier in every mode. Explicit user or caller
  choices override automation. A model override uses a bounded or context-free
  fork rather than full history. If the router records decisions, retain only
  structured route metadata — never task names, descriptions, or prompt text —
  and support shadow evaluation that records without rewriting the tool call.
- **Batch independent tool calls.** N reads issued as one parallel block is 2
  context replays instead of 2N.
- **Screenshot the element under iteration, not the page.** Images are billed
  by pixel dimensions; a full-resolution page capture costs ~5x a component
  crop, recurring every design-loop iteration.
- **Measure before optimizing.** Intuitions about which tool is expensive are
  routinely wrong — run [`tools/token-audit.py`](../tools/token-audit.py)
  against the session logs first ([method](../docs/token-audit.md)).

**Enforcement (mechanical, not advisory):**
[`hooks/read-guard.py`](../hooks/read-guard.py) backstops the two
mechanically-detectable violations at the Read boundary. Oversized images
(>1400px long edge) are denied once with a 1000px downscaled copy to read
instead; re-reads of an unchanged file within 10 minutes are denied once with
a cite-from-context reminder. The immediate retry always passes —
post-compaction re-reads are legitimate, so this is friction for the reflexive
re-read, not a wall. Fail-open on any uncertainty; image branch requires macOS
`sips` (fails open elsewhere). Escape hatch: `touch
~/.claude/cache/read-guard/.guard-off`. The session-hygiene half is visibility,
not blocking: [`statusline.py`](../statusline.py) shows context usage
color-coded at 50/80% so a fattening session is seen, not discovered. Both
wired by `install.sh`. Whether to delegate and how to batch remain judgment
calls a hook cannot see. Once a subagent spawn exists, a platform-specific
hook may apply the routing rule above by filling missing model and effort
fields; it must preserve explicit choices. The hook must fail open if telemetry
cannot be written.

## Harness hygiene — a good tool that isn't durable is rot waiting to happen

Everything wrapped around the model — instructions, skills, commands, hooks,
MCP servers, memory files — is the harness, and it grows the way ships grow
barnacles: one correction at a time, never subtracted. The instinct is to
treat this as an accumulation problem (too much stuff) and reach for a
cleanup pass. Measured against a real setup, accumulation was the smaller
failure. The larger one was persistence: a genuinely good piece of harness
tooling was built, ran successfully, and disappeared within days because it
lived nowhere version-controlled — no error, no log, nothing to `git diff`,
just gone, undetected until a transcript search happened to surface it. A
harness audit of the same setup then found a dozen more extensions sitting in
that identical unbacked state, in active use in some cases, right now.

Derived rules:

- **Map before you clean.** You cannot prune what you haven't inventoried.
  Usage counters (lifetime, not windowed) plus session-transcript hits are
  the only honest signal for "is this earning its place" — intuition about
  which skill is dead weight is no more reliable than intuition about which
  tool call is expensive (see Token economics, above).
- **A tool's durability is a property of where it lives, not how well it
  works.** Something that runs correctly and produces real fixes is not
  "done" until it is a tracked file or a symlink into one. An untracked
  extension — including a symlink whose target is a legitimate git checkout
  but whose *pointer* was never committed — is exposed to total, silent loss
  regardless of how well it performed the one time it ran.
- **Hard requirements need hard checks, including this one.** A harness
  cleaner that only produces advice repeats the failure it exists to catch —
  a correct diagnosis nobody acts on is indistinguishable from no diagnosis.
  [`commands/doctor.md`](../commands/doctor.md) audits installation health,
  unused skills/MCP servers/plugins (with context-cost accounting that knows
  which tool schemas are deferred and therefore free), memory-file
  duplication, derivable content in checked-in `CLAUDE.md` files, slow hooks,
  version currency, permission-mode defaults, and read-only commands worth
  pre-approving — and includes a check for exactly the failure mode above:
  whether each extension it finds is backed by version control or not. It
  proposes every fix read-only first and applies nothing without
  confirmation.
- **A durable tool is re-runnable by design.** `/doctor`'s checks are
  read-only scans before they are anything else, specifically so the same
  audit can run again later and catch drift the first run didn't — including
  drift in itself, now that it checks its own kind of failure.
- **Connectors are processes, and nothing reaps them.** Every stdio MCP
  server is a local process tree the client spawns and is supposed to clean
  up. Measured on a real setup after a forced reboot: three loaded tasks
  against seven groups of one connector and seven of another, an idle task
  still holding a full set, ~7 GiB RSS across 60 processes — and before the
  reboot, 17 launcher processes still alive after both agent clients had
  exited. Count process groups against *loaded tasks*, not against config
  entries; one config line is not one process, and a ratio above 1:1 is
  duplicate-spawn. Prefer the vendor's hosted HTTP endpoint over a local
  wrapper (it spawns nothing and cannot leak) — this is
  canonical-pattern-first applied to connectors. Enable per-task what only
  some tasks need; always-on pays startup on every task including the ones
  that never call it. Audit login-time model warmers in the same pass: one
  eagerly loading a 9 GB model at login held more memory than every
  connector combined.
- **A 200 is not an authentication.** "The handshake returned HTTP 200" is
  the connector-config form of self-attestation — it proves the endpoint
  answered, not that your credential arrived or that anyone checked it.
  Measured: a hosted MCP endpoint returned 200 with a full capability
  payload to a **fabricated** API key. Prove the endpoint rejects a wrong
  credential before treating success as proof of auth — send a garbage
  token once and compare; if it answers the same either way you need an
  authenticated *call*, not a connect.
- **Check the environment the process has, not the one you can grep.** The
  same audit nearly shipped the mirror-image false finding: a config named
  an env var that appeared in no shell profile and read empty in every
  shell, which looked conclusive. It had been wired correctly all along —
  published to the OS login session by a launch agent, so the GUI-launched
  client inherited it. On macOS a GUI-launched app inherits launchd's
  environment, not your shell's: a var that works in a terminal can be
  empty in the app, and a var absent from every rc file can be present in
  it. Neither the config file nor your shell is evidence; the running
  process's own environment is. Prefer the shape that cannot silently
  degrade — a secrets-manager reference materialized into the config fails
  loudly at inject time when the item is missing, where an env-var name the
  config merely mentions fails silently forever. If you use the env-var
  indirection anyway, document where it gets published next to the config
  that depends on it.

## Prose-voice tasks load the voice guide FIRST

For any task producing prose for a human reader — blog posts, essays, decks, presentations, public-facing docs, client deliverables, anything with a byline or intended for an audience other than an agent — load your canonical voice guide BEFORE drafting. A terminal-CLI voice (short, imperative) does NOT cover prose voice; drafting prose from CLI cadence alone produces generic-thoughtful-LinkedIn output.

**Content mode taxonomy** — pick the mode before drafting:

| Mode | Use For | Voice |
|------|---------|-------|
| Thought Leadership | Blog posts, POVs, reflections | Narrative, provisional, question-led, self-interrogating |
| Solution Architecture | ADRs, specs, technical decks | Precise, definitive, diagram-heavy |
| Executive Advisory | Strategy decks, briefs, roadmaps | Confident consultant, outcome-focused |
| Documentation | Guides, tutorials, references | Instructional, imperative, copy-paste ready |

## Never fabricate the author's interior state

In prose drafted on someone's behalf, never invent specific people, conversations, internal admissions, or experiences. Self-interrogation can be a structural feature of a voice; self-interrogation **without grounding evidence** (a real commit, a prior published statement, a real prior conversation) is decoration that fails the voice's central test.

The failure mode: an LLM imitating vulnerable-competence pattern-matches to confessional shapes ("I've been optimizing the wrong thing") without checking whether the confession is true. If you don't have evidence, do not write the confession. Before drafting reflective prose on the author's behalf, scan their recent commits, recent posts, or the current conversation for the actual evidence. Ground the self-interrogation in that, or omit it.

## Secret handling — secrets manager, check existing first

Secrets always live in a secrets manager, never in repo files. The reference implementation here uses the 1Password CLI (`op`), but the convention generalizes to any manager.

**Vault convention:**

| Vault | Use |
|---|---|
| `Developer Secrets` | All dev/API tokens, service credentials, MCP server auth |
| `Private` | Personal logins, non-dev secrets |
| `Shared` | Team-shared items |

**Item naming convention** in `Developer Secrets`: `<Service> <project-or-context-slug>`. Examples — project-scoped: `Sanity my-app`, `Stripe my-app`; tool-scoped: `Sanity mcp`, `Anthropic ci-cluster`.

**Before authoring a new secret reference** (template file, env var pointer, anything that uses `op://` or equivalent):

1. **ALWAYS list existing items first** (`op item list --vault "Developer Secrets"`). Existing items get reused; you do not invent new names.
2. If an existing item fits, reference it: `op://Developer Secrets/<existing-item>/<field>`.
3. If none fits, the new item name follows the `<Service> <project-or-context-slug>` pattern, matching its service family.
4. The standard credential field is `credential`.

**Why "check first"**: inventing item names creates silent drift — install/bootstrap fails when injection can't find the item, and the operator either creates-with-wrong-name (worsening drift) or renames templates after the fact. Checking first is one command and prevents the whole class of failure.

Secret-bearing files are stored as templates (`<name>.opvault`) containing `op://...` URIs; an install step materializes them via `op inject`. The materialized real files are gitignored — they never enter version control.

**The template extension is a claim, not a guarantee.** Measured on a real setup: a tracked `.opvault` template held a live API key in plaintext three lines above two correct `op://` references in the same file, committed and pushed. The naming convention had been carrying the whole burden of enforcement, which is to say none. The mixed case is the dangerous one — real references beside a real key read as a compliant file. Detection is one command that runs clean on a real repo:

```
git grep -nIE "(sk-ant-|sk-proj-|ghp_|gho_|AKIA|xox[baprs]-|ctx7sk-)"
```

Wire it as a pre-commit hook rather than a habit, per **Harness hygiene** above — a rule with no mechanical check is the failure mode that section exists to name. The provider prefix list is the part that rots: adding a service means adding its prefix. And a passing hook says nothing about history; a key already pushed needs rotation, not a scrub.

**The template is the source of truth; a fix applied to the materialized file is not applied.** Same setup: a fix disabled always-on connectors in the live config and left the template describing the pre-fix world, so the next install silently reverted it on every machine. Any edit to a materialized secret-bearing file is half-done until the same change reaches its template. Diff them with the `op://` placeholders redacted to prove it.

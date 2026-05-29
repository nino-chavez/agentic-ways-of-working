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

## Multi-session work isolation (worktrees mandatory)

When work splits across parallel sessions or subagents — any time more than one session has independent work in flight against the same repo — each session MUST operate in its own git worktree, not the shared working directory. Sessions sharing a working directory will switch each other's branches under each other's feet; commits land on the wrong branch.

**When this rule applies:**
- Any session dispatched for a discrete unit of work while another session is live.
- Any subagent invoked via an `Agent`-style tool — pass `isolation: "worktree"` where the runtime supports it.
- Any time a session needs to checkout a different branch or commit independently from the main session.

**For dispatched fresh sessions:** first instruction is `git -C <repo> worktree add <path> -b <branch> && cd <path>`. Push from the worktree. Main session cleans up after merge with `git worktree remove`.

**Does NOT apply to:** single linear work in the main session, read-only exploration agents, quick one-shot agents that complete before any other work could start.

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

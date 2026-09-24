---
name: dispatch-wave
description: Use when about to produce ≥2 substantive artifacts whose specs are fully derived in-thread and whose file scopes don't overlap. Codifies the orchestrator workflow for parallel agent dispatch — brief construction (nine mandatory fields, runtime isolation and cleanup included), model selection through the host-owned routing policy, inline work during wait, post-flight cross-review for inter-artifact consistency, and commit-and-push following the project's dev-push pattern. Triggers proactively at the end of any planning thread that produced ≥2 inspectable artifact briefs, OR when the user explicitly says "dispatch", "wave", "in parallel", "across agents". Skip for single-artifact work, vague briefs ("implement X"), or briefs missing a self-contained spec.
---

# dispatch-wave

You are the orchestrator deciding whether to dispatch parallel agents and how to brief them. This skill is the checklist; follow it in order.

## Pre-flight — should this be a wave?

Answer YES to all six. If any answer is NO, do the work inline or sequentially instead.

1. **≥2 artifacts** with target file paths named in-thread.
2. **Specs are complete**: each artifact has a clear goal, output structure, and tonal model. If specs are vague, dispatching produces shallow generic work — finish the specs first.
3. **File scopes don't overlap**: target paths are disjoint. If a tool exists in the project (`tools/parallel-dispatch-check/check.sh` if blueprint-stamped), run it. Otherwise check by inspection.
4. **Execution doesn't need synthesis the brief can't carry**: agents start cold; if an artifact needs judgment calls only the orchestrator can make, do it inline.
5. **The brief set matches the ask, not the repo.** Decompose the ask into clauses and label each by kind: experience (what a person sees, does, and judges), capability, or process/governance. Enumerate the repo's *functional* catalog (user jobs, flows, screens) separately from its *process* catalog (decisions, evidence, gates): a well-documented repo is greppable in proportion to its process surface, which pulls briefs toward governance questions. Compare the brief set's composition with the clause split. If the ask carries a judgment about quality of experience, open the rendered artifact yourself before writing the briefs; a reviewer looks with its brief's question, not the user's. Re-run this check after any advisor call: advice about the strongest finding is advice about answer quality, not question scope. Measured 2026-09-03 (a product audit of a consumer mobile app): the ask was about three-quarters experience, four of five briefs were mechanical, the one "user fit" brief asked for tap counts, and the orchestrator never opened one of eleven device captures. One sentence of correction later, job-shaped briefs (the person's actual errand, stated the way they would say it) plus the orchestrator looking first produced a different class of finding within minutes. Those are two interventions; that session confounded them, so do not report which one worked without separating them.

6. **Runtime resources don't overlap either.** A worktree isolates code and nothing else. Before writing briefs, list what the agents will share while running and state how each is separated or why sharing is safe:

   | Shared resource | Separation |
   |---|---|
   | Browser tab lease | A `BROWSE_SESSION` slug per agent. Subagents of one session inherit one harness session id, so by default they share one leased tab. |
   | Cookie host | A hostname per agent, `<slug>.localhost`. Browsers scope cookies by host, not port. |
   | Ports | One assigned per agent, written in the brief. Never "pick a free one". |
   | Database | A test user per agent on a shared database, each touching only rows it owns; a database per agent when the work changes schema or shared rows. |
   | Docker stack | The orchestrator starts and stops it. Agents never do. |
   | Machine load | The concurrency cap under Dispatch mechanics, and the baseline read in post-flight step 6. |
   | The visible window | The agent browser runs headless for the wave. The operator's screen is a shared resource too. |

   A row with no answer is a NO. Measured 2026-09-21 (four native subagents, each with its own worktree and dev server): every incident that day was a resource missing from this list. All four agents held one browser tab lease, and one agent's `browse-eval` POST ran in another agent's tab against that agent's dev server and created a record there. All four servers sat on bare `localhost`, so the last agent to sign in was signed in on every port, and an agent got a 404 on its own record. Field 9 (cleanup) did not help: it is an end-of-run rule and cannot separate agents that are still running.

## Brief construction — nine mandatory fields per artifact

Each agent gets a self-contained brief because they have zero context from your thread. Missing fields produce drift.

1. **Goal + audience** — what the artifact is, who reads it cold.
2. **READ-FIRST sources** — absolute paths to files the agent mirrors for tone/structure. Read these yourself first to confirm they exist and match what you remember. For structured artifacts, include a canonical worked example of the target artifact type alongside any rulebook — agents resolve borderline calls by analogy to the example, and cite it in their reasoning (2026-08-08 manifest sweep: the closest calls were settled by analogy to the worked example, not by the rules).
3. **Full output structure** — every section, every enumeration item, every required cross-link. Don't say "include the analysis"; embed the analysis.
4. **Cross-references** — including forward-links to files being written in parallel. Mark them as forward-links so the agent knows not to verify.
5. **Don't-do list** — no marketing copy, no emojis, no padding, no inflation to hit length targets. Add project-specific don'ts from the repo's agent instructions (`CLAUDE.md` or `AGENTS.md`).
6. **Voice + length constraints** — match the tonal model from READ-FIRST; give a natural-fit range, not a target.
7. **Reporting expectations** — what the agent reports back (file path, line count, cross-refs they couldn't resolve, judgment calls). This is what makes post-flight cross-review possible.
8. **Runtime isolation** — the agent's own row from the pre-flight inventory, as literal values, not as a rule to interpret: its `BROWSE_SESSION=<slug>` prefix on every browse-tool command; its hostname `<slug>.localhost` for curl, the `Origin` header and every navigation, never bare `localhost` or `127.0.0.1`; its port; its test user. One tab, reused with `browse-nav`; test data is created with curl or a same-origin fetch, not a new tab per attempt. Before any browser action that writes, confirm the tab's URL is on the agent's own host and port. Never launch a Chrome binary, and never run `browse-start` or `browse-stop`: the browser belongs to the orchestrator. Work only in the agent's own worktree: confirm `pwd` before the first edit, because a session's cwd can default to the shared integration worktree (2026-09-21: one agent edited it by mistake and had to revert). Measured 2026-09-21: `<slug>.localhost` resolves to loopback, Vite accepted it, and the auth cookie stayed host-only (checked with curl), which ended the cross-signing; the `BROWSE_SESSION` prefix ended the shared lease. The same wave had 14 tabs open where 4 were needed, from stale tabs after the hostname switch plus one agent opening a tab per attempt, in a headed window the operator was looking at.
9. **Cleanup before reporting** — every process, tab and fixture the agent starts is the agent's to stop: dev servers and watchers it launched, browse-tool tabs it opened (close by target id over CDP, never the browser — the profile is shared and holds logins), local Supabase stacks it started, throwaway fixtures it created. The report lists what was cleaned and what was deliberately left, with why. Measured 2026-09-11 (four rounds of waves on one product repo): no brief said this, so eight side sessions and four rounds of waves left seven idle dev servers running 13–22 hours, 211 tabs in the shared agent browser holding 18.6 GB, and five local Supabase stacks (5.4 GB, three idle for four days); the combined load crossed 100 and Docker crashed mid-gate for every running wave.

When a brief carries proposed classifications or other judgment calls the orchestrator derived from labels, summaries, or memory rather than the source artifacts, mark the proposal as a **prior, not a spec**: instruct the agent to verify every item against the artifact's own text and license evidence-based deviation with a mandatory justifying quote. Measured 2026-08-08 (a document-classification sweep): 16 of ~70 label-derived proposals were wrong; the deviation-with-quote clause caught all sixteen, and the quotes made post-flight cross-review pre-evidenced instead of a second read.

## Model selection

Use the host's owned routing policy for task classification, model, and effort.
Do not treat a new persistent session as the meaning of “dispatch next.” The
parent retains coordination and judgment; the default deliverable is a bounded
worker result. See [dispatch routing](../../docs/dispatch-routing.md).

When a required dispatcher is installed, use it before any worker session is
created. It must record classification and the selected model/effort before
launch. A classifier or receipt failure blocks dispatch; do not fall back to
session defaults or a raw creation tool. Native agent hooks are only as strong
as their host's enforcement and trust behavior.

Without that integration, select the cheapest adequate model explicitly and
label the route as caller-selected, not mechanically enforced. Ordinary
execution-from-spec and judgment-heavy work are different jobs; do not assign a
frontier model to every artifact in a wave.

## Dispatch mechanics

1. Use the harness's local task bookkeeping to make the work visible — one item per artifact, plus orchestrator side-work and cross-review. `TaskCreate` in this instruction is bookkeeping, not Codex `create_thread`. Separate persistent sessions require an explicit user request.
2. Mark each artifact task `in_progress`; record the worker ID when launch returns it.
3. Use the required dispatcher for worker sessions where installed. Independent work can run concurrently, with one isolated worktree per writer. Do not substitute raw session creation when the dispatcher blocks.
4. For a native subagent workflow, use the host's agent API and owned routing policy. Use a general-purpose agent unless a specific role fits. Native hooks remain a separate, host-dependent path; do not describe them as the dispatcher's enforcement.
5. Use completion notifications when the host provides them. For CLI workers, collect the process result and routing receipt with bounded waits while continuing independent work.
6. **Cap browser-driving agents at two at a time.** Agents that only read and write files are not counted. Every agent that drives a browser also runs a dev server and a tab in one shared Chrome, and all of them contend for one profile, one window and the machine's load. Queue the rest behind the first two. The cap is a starting point from one wave (2026-09-21: four at once produced the lease, cookie and tab incidents above), not a measured limit; raise it only with the pre-flight inventory fully answered.
7. **Run the agent browser headless for the wave** (`browse-start --headless`, started by the orchestrator before dispatch). Every browse-tool command brings its tab to the front, so a headed Chrome with several agents on it takes the operator's keyboard focus every few seconds (2026-09-21). Local pages render the same headless. If a logged-in external site or a hand-off to the operator needs the headed browser, that work is not wave work; do it inline.
8. **A fixed pre-push or e2e harness port is shared across every worktree of the repo**, unlike a dev-server port, so concurrent pushes from parallel agents collide on it. If the project's browser test suite binds one port with `reuseExistingServer` (rally-hq: 5174), brief the remedy up front so an agent does not re-diagnose it as its own bug: read `test-results/*/error-context.md`; a connection refused on that port or an `SQLITE_BUSY` line is the shared harness, `rm -rf .wrangler/state test-results` and push again once nothing else holds the port; a real assertion is the agent's code. Measured 2026-09-21: five of six wave pushes were refused at least once on this port.

## Run inline work during the wait

The orchestrator stays productive. Good inline candidates while agents are out:

- Mechanical mirror-edits (new file modeled exactly on existing file with field swaps).
- Workflow / config / yaml updates the agents' artifacts depend on.
- Reading reference files you'll need for post-flight cross-review.

Don't take on work that overlaps an agent's file scope. Don't start a new thread of synthesis that contradicts an in-flight brief.

## Post-flight cross-review (mandatory, not optional)

When agents return, before commit:

1. Compare any **shared content** embedded in multiple briefs (e.g., an item enumeration the orchestrator described in both briefs). Agents will sometimes invent details that don't match. The fix is yours — agents can't see each other's outputs.
2. Verify **forward-link targets** now exist. Cross-references that pointed to files-being-written-in-parallel need to resolve after all returns.
3. Spot-check **frontmatter conformance** if the project has lint rules (`type:` enum, `canonical:` field, etc.).
4. Run any **mechanical lint** the project provides (a frontmatter lint, schema validators, whatever the repo ships).
5. Verify each agent's **file scope against the remote, not its report** — e.g. GitHub compare from the last known base to the pushed head, confirming one commit and only the briefed files. Completion reports are self-attestation: an agent saying it stayed in scope is a claim, not evidence. The compare call is the grep.
6. Verify each agent's **resource cleanup the same way**: `ps -ax -o pid,etime,command | grep <its worktree>` shows nothing, its port is not listening, and the shared agent browser's tab count has not grown. Read the port, do not recall it: `cat "$TMPDIR"/browse-tool-state-*.json` names the port the `shared` profile is on, then `curl -s http://127.0.0.1:<port>/json`. An earlier version of this step named 9339; on 2026-09-21 the browser was on 9222 and nothing listened on 9339, so the check as written would have read an empty answer as "no tabs". Close a stale tab by target id (`curl -s http://127.0.0.1:<port>/json/close/<id>`), never the browser. "I stopped my server" is self-attestation too. Kill what it left and say so in the integration receipt. Before dispatching the next wave, read the baseline the same way (load, `docker ps` stacks, tab count) so accumulation from earlier rounds is not mistaken for this one's.

An agent that goes **idle without reporting** may simply be done: check its target's ground truth first (branch heads, pushed file contents) before re-engaging. The push is the report; the narrative is optional. 2026-08-08: two of seven agents in a manifest sweep pushed correct work and idled silently — both were cross-reviewed from source faster than a re-prompt would have returned.

## Commit and push

Follow the project's commit + push conventions. For projects stamped from Blueprint (the local-integration pattern):

- Stage **specific files** with `git add path1 path2` — never `git add -A` (catches unrelated working-tree changes).
- Commit message subject in the project's conventional-commits format. Body lists what each new file does.
- Include `Co-Authored-By:` footer per the repo's agent instructions (`CLAUDE.md` or `AGENTS.md`).
- Push to `dev` per pattern 1 (local-integration) if the work is a single coherent wedge. Switch to `dev` branch first; fast-forward from `origin/dev` before committing if behind.
- Don't fight pre-push hook warnings about issue numbers in commit subjects that aren't `closes #N` candidates — they're informational.

## Skip when

- Single artifact (just write it).
- Briefs aren't complete (finish them first).
- File scopes overlap (dispatch serially or narrow scopes).
- Work needs orchestrator synthesis mid-execution (do inline).
- The artifact is a few minutes of mechanical pattern-matched editing (do inline).

## Cross-skill invocation

Consider proactively suggesting this skill when:
- A planning thread closes with ≥2 named artifacts and their structures fully discussed.
- The user says "dispatch", "wave", "in parallel", or describes work across multiple files with clear briefs.
- A `/blueprint-handoff` style handoff describes ≥2 parallel-safe next moves.

## Reference

- `tools/parallel-dispatch-check/check.sh` (blueprint-stamped projects) — pre-flight file-scope overlap detector. Run before dispatching.
- The canonical worked example (2026-05-27): three Sonnet agents drafted a feasibility doc, a sibling strategy doc, and a methodology amendment in parallel while the orchestrator wrote an ingester inline. The brief shape, the cross-review catch (on reclassification names), and the commit shape are all visible in that one diff. Blueprint's own `dispatch` skill cites the same example.

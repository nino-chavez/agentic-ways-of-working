---
name: blast-radius
description: Find what a change could break outside its own diff before it ships, and prove the one fact its safety rests on by running real code instead of writing a convincing paragraph. Use when the user says "blast radius", "what could this break", "is this safe to merge", or hands over a small-looking diff they do not trust yet; also before shipping a change to a shared contract, schema, wire format, config, or dependency version. Complement to `symbol-surgery`, which covers the references a language server can see; this covers the ones it cannot.
---

# Blast Radius

Find what a change breaks somewhere else, before it ships.

Listing callers is not the job. `symbol-surgery` step 2 (`findReferences`, `incomingCalls`) or one grep does that in seconds. The job is the breakage a reference search will not show: the consumer in another language, the column a report reads, the cached payload written by the old code, the library behavior the change quietly leans on.

## The writeup is not the deliverable

A blast-radius writeup reads as convincing whether or not it is true. That is audit discipline's self-attestation problem pointed at your own analysis: a paragraph saying "this is safe because X" is a claim, and nothing in the paragraph checks X. So the deliverable is not the paragraph. It is the one or two facts the change's safety depends on, each carried as far down this ladder as is cheap, with the rung stated.

| Rung | What you have | Worth |
|---|---|---|
| 1 | You said so | Nothing on its own |
| 2 | You pointed at the line: a real `file:line`, or the library's own source at the pinned version | A lead |
| 3 | You walked the failure path step by step and showed it does not reach | An argument |
| 4 | You ran it: a script or test that calls the real code and fails loudly if you are wrong | Evidence |
| 5 | You reproduced it in the running app, in the environment that actually runs it | Proof |

A safety fact that did not reach rung 4 is reported as **unproven**, never as settled. Rung 4 is usually one small script that imports the same library version the app ships and calls the exact function in question. Do not hand the human a check you could have run.

## Process

### 1. Read the change, including what the diff does not say

The diff, the symbols it adds, changes, and deletes, and the behavior that is now different. Pull the PR description and commit messages for stated intent. Then write down the implicit part: ordering that changed, a default that moved, an error that is now swallowed or now thrown, work that moved from one lifecycle phase to another.

### 2. Find the one fact it is safe because of

Most changes that look risky are safe because of a single fact: "this call only evicts entries that are already dead," "every writer of this column goes through this one function," "the old and new serializers emit identical bytes for existing rows." Find that fact and state it as one sentence that could be false. If it holds, most of the scary cases clear at once. Spend the time here, not on a long list of maybes.

If no single fact carries the safety, say so. That is a finding: the change is safe only by a conjunction of things, and each one needs its own rung.

### 3. Look where reference search stops

Walk this list and skip what plainly does not apply:

- **Library internals.** Read the source of the function you call, at the version the lockfile pins, including any local patch. The docs describe intent; the pinned source is what runs.
- **Timing and lifecycle.** Microtasks, teardown and unmount, retries, a second instance of the same job, cold start versus warm, build time versus request time.
- **Data at rest.** Rows, cache entries, queue messages, and local storage written by the old code and read by the new. A migration changes the schema, not the history.
- **Shapes that cross a boundary.** The JSON an API returns, a database column or row-level policy, a webhook payload, a wire format, an env var, a feature flag, anything another language or another repo reads.
- **Consumers outside the repo.** Sibling repos, scheduled jobs, dashboards, a mobile client on an older build, a CI step that shells into this code.
- **Three hops downstream.** The caller's caller that catches the error, the log parser that matches the old message, the alert keyed on the old metric name.

A search that finds nothing is an answer; record it as cleared, with the search. When the claim is "nothing else uses X," enumerate what the system has rather than searching for the name you expect X to have.

### 4. Be honest about each risk

Give each risk a real likelihood and a real cost. Keep the ones you confirmed. List the ones you checked and cleared separately, with why. Cite a real `file:line`. Never invent a caller, an API, or a consumer to make the list look thorough.

### 5. Prove the one fact

Write the script or test that runs the real code, run it, and paste what happened: the command, the output, the exit code. If it cannot be proven cheaply, mark it unproven and name what proving it would take. If the proof is a test and it is cheap to keep, it is the regression test; say where it should live.

The proof must not mutate shared or production state. Run it against a scratch copy, a fixture, a local instance, or a read-only path. If the only way to reach rung 5 is to run an evicting, writing, or deleting call against something live, stop at rung 4, report "unproven at rung 5", and name what the run would take.

Prove it where it runs. A fact proven on a laptop about code that runs in CI, on a Worker, or on a device is rung 4, not rung 5.

### 6. For a wide change, get a second family's read

For a change to a shared contract or anything with many consumers, put the same question to a model from a different family and merge the answers. Agreement is signal. A risk only one of them found still gets its own rung; do not average it away.

## What to hand back

- **What it does.** What changed, including the part that is not obvious from the diff.
- **The one fact it is safe because of.** The sentence, the rung it reached, and the proof. If it did not reach rung 4, the word "unproven".
- **Risks.** Only the real ones. Each names how it breaks, the `file:line`, how likely, how bad, and how to check. Proof pasted for the ones that matter.
- **Cleared.** What was checked and why it is fine.
- **Before you merge.** The cheapest test or repro that would catch the real bug, including the script written in step 5.

This is diagnosis. Do not fix what you find unless asked; a risk list that arrives with an unrequested patch has changed the diff it was judging.

## Relationship to other skills

- **`symbol-surgery`** — owns semantic references inside the codebase. Run its blast-radius step first when a language server is available; start here where its reference list ends.
- **`diagnose`** — for a break that has already happened. This skill is for the break that has not.
- **`evidence-audit`** — the same self-attestation rule applied to claims in a document. It has no ladder; its outcomes are confirmed, contradicted, or downgraded.
- **`/code-review`** — correctness of the diff itself. This skill is about everything the diff touches without naming.

## Provenance

Adapted from the `blast-radius` skill in Lauren Tan's pstack (`github.com/cursor/plugins`, MIT, read at `e31650e`, 2026-09-16): the five-rung ladder, the single safety fact, and the cleared-versus-confirmed split are hers. The boundary checklist, the environment rule, the unproven label, the no-mutation rule, and the diagnosis-only stop are local. pstack is Copyright (c) 2026 Lauren Tan, used under the MIT License; the full notice is in `LICENSE-pstack` beside this file.

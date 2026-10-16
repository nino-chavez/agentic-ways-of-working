# Review findings and human decisions

Use this reference when a plan has several unresolved choices or an independent
review produces findings that need action. Keep the record beside the existing
plan, issue, or decision document. A small task can use a table in that document;
it does not need another file or a review round.

This is a capture convention for people and agents using any tracker or model.
The host's permission policy, project approval rules, and worker dispatcher still
own authorization and execution.

## Bind the review to what was reviewed

Name the artifact, the exact question, and its source version before requesting
a review. Use a commit plus path for committed work, or a SHA-256 of the actual
file bytes for a draft. Include relevant evidence and the existing review limit.
Do not supply an earlier reviewer's verdict as the evidence to inspect.

A second model family can challenge different assumptions when an authorized
route supports it. Do not bypass the required dispatcher to obtain that family.
Record the requested provider separately from observed runtime identity. A
same-family review, an unknown runtime, and a human review stay distinguishable.
Agreement between reviewers does not establish correctness.

Use one bounded challenge and a focused verification of authorized fixes when
the owning review workflow permits them. Existing limits take precedence; a
changed draft or an unresolved finding never resets an exhausted allowance.

## Resolve findings against evidence

Give each finding a stable ID. Keep the reviewer's assertion separate from the
parent's source check and the action that follows it.

| ID | Finding and location | Source check | Disposition | Next action |
|---|---|---|---|---|
| F1 | Exact claim and affected source version | File, observed behavior, or test result | Confirmed / narrowed / rejected / unresolved | Authorized fix, human decision, or none |

The row above is a format example, not a finding. Keep rejected findings with
their reason so a later reviewer does not reopen them from the same evidence.
Record performed checks as results and proposed checks as plans. Do not call a
finding fixed until the relevant check has been rerun on the changed artifact.

Separate factual defects from preferences and changes to accepted decisions.
Fix a confirmed defect within the assignment. Send a product choice to its
owner. An agent's disposition is never the owner's approval.

## Present only the decisions that are ready

Follow `grill-with-docs` for question sequencing: look up facts first, group
independent questions, and wait before asking dependent questions. Use the host's
question UI or the project's existing review page. For one simple choice, ask
directly. Do not build a new page just to collect an answer.

For a larger set, show a short decision sheet. Each item carries:

- A stable ID and the concrete question the person must answer.
- The relevant artifact version and prerequisite decision IDs.
- The available options, a recommendation, and its evidence or trade-off.
- The person's explicit answer and a reference to the original reply.
- Its current state: open, answered, or deferred.

Recommendations and preselected controls are proposals. Silence leaves the item
open. A partial reply resolves only the identified items. If a reply's mapping
is ambiguous, clarify that mapping without repeating the whole interview.

Keep the original answer when the basis changes. Mark the affected item as
needing revalidation, explain the change, and ask only if the change could alter
the answer. Unaffected decisions remain settled. Do not silently apply an old
answer to different wording, options, evidence, or scope.

Record an answer in the project's existing decision owner. Preserve its source
reference, scope, and version. A local answer sheet is a working record; it is
not authenticated proof of approval. Where the project requires a trusted host
decision or approval receipt, use that existing mechanism. Never synthesize one
from a JSON field, a reviewer vote, or an agent-authored quotation.

## Check the whole assignment before splitting it

For work divided among tasks, list the requirements once with stable IDs. Map
each task to the IDs it covers, its planned verification, its read paths, its
edit paths, and the tasks it depends on. Keep this with the existing plan.

Before dispatch, check that every requirement has an owner, references resolve,
dependencies have no cycles, and concurrent tasks do not edit each other's
inputs or outputs. Shared reads are fine. A dependency makes work ordered; it
does not schedule that work. Include future files and directory scopes, not
only paths currently present on disk.

When available, use Blueprint's `tools/parallel-dispatch-check` structured plan
mode for mechanical checks. Older copies accept only edit globs; do not pass
them a manifest or treat their result as coverage evidence. Without the newer
checker, inspect the same relationships manually and state that limit.

Mechanical coverage proves that IDs and declared scopes connect. It does not
prove the tasks satisfy the requirement, the plan is complete, tests passed, or
someone authorized the work. The parent still checks those claims against the
actual deliverable. Keep runtime resource isolation in `dispatch-wave` too.

## Adaptation boundary

The review-to-decision sequence and explicit read/edit coverage were informed
by [Bridge MCP 0.2.70](https://cdn.jsdelivr.net/npm/@bridge_gpt/mcp-server@0.2.70/README.md)
and its [coherence-input implementation](https://cdn.jsdelivr.net/npm/@bridge_gpt/mcp-server@0.2.70/build/epic-coherence-input.js),
inspected on 2026-10-16. This adaptation uses existing skills and local records.
It does not adopt Bridge's service, source indexing, ticket system, provider
routing, credential store, automatic acceptance, or worker launcher. No Bridge
code is copied. Benefits to delivery time or review quality remain unmeasured.

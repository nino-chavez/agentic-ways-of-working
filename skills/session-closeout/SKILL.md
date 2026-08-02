---
name: session-closeout
description: Preserve durable lessons and produce an archive-safety receipt before ending, clearing, or archiving an agent task. Use when the operator says close out, wrap up, end the session, prepare the task for archive, make it archive-ready, or invokes $session-closeout after substantive work.
---

# Session Closeout

Keep what should survive the task without treating the full transcript as permanent memory.

## Workflow

1. Reconstruct the outcome from the workspace and external receipts. Inspect the relevant status, diff, tests, artifacts, and deployment evidence. Treat prior summaries as leads, not proof.
2. Sort the residue:
   - stable project truth that belongs in an existing canonical document;
   - a reusable method that belongs in the configured memory or recipe store;
   - chronology that can disappear with the transcript;
   - unresolved work, secrets, private material, or unverified claims that block safe archival.
3. Prefer an existing source of truth over a new recap file. Update it only when the task already authorized the underlying change. Never manufacture human approval, readiness, or evidence.
4. Save a reusable recipe only when another task is likely to benefit. Use the project's documented memory command or knowledge store. Keep the entry short, sourced, and free of raw private text. If no durable recipe exists, save none.
5. Confirm the transcript has been queued or ingested by the configured lifecycle hook. Do not perform mining inside the hook; the hook should enqueue identity and return quickly.
6. Do not archive or delete the task. Give the operator the receipt and let them take the irreversible action.

## Required receipt

End with exactly these fields:

```text
CLOSEOUT_COMPLETE
Outcome: <what landed>
Canonical updates: <paths or none>
Verified evidence: <checks or receipts>
Still unproven: <remaining claims or none>
Next action: <single concrete action or none>
Recall recipe: <recipe id/path or none>
Archive-safe: yes|no
```

Set `Archive-safe: yes` only when the durable lesson is stored, secrets are excluded, evidence is named, and nothing requires the raw transcript to resume safely.

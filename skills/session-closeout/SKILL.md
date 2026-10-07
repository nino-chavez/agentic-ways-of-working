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
3. Decide the fate of the workspace this task created. List each linked worktree, branch, and running dev server, database stack, or browser tab the task started (`git worktree list`, `git branch`, the commands it ran). Give each exactly one disposition and record it:
   - merged into the base branch, or landed by squash or cherry-pick (confirm with `git cherry <base> <branch>` or a content check): remove the worktree with `git worktree remove` (no `--force`) and delete the local branch;
   - commits not in the base branch: push the branch so the work is not only on this machine, and name the open PR or the reason it stays open;
   - uncommitted changes: commit them to the task's branch, or state why they are discarded. Never leave them undecided;
   - evidence in an ignored folder that is still needed (`.artifacts/`, receipts, a backup app): move it to the repository-level `.artifacts/` or a canonical document before removing the worktree, and name what was kept;
   - a process the task started: stop it.
   Touch only what this task created. Leave a worktree held by a live session, one a build or release depends on, and the worktree this session is running in (its host removes that one). Report anything you could not decide. Reclaiming build output in idle worktrees is the reaper's job, not this step's.
4. Prefer an existing source of truth over a new recap file. Update it only when the task already authorized the underlying change. Never manufacture human approval, readiness, or evidence.
5. Save a reusable recipe only when another task is likely to benefit. Use the project's documented memory command or knowledge store. Keep the entry short, sourced, and free of raw private text. If no durable recipe exists, save none.
6. Confirm the transcript has been queued or ingested by the configured lifecycle hook. Do not perform mining inside the hook; the hook should enqueue identity and return quickly.
7. Do not archive or delete the task. Give the operator the receipt and let them take the irreversible action.

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
Workspace: <each worktree, branch, and process this task created, with its disposition; or none>
Archive-safe: yes|no
```

Set `Archive-safe: yes` only when the durable lesson is stored, secrets are excluded, evidence is named, every worktree, branch, and process the task created has a recorded disposition, no commit exists only on this machine, and nothing requires the raw transcript to resume safely.

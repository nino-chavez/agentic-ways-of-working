# Keep the lesson, not every transcript

Agent sessions are useful working evidence. They are poor permanent storage.

A raw transcript contains chronology, tool output, repeated context, and sometimes private material. The durable part is much smaller: what changed, what proved it, what remains uncertain, and which method is worth reusing.

This pattern separates those jobs instead of asking one archive folder to do all of them.

## The incident that forced the distinction

One Codex home directory reached roughly 24 GB. The first assumption was that archived conversations caused all of it. Measurement showed two different stores growing for two different reasons:

- Cargo build output inside a linked worktree accounted for 18.5 GiB of logical files.
- 246 archived session transcripts accounted for another 5.04 GiB.
- 605 active and archived transcripts could be represented by a 34 MB SQLite recall database after mining.
- The compact store retained 553 user corrections and preferences plus 11,773 completed response examples, with source-client provenance and a watermark for every transcript.

The build output was recreated material and could be cleaned independently. The transcripts required a semantic closeout before deletion. Automatic mining alone could not prove that every project-specific lesson had been promoted.

That distinction stopped an unsafe shortcut: the archive deletion remained blocked until the operator could explicitly accept the loss of raw detail.

## The retention loop

Use five separate layers:

1. **Canonical project truth** — decisions, operating rules, and product facts live in the repository that owns them.
2. **Closeout skill** — an agent inspects evidence, promotes the durable residue, names what is still unproven, and emits an archive-safety receipt.
3. **Fast lifecycle hook** — the hook records the transcript path, task identifier, client, and working directory, then returns.
4. **Background miner** — a worker normalizes each client's transcript format, extracts only genuine human and assistant messages, stores provenance, and commits a path-plus-modification-time watermark.
5. **Retention policy** — active tasks stay raw; archived tasks get a grace window; deletion is allowed only after closeout and a current watermark.

The hook is transport. The skill is judgment. The database is retrieval. None of them replaces the others.

## Install the closeout skill

The repository installer links its skills into Claude Code. Codex users can link this one skill into their own skill directory:

```bash
mkdir -p ~/.codex/skills
ln -s "$PWD/skills/session-closeout" ~/.codex/skills/session-closeout
```

Restart the client, then say “close out,” “wrap up,” or “archive this task,” or invoke `$session-closeout` directly. The skill does not delete transcripts. It produces the evidence receipt that a separate retention policy can consume.

## Why the hook only enqueues

Lifecycle hooks have tight operational budgets and can be interrupted. Codex requires new or changed command hooks to be reviewed before they run, and `SessionEnd` supports a maximum three-second timeout. Keep that path boring: append one queue record and return. Let a scheduled worker parse transcripts and update the database outside the agent turn.

Example Codex wiring:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "session-retention enqueue",
            "timeout": 3
          }
        ]
      }
    ]
  }
}
```

`session-retention enqueue` is an interface name in this example, not a binary shipped by this repository. Point it at your own append-only queue command. Queue and miner implementations depend on the clients, transcript formats, and storage policy you actually use.

Review the exact command through `/hooks`. Do not bypass hook trust as a standing configuration. See the official [Codex Hooks guide](https://learn.chatgpt.com/docs/hooks).

Queue records should contain identity, not transcript content:

```json
{
  "source_client": "codex",
  "session_id": "…",
  "transcript_path": "…",
  "cwd": "…"
}
```

The worker must tolerate the transcript moving from an active-session folder to an archive between enqueue and processing. Resolve by stable task identifier when the original path no longer exists.

## Treat transcript formats as untrusted inputs

Different clients write different JSONL shapes. Even one client may mix real messages with injected instructions, tool output, summaries, and internal events.

Build a small normalization boundary:

```text
client JSONL
    ↓
(timestamp, role, phase, text)
    ↓
signals · response examples · recipes
```

Only mine event types verified to represent actual human or assistant prose. Keep `source_client`, canonical task identifier, project path, and timestamp beside every retained record. Fixtures should prove that injected configuration and tool results do not become user preferences.

Advance the watermark only after the database transaction commits. A useful watermark binds the exact transcript path to its current modification time. A file that changed after mining is stale and must be processed again.

## Closeout is the semantic gate

Invoke [`$session-closeout`](../skills/session-closeout/SKILL.md) before archiving substantive work. Its receipt makes the missing judgment visible:

```text
CLOSEOUT_COMPLETE
Outcome: cross-client retention pipeline installed
Canonical updates: docs/session-retention.md
Verified evidence: parser fixtures and database integrity check passed
Still unproven: old transcripts may contain project details not promoted elsewhere
Next action: review the archive after the grace window
Recall recipe: session-retention-v1
Archive-safe: no
```

`Archive-safe: no` is a successful closeout result when raw context is still carrying something important.

## Worktree artifacts are a separate cleanup problem

Build output under linked worktrees can dwarf the transcript archive. It is tempting to attach a recursive delete directly to a turn-ending hook. One live test did exactly that and removed 594 MB of `node_modules` from a real worktree outside the requested scope. The branch, source, and manifest survived, but the side effect exposed the problem with proving safety by performing the destructive action.

A worktree artifact reaper needs stronger gates than “the directory name is usually generated”:

- enumerate linked worktrees through `git worktree list`; never infer them from one folder convention;
- exclude the main checkout and every worktree held by a live session from every supported client;
- require Git to report the candidate ignored **and** require `git ls-files -- <candidate>` to return no tracked descendants;
- use a real liveness signal, not only the worktree root directory's modification time;
- plan first, then recheck every gate immediately before deletion;
- enqueue long cleanup work instead of running recursive deletion inside a short lifecycle hook;
- remove worktrees only when clean, merged into the chosen base, idle beyond policy, and removable without `--force`;
- ship report-only mode first and keep destructive mode explicitly enabled.

An ignored directory is not automatically disposable. A successful deletion is not proof that the safety model is complete.

## A practical default policy

| Material | Default retention |
|---|---|
| Active task transcript | Keep |
| Archived transcript without closeout | Keep temporarily |
| Archived transcript with closeout and current watermark | Delete after a short grace window |
| Canonical project documentation | Keep with the project |
| Reusable recipe or correction | Keep in a small searchable store |
| Reproducible build output in an idle linked worktree | Reclaim under the stronger gates above |

The target is not zero history. It is enough durable evidence to resume, audit, and improve—without paying permanent disk cost for every intermediate turn.

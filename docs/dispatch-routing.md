# Dispatch routing

A dispatch creates a bounded worker for the current task. A separate persistent
user task requires an explicit request. Task bookkeeping is not permission to
create a sidebar conversation.

## Enforce before launch

A required dispatcher owns both classification and execution. It accepts a brief
and workspace, asks the configured classifier, applies the local model policy,
and persists a routing receipt before it starts a worker. Missing credentials,
unavailable classification, invalid results, or an unwritable receipt block the
launch. It never substitutes the parent session's default model.

```text
brief + isolated workspace
  -> classification
  -> model and effort policy
  -> durable routing receipt
  -> worker launch
```

The caller must not be able to bypass the dispatcher through another exposed
creation tool. Disable those tools in the host configuration. Verify the tool
catalog after reload; changing a configuration file does not change every
already-loaded task. A hook that can be skipped, time out, or fail open is a
best-effort guard and must not be described as required enforcement.

## Operator implementation

The companion Operator repository owns `adapters/dispatch.py` and its route
policy, tests, and receipt format. Dotfiles installs the `operator-dispatch`
launcher and the Codex app-tools deny-list. Use its `docs/dispatch.md` for current
flags and limitations; this repository does not duplicate the runtime.

```sh
operator-dispatch --cwd /path/to/repo/.worktrees/task --prompt-file brief.txt
operator-dispatch --cwd /path/to/repo --prompt-file brief.txt --read-only
```

This adapter launches a bounded Codex CLI worker. It does not reproduce native
collaboration-agent lifecycle or create a desktop sidebar task. Write work needs
a linked worktree; the caller must intentionally carry forward dirty source.
Native subagents keep their separate routing integration, described below. A machine with full
shell access can still invoke the backend directly; this policy is an execution
contract, not an operating-system security boundary.

## Evidence

Test a failed classifier and unwritable receipt with a backend that records every
invocation. Both must produce zero starts. Test the successful path against the
actual runtime: the requested model and effort must match the child metadata.
A receipt written before launch proves a decision, not that the worker ran.

Carry the launcher, tool deny-list, policy owner, and tests through their tracked
repositories. Report source edits, publication, installation on each machine,
and catalog reload as separate states. Do not claim enforcement on a second
machine merely because one machine passed.

## Native hook acceptance

Native routing must classify the effective child configuration, including any
supplied model or effort and named-profile defaults. A caller-supplied model is
not evidence of user authorization. Permit choices within the classified
allowance; require a scoped user-authorized exception for excess choices. An
unknown effective model is unresolved, not automatically approved.

Where required native routing is enabled, classifier and request-record failures
must return a valid pre-tool denial. Reuse the dispatcher's classifier policy.
Do not count ordinary calls or shadow observations as escalation attempts.

Record the pre-tool result as requested. Link the tool result to the child and
compare observed model and effort with that request. A start event without effort
is partial evidence. A missing callback stays unverified. Post-start checks can
report a mismatch; they cannot retroactively prevent the launch.

Test the actual native API after installation and trust review, including an
implicit route, an effort-only choice, an excess model, a named profile, and a
classifier failure. Require both routing receipts and child runtime evidence.
The existence of a hook file is not a passing test. Keep host-specific trust
and bypass limitations in the adapter documentation.

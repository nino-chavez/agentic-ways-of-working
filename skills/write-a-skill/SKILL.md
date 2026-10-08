---
name: write-a-skill
description: Create or revise agent skills with clear triggers, focused instructions, and reusable resources. Use when a request is to create, write, or substantially update a skill.
---

# Writing Skills

Create the smallest skill that changes a future agent's decisions. Preserve the
user's scope, authorization, and stop conditions; do not turn one example into
a universal rule.

## Draft

1. Establish the task, representative use cases, and any permission or safety
   boundary that changes the work. Ask only about a missing choice that would
   materially change the skill.
2. Write a discriminating description: what the skill does and when it should
   trigger. State useful exclusions only when they prevent likely misrouting.
3. Keep shared purpose and essential constraints in `SKILL.md`. Link directly
   to a reference when detail is conditional or substantial, and say when to
   read it. Keep every rule in one owner; inspect a referenced skill or tool
   before describing what it contains.
4. Add a script only for a deterministic operation that agents would otherwise
   recreate repeatedly. Reuse and run existing deterministic helpers rather
   than copying their logic into prose.

## Size and structure

Use the structure the task needs. A simple skill can be self-contained;
substantial mode-specific instructions, schemas, and examples belong in direct
references.

Treat 500 body lines as a review heuristic, not a hard limit: move conditional
detail when doing so makes the entrypoint clearer. A long reference should have
a short contents list near its start once navigation would otherwise be hard
(around 100 lines is a useful prompt to consider one). Neither number is a
quality score or a required split point.

```text
skill-name/
├── SKILL.md            # required entrypoint
├── references/         # detailed, directly linked guidance when needed
├── scripts/            # deterministic helpers when needed
└── assets/             # files copied into outputs when needed
```

## Validate

Separate three claims in the handoff:

- **Structure-valid:** frontmatter, links, names, and scripts pass the
  available mechanical checks.
- **Behavior-proven:** a representative task produced the required observable
  behavior under its stated scope.
- **Host-loaded:** the target host discovered and loaded the skill. This is
  separate from both source validation and behavior.

For a simple, low-impact edit, source review and cheap manual validation are
enough. For a complex or consequential skill, read
[behavioral validation](references/behavioral-validation.md) and run its small
baseline/candidate comparison when the current permissions and stop limits
allow it. Use the required dispatcher for any evaluation worker; do not create
another runner or repeat its launch instructions. See
[dispatch routing](../../docs/dispatch-routing.md).

Before handoff, check the requested cases, referenced-file existence, terms,
and available frontmatter validation. Report what was not tested and why.

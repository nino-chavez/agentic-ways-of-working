---
name: symbol-surgery
description: Symbol-first refactoring discipline using the native LSP tool — locate by symbol, check blast radius before changing, edit at symbol boundaries, verify references after. Use when renaming, changing a signature, moving/extracting/inlining, or deleting code that other code depends on; or whenever the question is "where is this used and what breaks if I change it?" Mechanical-execution complement to `deepen` (which decides what to refactor) and `simplify` (which decides what's clean).
---

# Symbol Surgery

The execution discipline for refactors. `deepen` decides *what* module should change and `simplify` decides *what's clean*; this skill governs *how* the change lands — precisely, with the blast radius known before the first edit.

The whole point: stop reaching for Grep-then-guess-line-numbers by reflex. You already have a Language Server in this harness (the `LSP` tool). Symbol-granular navigation and scoped editing beat text search on precision and on tokens — no whole-file reads to locate a symbol, no broken call sites discovered after the fact.

This is the part of Serena worth keeping: symbol-first, blast-radius-aware editing. The 40-language server management is not — the native `LSP` tool already covers navigation in-process, with no MCP roundtrip.

## When to use

Use when the change touches code other code depends on:

- **Rename** a function, class, type, variable, export.
- **Change a signature** — add/remove/reorder params, change return type.
- **Move / extract / inline** a symbol across files.
- **Delete** a symbol you believe is unused.
- Answer **"where is this used and what breaks if I change it?"**

Skip it for: greenfield writing, a self-contained local edit where you already hold the full context, or non-code text. Don't run heavy navigation on a one-line tweak.

## The native LSP operations

All operations take `filePath`, `line`, `character` (1-based). `workspaceSymbol` also takes `query`.

| Need | Operation |
|---|---|
| Find a symbol's definition | `goToDefinition` |
| Find every use of a symbol | `findReferences` |
| Type / doc at a position | `hover` |
| All symbols in one file (with ranges) | `documentSymbol` |
| Find a symbol by name across the repo | `workspaceSymbol` (always pass `query`) |
| Implementations of an interface/abstract | `goToImplementation` |
| Who calls this function | `incomingCalls` (after `prepareCallHierarchy`) |
| What this function calls | `outgoingCalls` |

## Process

### 1. Locate — never guess a line range

To act on a symbol, first get its real identity and span:

- In a known file: `documentSymbol` returns every symbol with its range. Edit at those boundaries.
- By name across the repo: `workspaceSymbol` with `query`.
- Unsure what a name refers to: `hover` / `goToDefinition`.

This replaces reading the whole file to find where something lives.

### 2. Blast radius — before you change anything

For any rename, signature change, move, or delete, enumerate callers **first**:

- `findReferences` on the symbol → every use site.
- `incomingCalls` (via `prepareCallHierarchy`) → the call graph reaching it, when references alone undersell the coupling.

This is the step that prevents the classic "renamed it, broke six call sites you didn't know about." If the reference list is larger than expected, that *is* the finding — surface it before editing.

### 3. Edit at symbol scope

The symbol-level editors Serena ships are compositions of tools you already have. Use these recipes:

- **Replace a symbol's body** → `documentSymbol` to get the range, then `Edit` that range. Don't hand-count lines.
- **Insert before/after a symbol** → `documentSymbol` for the boundary, then `Edit` at it.
- **Safe-delete** → `findReferences` to assert zero callers (or migrate them first), then `Edit` to remove. Never delete on the assumption it's dead — prove it.
- **Rename / signature change** → edit the definition, then `Edit` every site from step 2. The reference list is your checklist.

### 4. Verify — references, not vibes

After the edit, confirm the change is complete and consistent:

- Rename: `findReferences` on the **old** name returns zero; the **new** name returns the count you expected.
- Signature change: `hover` / type-check resolves at the call sites you touched.
- Delete: `findReferences` returns zero (it should, since step 3 proved it).

Don't declare a refactor done until the references say it is.

## When LSP isn't available

The language server must be configured for the file type, or the operation errors. If it's unavailable, fall back to `Grep` for navigation — but say so, and treat the blast radius as *approximate*. Grep finds text matches, not semantic references: it misses re-exports and aliasing, and it produces false hits on comments and same-named-but-unrelated symbols. State the reduced confidence rather than implying full coverage.

## Relationship to other skills

- **`deepen`** — decides which modules should be reshaped (depth/leverage). Hand its chosen refactor to this skill to execute.
- **`simplify`** — code-quality cleanups on a diff. This skill is how those cleanups land safely when they cross call sites.
- **`diagnose`** — when the refactor is in service of a bug, diagnose first, then operate.

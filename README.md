# Agentic Ways of Working

There are two honest ways to share how you work with AI, and both are incomplete.

You can publish your dotfiles — but those carry your secrets, your machine paths, and a corpus of your own prompts. What ships isn't a method; it's a fingerprint. Or you can write the essay — the principles, the philosophy, the "here's how I think about agents" post. That travels anywhere, and installs into nothing.

This repo is the layer between. The transferable middle: the rules an agent actually reads, the skills it actually runs, the hooks that actually fire — extracted from a private cross-machine setup and stripped of everything that was *me* rather than *method*. You can clone it, run one script, and have the same working stance enforced in your own sessions by tonight.

## The one idea worth stealing

Every harness wants its own rules file. Claude Code reads `CLAUDE.md`. Codex reads `AGENTS.md`. Gemini reads `GEMINI.md`. The naive move is to maintain three copies and watch them drift until they quietly disagree about how you work.

So there's exactly one canonical doc — [`principles/working-style.md`](principles/working-style.md) — and each harness gets a thin [adapter](adapters/) that points at it. Edit the principles once; every tool picks up the change on its next read. The principles themselves assume nothing about which model is driving.

What's in them:

- **Decision bias — default to action.** The single highest-leverage rule. Agents pad turns with "want me to keep going?" permission-checks that cost you the one thing you can't get back: the time spent waiting to say yes to something that didn't need asking. The rule names the failure shape precisely, and a [Stop hook](hooks/anti-hesitation.py) enforces it — if the agent ends a turn on a hesitation question, it gets bounced back to restate it as a status sentence.
- **Canonical-pattern-first for infrastructure.** Before writing auth or payments or webhooks, read the vendor's recommended approach and check your own repos for a working version. Custom shapes have to name what disqualified the canonical one. Silence isn't an answer.
- **Worktree isolation.** The moment two sessions touch one repo, they get separate git worktrees — otherwise they switch each other's branches mid-flight and commits land in the wrong place. A [PreToolUse hook](hooks/worktree-guard.py) enforces it: a contended git op in a shared checkout while another session is live gets denied, with the `git worktree add` remedy handed back.
- **Prose-voice and fabrication guardrails.** Prose for a human reader loads the voice guide first; reflective prose never invents an interior state the author didn't actually have.
- **Secret handling.** Secrets live in a manager, never in files, and you check for an existing entry before inventing a new name — the convention that prevents silent install-time drift.

## What's in the box

```
principles/working-style.md   The canonical doc. Harness-agnostic. Start here.
adapters/                     Thin pointers: CLAUDE.md, AGENTS.md, GEMINI.md
skills/                       The methodology suite (see below)
commands/campsite.md          /campsite — toggle the north-star working stance
hooks/                        anti-hesitation, campsite, blueprint-session-start, worktree-guard
git-hooks/                    Local post-commit Claude review on commits worth reviewing
docs/                         Analysis & essays — e.g. the Aquifer substrate convergence
install.sh                    Symlinks it all into ~/.claude and wires the hooks
```

### Skills

The craft skills are self-contained — drop them in and they work:

| Skill | What it does |
|---|---|
| `campsite` (command + hook) | Toggle "leave it better than you found it": no shims, fix-as-discovered, touched files end clean |
| `deepen` | Find architectural deepening opportunities (Ousterhout's depth/seam vocabulary, the deletion test) |
| `symbol-surgery` | Symbol-first refactoring discipline on the native LSP tool: locate by symbol, check blast radius, edit at boundaries, verify references (executes what `deepen` decides) |
| `diagnose` | Disciplined hard-bug loop: reproduce → minimise → hypothesise → instrument → fix → regression-test |
| `grill-with-docs` | Stress-test a plan against your domain model and documented decisions |
| `tdd` | Red-green-refactor with deep-module and mocking guidance |
| `triage` | Move issues through a state machine driven by triage roles |
| `ship` | Deterministic deploy: reads a per-project `DEPLOY.md` as source of truth |
| `zoom-out` | Pull up from the diff to the system before committing to a direction |
| `write-a-skill` | Author new skills with progressive disclosure and bundled resources |

The `blueprint-*` suite (`research`, `prototype`, `docs`, `validate`, `deploy`, `dispatch`, `triage`, `handoff`, `amendment`) encodes a brownfield-redesign methodology — diagnose current state, prescribe, prototype the proposed state. These are the most opinionated artifacts here and they pair with a separate Blueprint methodology repo; point `BLUEPRINT_HOME` at your clone of it. Take them as a worked example of how far you can push a methodology into skills, not as a turnkey drop-in.

### Git hooks

The Claude Code hooks above fire inside a session. [`git-hooks/`](git-hooks/) is the other direction: a git `post-commit` hook that runs a focused Claude review *after* a commit — but only on the commits worth it (a configurable line threshold, or a touched sensitive path like `auth/` or `*.sql`). It's the local-first answer to a PR-gated review action: no PR required, async so the commit returns instantly, and billed to your Claude subscription rather than a metered API key (it unsets `ANTHROPIC_API_KEY` before calling `claude`). Merge commits and rebase replays are skipped so a ten-commit rebase doesn't fan out ten reviews. Self-contained with its own `install.sh` — see [`git-hooks/README.md`](git-hooks/README.md).

## Install

```bash
git clone https://github.com/nino-chavez/agentic-ways-of-working.git
cd agentic-ways-of-working
./install.sh            # symlinks into ~/.claude, wires hooks, backs up settings.json
# ./install.sh --copy   # copy instead of symlink, if you'd rather not track this repo
```

The installer is idempotent and reversible: it backs up `settings.json` before touching it, only adds hook registrations that aren't already present, and symlinks by default so `git pull` updates everything in place. It won't overwrite your existing rules file — that one line is left for you to run deliberately (it prints the command).

Then restart your session so the hooks load. Toggle the north-star stance per-project with `/campsite on`.

## Usage in practice

The point isn't the files — it's what changes in a session. Three things you'll notice once it's installed.

**1. The agent stops asking permission to continue.** The decision-bias principle plus the `anti-hesitation` Stop hook mean a turn can't end on a permission-gating question when the direction is already set. If the model tries to close with one, the hook bounces it:

```
✗  "I've drafted the migration. Want me to apply it next, or stop here?"
       └─ Stop hook fires → forces a restate

✓  "Migration drafted and applied; tests green. Moving to the rollback script next."
```

One "go" covers the whole thread, not one step at a time.

**2. Campsite Mode raises the bar, per-project.** Turn it on when you want north-star-only work — no shims, fix-as-discovered, touched files end clean:

```bash
/campsite on      # → "Campsite Mode ON — north-star only, fix-as-discovered, touched files end clean."
# ... do the work; pre-existing defects in files you touch get fixed in the same change ...
/campsite off     # → back to scoped, surgical edits
```

The flag lives at `<project>/.claude/campsite-mode`, so each repo opts in independently.

**3. Skills fire on intent, not ceremony.** You don't memorize commands — describe the work and the matching skill engages:

```
"this test is flaky and I can't reproduce it"        → diagnose   (reproduce → minimise → instrument → fix)
"does this module actually pull its weight?"          → deepen     (depth/seam/locality, the deletion test)
"ship it"                                             → ship       (reads the project's DEPLOY.md, executes)
"stress-test this plan against our domain model"      → grill-with-docs
```

Same principles apply whichever harness reads them — point Codex at `adapters/AGENTS.md` or Gemini at `adapters/GEMINI.md` and the decision-bias, canonical-first, and worktree rules travel unchanged.

## What's deliberately not here

The personal layer. No voice corpus, no recall database, no secrets, no machine paths, no project-specific context. Those are what made the private version *mine*; none of them would make the public version *yours*. If a skill or hook references a path that doesn't exist in your setup, it degrades quietly rather than failing — that's intentional.

This is a snapshot of a working method, not a finished standard. The principles have been revised more times than the commit history will show, and they'll keep moving. Fork it, cut the parts that don't fit how you think, and let the rest earn its place.

## License

MIT — see [LICENSE](LICENSE).

---
name: ship
description: Deploy, release, or run migrations against production for a project. Use when user says "deploy", "ship", "push to prod", "release", "run the migration", "apply migrations", or asks how a project gets deployed / where it lives. Reads per-project DEPLOY.md as source of truth; falls back to inference + offer to scaffold one.
---

# Ship

Deterministic deployment workflow. Reads the project's `DEPLOY.md` and executes it. If none exists, infers the shape from project files and offers to write one.

The goal: stop burning cycles re-discovering "where does this deploy / how do migrations apply / what can the agent actually run" every session.

## Workflow

### 1. Find the project root + DEPLOY.md

From the current directory or the file under discussion, walk up to the nearest `package.json`, `pyproject.toml`, `Cargo.toml`, or `.git/`. That's the project root.

Look for `DEPLOY.md` at the project root.

- **Present** → use it as the source of truth. Don't second-guess it. If something looks stale, flag it but follow the stated workflow until corrected.
- **Missing** → go to step 2.

### 2. Infer + offer to scaffold (only if DEPLOY.md missing)

Read these in parallel to infer the deploy shape:

- `package.json` (scripts, dependencies — `next`, `astro`, `@sveltejs/kit`, etc.)
- `wrangler.toml` / `wrangler.jsonc` → Cloudflare Workers/Pages
- `vercel.json` / `vercel.ts` → Vercel
- `supabase/config.toml` → Supabase project ref
- `.github/workflows/*.yml` → CI-triggered deploys
- `netlify.toml`, `fly.toml`, `railway.toml`, `render.yaml`
- `git remote -v` → where pushes go

Offer to scaffold a `DEPLOY.md` from [template.md](template.md), pre-filled with what you inferred. Show the inferred values; ask the user to confirm or correct before writing. (This is the one place to ask — getting this wrong contaminates every future ship.)

### 3. Preflight (before any deploy action)

Run these checks. Report any that fail; ask before proceeding.

- `git status` — uncommitted or unpushed work? Naming the branch you're on matters; deploys to main vs preview behave differently.
- Pending migrations? Check the project's migration directory against what's deployed (if the manifest specifies a check command, run it).
- Required env vars present in the target environment? The manifest should list them; if it doesn't, name what you'd expect based on the code and stop.

### 4. Trigger the deploy

Follow the manifest. Most common shapes:

- **Push-to-deploy (Vercel / Cloudflare Pages / Netlify with GitHub integration)**: `git push` and report the expected build URL. Do not run the platform CLI to "redeploy" unless the manifest says to — push-triggered builds are the canonical path; CLI re-deploys can race.
- **Manual CLI deploy**: run the exact command in the manifest. Don't substitute flags.
- **CI-gated**: push and link to the workflow run.

**Never assume push-to-deploy is wired.** A Cloudflare Pages project, a Vercel project, or a Netlify site can all exist *without* the git integration enabled — in which case `git push` does nothing and you have to run the platform CLI yourself. The integration is configured in the dashboard, not the repo, so you cannot infer it from the codebase alone.

Signals that push-to-deploy IS wired (any one is sufficient):
- The manifest says so (canonical source — trust it).
- A `.github/workflows/*.yml` file deploys on push.
- A `.vercel/project.json` exists AND project is linked AND user has confirmed it deploys on push (the file alone doesn't prove the integration is active).

If none of those are true, treat the project as manual-CLI-deploy. Don't push and wait. Don't poll Pages dashboards "in case it picks up." Run the CLI command, or ask the user which mode this project uses.

### 5. Verify

After triggering, do whatever the manifest's verify step says. If no verify step is specified:

- Curl the prod URL's health endpoint if one exists
- Report the expected build URL and an estimated build time
- Name the runtime errors that could appear if a step was skipped (e.g., "the deploy will succeed, but route X will 500 because the migration hasn't run")

End with a status sentence, not a question.

## Authority limits — things the agent CANNOT run

These fail silently or hit auth walls. Don't waste a turn trying; instead, hand the exact command to the user OR use the documented fallback.

| Command | Why it fails | What to do instead |
|---|---|---|
| `supabase db push` against prod | Requires `supabase login` state the agent's shell doesn't have, OR `SUPABASE_DB_PASSWORD` env var. Returns 403 "your account does not have the necessary privileges". | Either (a) ask user to run `supabase db push` themselves, or (b) paste migration SQL into Supabase dashboard → SQL Editor. Manifest should specify which. |
| `vercel deploy --prod` without `VERCEL_TOKEN` | Requires interactive `vercel login`. | Push to main if project is GitHub-linked (canonical). Otherwise ask user to run. |
| `wrangler deploy` / `wrangler pages deploy` without prior login | Requires `wrangler login` browser flow OR `CLOUDFLARE_API_TOKEN`. **Usually already authed** because `wrangler login` persists to `~/.wrangler/`. Try it; if it 401s, ask user to run `wrangler login`. |
| `gh auth refresh` / anything needing TOTP | Browser/device auth. | Hand off. |
| Anything that needs an unlocked 1Password CLI session | `op` requires `eval $(op signin)` in the user's shell. | Either ask user to run with their session, or instruct them to fetch the specific secret and paste it. |
| Production database writes via `psql` / direct connections | Connection strings with prod credentials are not in the agent shell. | Use the platform's migration mechanism (Supabase dashboard, etc.). |

When you hit one of these, stop. Don't retry with different flags. State the limit, give the user the exact command to run, and continue with whatever can be done without that step.

## Common platform shorthand

For when the manifest is terse and you need to fill in standard behavior. **All "X-linked" notes below require evidence — see step 4. Don't assume linkage from repo files alone.**

- **Cloudflare Pages, GitHub-linked**: push to `main` triggers production build; PR branches trigger preview builds at `<branch>.<project>.pages.dev`. Build runs in CF's environment.
- **Cloudflare Pages, manual (standalone project)**: requires `npm run build` (or framework equivalent) THEN `wrangler pages deploy <build-output-dir>`. SvelteKit + adapter-cloudflare outputs to `.svelte-kit/cloudflare`. Astro outputs to `dist`. Next.js via Cloudflare adapter outputs to `.vercel/output/static`. The wrangler CLI usually works in the agent shell because `wrangler login` persists to `~/.wrangler/config/`.
- **Vercel, GitHub-linked**: push to production branch (usually `main`) triggers prod build. Preview URL per branch + per PR.
- **Vercel, manual**: `vercel deploy --prod`. Requires `VERCEL_TOKEN` or interactive login the agent can't do — usually hand off to user.
- **Supabase migrations**: files in `supabase/migrations/` apply in filename order. The dashboard SQL Editor always works for the agent; `supabase db push` usually doesn't (see authority limits).
- **GitHub Actions deploys**: the workflow is the source of truth; trigger conditions are in `on:`. Don't run deploy scripts manually if a workflow exists for the same step — you'll desync.

## When the user says "deploy this"

1. Read `DEPLOY.md` (or scaffold one).
2. Preflight: `git status`, list any pending migrations.
3. State what you're about to do in one sentence.
4. Do it.
5. Report what's happening, what could go wrong, and end with a status sentence.

Don't ask permission for each step inside an already-authorized ship.

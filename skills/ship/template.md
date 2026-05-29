# DEPLOY.md template

Copy this to the project root as `DEPLOY.md`. Fill in only the fields that apply; delete the rest. Keep it terse — this is read every ship.

```markdown
# Deploy — <project name>

## Host
- **Platform**: Cloudflare Pages | Vercel | Netlify | Fly.io | self-hosted | ...
- **Project / app ID**: <dashboard slug or ID>
- **Production URL**: https://...
- **Preview URL pattern**: https://<branch>.<project>.pages.dev (or N/A)

## Deploy trigger
- **Canonical**: push to `main` (auto via GitHub integration)
- **Manual fallback**: `<exact CLI command>` (only if push-to-deploy is broken)
- **Build time**: ~<N> minutes typical

## Database
- **Provider**: Supabase | Neon | PlanetScale | RDS | ...
- **Project ref / connection**: <ref or env var name; never paste secrets>
- **Migrations live in**: `<path>` (e.g., `supabase/migrations/`)
- **Apply via**: Supabase dashboard SQL Editor | `wrangler d1 migrations apply` | ...
- **Agent can run migrations?**: NO (auth scope) | YES (with `SUPABASE_DB_PASSWORD` from 1Password item "<name>")

## Environment variables
- **Where they live**: Cloudflare dashboard | Vercel dashboard | `.env.local` only | 1Password item "<name>"
- **Required for deploy**: list the names (not values)
- **Pull command**: `vercel env pull` | `wrangler secret list` | manual sync

## Domains
- <primary domain>
- <any aliases>

## Preflight checks (run before every ship)
- `git status` must be clean on the deploy branch
- `<command to check pending migrations>` (if applicable)
- `<command to verify env parity>` (if applicable)

## Verify after deploy
- Curl: `curl -fsSL https://.../health`
- Watch: `<dashboard or logs URL>`
- Known runtime errors if a step is skipped: <e.g., "/t/[slug] 500s until migrations apply">

## Authority limits specific to this project
Anything the agent cannot do for THIS stack (in addition to the general list in the ship skill):
- e.g., "Cannot rotate the Stripe webhook secret — requires dashboard MFA"
- e.g., "Cannot modify Cloudflare DNS — managed by infra team"

## Notes
- One-liner reminders. Quirks. Past incidents worth remembering.
- e.g., "2026-03-12: deploy bricked because CF Pages env var rename was case-sensitive"
```

## Why these fields

Each one closes a real failure mode that's happened in past sessions:

- **Host + trigger** — so the agent knows whether to `git push` or run a CLI deploy
- **Migration apply mechanism + agent-can-run flag** — directly closes the `supabase db push` 403 trap
- **Env var location** — so the agent stops guessing where to look for missing env vars
- **Authority limits specific to project** — captures stack-specific dead-ends without bloating the global skill

## When to update

- Migration mechanism changes (e.g., switched from CLI to dashboard)
- Host changes (Vercel → Cloudflare)
- A deploy fails for a reason that should have been documented — add a "Notes" line

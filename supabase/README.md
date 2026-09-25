# supabase/

**Owner:** `backend` subagent (schema, migrations, API, scoring, auth). `sre` owns scheduled-function config, environments, and backups.

Placeholder. `backend` will run `supabase init` here, which creates the standard layout:

- `migrations/`: timestamped SQL migrations, the only way the schema changes. Applied to dev first, then prod.
- `functions/`: Edge Functions (API endpoints, scheduled jobs such as the availability sync).
- `config.toml`: local stack config for `supabase start`.

Rules:
- No secrets in this directory. Function secrets are set with `supabase secrets set` per project (see `docs/ENVIRONMENTS.md`).
- `supabase/.branches/` and `supabase/.temp/` are gitignored.
- A migrations check will be added to CI once the first migration lands.

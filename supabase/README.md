# supabase/

**Owner:** `backend` subagent (schema, migrations, API, scoring, auth). `sre` owns scheduled-function config, environments, and backups.

Placeholder. `backend` will run `supabase init` from the repo root, which fills this directory with the standard layout:

- `config.toml`: config for the local stack (`supabase start`, Docker). **This is the whole database environment for Phases 0–1** (local-first, DECISIONS 2026-09-26). The API is at `http://127.0.0.1:54321`, and `supabase status` prints the keys and DB URL for `.env`.
- `migrations/`: timestamped SQL migrations, the only way the schema changes.
- `functions/`: Edge Functions (API endpoints, scheduled jobs such as the availability sync).

Rules:
- Migrations flow **local → dev (Phase 2+) → prod (TestFlight+)**. Nobody edits a hosted schema in the dashboard.
- No secrets in this directory. Hosted function secrets are set with `supabase secrets set` per project (see `docs/ENVIRONMENTS.md`).
- `supabase/.branches/` and `supabase/.temp/` are gitignored.
- A migrations check will be added to CI once the first migration lands.

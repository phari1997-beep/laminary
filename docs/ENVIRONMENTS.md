# Environments, secrets, and CI

Owner: `sre`. Last updated: 2026-09-25 (Phase 0).

This covers where Laminary runs, where each secret is kept, and the account setup only Hari can do. Nothing here has been created yet. Every cloud resource below is a **Hari action**, because Hari owns all accounts and billing (`CLAUDE.md`).

---

## 1. Environments

| | local | dev | prod |
|---|---|---|---|
| Purpose | Day-to-day work, tests | Integration, pipeline pilots, TestFlight builds | Real users |
| Database | `supabase start` (Docker, local Postgres + pgvector) or the dev project | Supabase project `laminary-dev` | Supabase project `laminary-prod` |
| Secrets live in | `.env` at repo root (gitignored) | GitHub Environment `dev` + Supabase dashboard (dev) | GitHub Environment `prod` + Supabase dashboard (prod) |
| Migrations applied | By hand (`supabase db reset`) | Automatically on merge to `main` (workflow added in Phase 2) | Manually triggered, after dev has run the same migration |
| App builds | Expo Go / dev client | EAS profile `preview` (internal distribution / TestFlight) | EAS profile `production` (store builds) |
| Data | Fixtures | 500-title pilot, then full catalog | Full catalog |

Rules:
- **Two separate Supabase projects**, not one project with two schemas. Separate keys mean a leaked dev key can't touch prod, and a bad dev migration can't break users.
- **Dev never holds prod credentials**, and local `.env` points at dev only. Anything needing prod credentials runs in CI under the `prod` GitHub Environment, which requires Hari's approval to run.
- Migrations flow one way: local, then dev, then prod. Nobody edits the prod schema in the dashboard.
- EAS build profiles (`development`, `preview`, `production`) will go in `app/eas.json` when `frontend` sets up the Expo project (Phase 3). Each profile reads the `EXPO_PUBLIC_*` values for its environment.

---

## 2. Secrets

Rule: **no secret is ever committed.** Every variable is listed, with empty values, in `/.env.example`. `.env` and `.env.*` are gitignored, and `.env.example` is explicitly allowed through.

### Where each secret is stored

| Variable | Secret? | local | GitHub Actions | Supabase Edge Functions | EAS (app build) |
|---|---|---|---|---|---|
| `SUPABASE_URL`, `SUPABASE_PROJECT_REF` | no | `.env` | Environment variables (`dev`/`prod`) | built in | as `EXPO_PUBLIC_SUPABASE_URL` |
| `SUPABASE_ANON_KEY` | public, protected by RLS | `.env` | Environment variable | built in | as `EXPO_PUBLIC_SUPABASE_ANON_KEY` |
| `SUPABASE_SERVICE_ROLE_KEY` | **yes** | `.env` (dev key only) | Environment secret | built in | **never** |
| `SUPABASE_DB_URL` | **yes** (contains DB password) | `.env` (dev only) | Environment secret | n/a | **never** |
| `SUPABASE_ACCESS_TOKEN` | **yes** | not needed (use `supabase login`) | Repository secret | n/a | **never** |
| `TMDB_API_KEY` | **yes** | `.env` | Environment secret | `supabase secrets set` | **never** |
| `ANTHROPIC_API_KEY` | **yes** | `.env` | Environment secret | `supabase secrets set` if used there | **never** |
| `EMBEDDING_API_KEY` | **yes** | `.env` | Environment secret | `supabase secrets set` | **never** |
| `AVAILABILITY_API_KEY` | **yes** | `.env` | Environment secret | `supabase secrets set` | **never** |
| `POSTHOG_KEY`, `POSTHOG_HOST` | public (write-only ingestion key) | `.env` | Environment variable | n/a | as `EXPO_PUBLIC_POSTHOG_*` |
| `EXPO_TOKEN` | **yes** | not needed (use `eas login`) | Repository secret | n/a | n/a |
| `JOB_HEARTBEAT_URL` | yes (anyone holding it can fake a heartbeat) | not needed | Environment secret | `supabase secrets set` | **never** |

Rules:
- Anything prefixed `EXPO_PUBLIC_` ships inside the app binary and web bundle. Only the Supabase URL, anon key, and PostHog ingestion key may carry that prefix.
- Use separate keys for dev and prod for every vendor that allows it (Supabase always does; for Anthropic, create two keys in the Console so either can be revoked on its own and spend can be split by key).
- **Rotation:** rotate any key that shows up in a log, screenshot, chat, or commit. Revoke first, then rotate, then check the vendor's usage page for abuse. Detailed steps go in `docs/RUNBOOK.md` once there are live keys.
- **Detection:** CI runs gitleaks over the full git history on every push and PR (`secret-scan` job). GitHub's own secret scanning and push protection should also be on (Hari checklist below).

---

## 3. CI (`.github/workflows/ci.yml`)

Runs on push to `main`, on PRs to `main`, and on manual dispatch. It uses no secrets.

| Job | What it does | Expected duration |
|---|---|---|
| `pipeline` | Python 3.12, `pip install -e .[dev]`, `ruff check`, `pytest` in `pipeline/` | about 1 min |
| `app` | Passes as a no-op until `app/package.json` exists, then runs `npm ci`, `npm run lint --if-present`, `npm test --if-present` on Node 22 | under 10 s now |
| `secret-scan` | Installs a pinned, checksum-verified gitleaks 8.30.1 and scans the full history | under 30 s |

Conventions:
- Actions are pinned to commit SHAs. Update them deliberately (Dependabot for `github-actions` can be added later).
- Workflow permissions default to `contents: read`.
- Deploy workflows (migrations, Edge Functions, EAS builds) come later as separate files gated on GitHub Environments. They are not part of this skeleton.
- **Minutes budget:** billing rounds each job up to a whole minute, so one CI run costs about 3 billed minutes. On a private repo on GitHub Free (2,000 min/month) that allows roughly 650 runs a month. On a public repo, minutes are free.

Local equivalent of the `pipeline` job:

```sh
cd pipeline
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && pytest
```

---

## 4. Hari's account-setup checklist

Nothing here has been done. Items marked **[OPEN]** need a decision, and **$** means a recurring cost is possible. Do them in order when each phase needs them. The phase column shows the latest point each one can wait until.

| # | Action (Hari) | Needed by | Cost | Notes |
|---|---|---|---|---|
| 1 | Push this repo to GitHub, **decide public or private** **[OPEN]** | Phase 0 | Free | Private on GitHub Free: 2,000 Actions min/month. Public: unlimited minutes, but code is visible |
| 2 | Turn on GitHub **secret scanning + push protection** and **Dependabot alerts** (Settings, then Code security) | Phase 0 | Free | Push protection blocks known key formats before they land |
| 3 | Protect `main`: require PRs and the `pipeline` and `secret-scan` checks to pass | Phase 0 | Free | Add `app` as required once the Expo project exists |
| 4 | Create GitHub Environments `dev` and `prod`; set `prod` to require Hari as reviewer | Phase 1 | Free | Deploy workflows read secrets from these |
| 5 | Create Supabase org + project `laminary-dev` (US region), enable `pgvector` | Phase 1 | Free tier | Store the DB password in a password manager, never in the repo |
| 6 | Create Supabase project `laminary-prod` **[OPEN] plan: Free vs Pro** | Phase 2 | **$** Pro is about $25/month | Free projects **pause after about 1 week of inactivity** and have no self-serve backups. That's unacceptable for prod once users exist. Recommend Pro for prod from TestFlight onward; dev stays free. Confirm current pricing at signup |
| 7 | Create a Supabase personal access token for CI and add it as the `SUPABASE_ACCESS_TOKEN` repo secret | Phase 2 | Free | Only needed when deploy workflows exist |
| 8 | Anthropic Console: create org, set a **monthly spend limit**, create keys `laminary-dev` and `laminary-prod` | Phase 1 | **$** usage-based | Pilot of 500 titles first, per PLAN 5.4. Hari approves scale-up spend |
| 9 | TMDB account + API key; **confirm commercial terms** (PLAN 8) | Phase 1 | Free for non-commercial use | A commercial agreement may cost money **[OPEN]** |
| 10 | Embedding provider account **[OPEN]** (data-pipeline to propose) | Phase 1 | **$** usage-based | |
| 11 | Availability vendor contract **[OPEN]** (PLAN 5.3, 10.3) | Phase 2 | **$** likely the largest recurring cost | Vendor comparison due before Phase 2 |
| 12 | PostHog Cloud project (US), separate dev/prod projects | Phase 3 | Free tier | |
| 13 | Job-heartbeat / uptime monitor **[OPEN]**, e.g. a free-tier dead-man's-switch service | Phase 2 (before the first scheduled job) | Free tier | sre will propose options with a comparison. Needed to alert on failed or stale availability syncs |
| 14 | Expo account + EAS; create `EXPO_TOKEN` | Phase 3 | Free tier; **$** if build minutes run out | |
| 15 | Apple Developer Program | Phase 3 (TestFlight) | **$** $99/year | |
| 16 | Google Play Console | Phase 4 | **$** $25 one-time | |
| 17 | Password manager vault for all of the above; turn on 2FA on every account | Phase 0 | | Recovery codes stored offline |

When an item is done, hand the values to the coordinator **through the vendor dashboard or GitHub settings, not through chat**. Agents only need to know that a secret exists and what it is called.

---

## 5. Not done yet (tracked for later phases)

- `docs/RUNBOOK.md`: rotation, restore, and job-failure procedures (once there is something to run).
- `docs/SLOS.md`: targets for rec latency and availability data age (before Phase 2 exit).
- Migrations check in CI (when `supabase/migrations/` has its first file).
- Backup and tested restore procedure (depends on item 6).

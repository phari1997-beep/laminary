# Environments, secrets, and CI

Owner: `sre`. Last updated: 2026-09-26 (Phase 0).

This covers where Laminary runs, where each secret lives, and the account setup only Hari can do. No cloud resource has been created yet. Every hosted resource below is a **Hari action**, because Hari owns all accounts and billing (`CLAUDE.md`).

Governing decisions (`docs/DECISIONS.md`):
- **2026-09-25:** the GitHub repo is **private**, on **GitHub Free** for a personal account.
- **2026-09-26:** data is **local-first**. Phases 0–1 use the Supabase CLI locally in Docker. A hosted free-tier **dev** project comes at Phase 2, and a hosted **Pro prod** project only when TestFlight testers arrive in Phase 3.

---

## 1. Environments

| | local | dev | prod |
|---|---|---|---|
| Exists from | Phase 0 | Phase 2 | Phase 3 (TestFlight) |
| Purpose | All work in Phases 0–1, including the 500-title pilot. Day-to-day work and tests after that | Integration, availability sync, scaling annotation, scoring API | TestFlight testers, then store users |
| Database | `supabase start` (Docker, local Postgres + pgvector), configured by `supabase/config.toml` | Supabase project `laminary-dev`, **Free** plan | Supabase project `laminary-prod`, **Pro** plan (about $25/month) |
| Secrets live in | `.env` at repo root (gitignored) | GitHub repo secrets with a `_DEV` suffix, plus the Supabase dashboard (dev) | GitHub repo secrets with a `_PROD` suffix, plus the Supabase dashboard (prod) |
| Migrations applied | By hand (`supabase db reset`) | By a deploy workflow after merge to `main` (added in Phase 2) | By a `workflow_dispatch`-only deploy workflow that **only Hari** triggers, by hand (DECISIONS 2026-09-26), after the same migration has run on dev |
| App builds | Expo Go / dev client | EAS profile `preview` (internal distribution, for the team only) | EAS profile `production`: **TestFlight builds and store builds** |
| Data | Fixtures, then the 500-title pilot (Phase 1) | Full catalog as it scales (Phase 2) | Full catalog |

Rules:
- **Local-first until Phase 2.** In Phases 0–1, `.env` points at the local stack: `SUPABASE_URL=http://127.0.0.1:54321`, with keys and the DB URL from `supabase status`. From Phase 2 it points at dev. The local stack's keys are fixed defaults that exist only on your machine. Still, don't paste them anywhere; gitleaks will flag them.
- **Two separate hosted projects**, not one project with two schemas, so a leaked dev key can't touch prod and a bad dev migration can't break users.
- **Your local `.env` never holds prod credentials.** Anything that needs them runs in a `deploy-prod*.yml` workflow that is triggered **only** by `workflow_dispatch` (a manual run from the Actions tab). Until Phase 3 this dispatch-only rule is a convention, not a CI check (section 2). GitHub Free has no approval gate for private repos (section 3). The manual trigger and the CI *prod-secret tripwire* (section 2) are a tripwire against accidental misuse, not access control: on GitHub Free, any workflow on any branch can read all repo secrets, so **write access to the repo is the real security boundary**.
- Migrations flow one way: local, then dev, then prod. Nobody edits the prod schema in the dashboard.
- **TestFlight points at prod.** EAS profiles go in `app/eas.json` once `frontend` sets up the Expo project (Phase 3): `development` for the local or dev stack, `preview` for internal builds against dev, and `production` for TestFlight and store builds against prod.

---

## 2. Secrets

Rule: **no secret is ever committed.** Every variable appears, with empty values, in `/.env.example`. The one exception is the local `SUPABASE_URL`, which isn't a secret. `.env` and `.env.*` are gitignored, and `.env.example` is explicitly allowed through.

**Naming in GitHub:** private repos on GitHub Free can't use GitHub Environments, so every secret is a **repository secret**. Anything that differs by environment gets a `_DEV` or `_PROD` suffix, for example `SUPABASE_SERVICE_ROLE_KEY_DEV` and `SUPABASE_SERVICE_ROLE_KEY_PROD`. Deploy workflows map the suffixed secret to the unsuffixed variable name the code reads.

| Variable (as the code reads it) | Secret? | local `.env` | GitHub repo secret(s) | Supabase Edge Functions | EAS (app build) |
|---|---|---|---|---|---|
| `SUPABASE_URL` | no | `http://127.0.0.1:54321` (Phases 0–1), dev URL (Phase 2+) | `SUPABASE_URL_DEV` / `_PROD` (from Phase 2) | built in | as `EXPO_PUBLIC_SUPABASE_URL` |
| `SUPABASE_ANON_KEY` | public, protected by RLS | from `supabase status` | `SUPABASE_ANON_KEY_DEV` / `_PROD` | built in | as `EXPO_PUBLIC_SUPABASE_ANON_KEY` |
| `SUPABASE_SERVICE_ROLE_KEY` | **yes** | from `supabase status` (Phase 2+: dev key only) | `SUPABASE_SERVICE_ROLE_KEY_DEV` / `_PROD` | built in | **never** |
| `SUPABASE_DB_URL` | **yes** once hosted (contains the DB password) | from `supabase status` (Phase 2+: dev only) | `SUPABASE_DB_URL_DEV` / `_PROD` | n/a | **never** |
| `SUPABASE_PROJECT_REF` | no | not needed until Phase 2 | `SUPABASE_PROJECT_REF_DEV` / `_PROD` | n/a | n/a |
| `SUPABASE_ACCESS_TOKEN` | **yes** | not needed (use `supabase login` from Phase 2) | `SUPABASE_ACCESS_TOKEN` (one per account, from Phase 2) | n/a | **never** |
| `TMDB_API_KEY` | **yes** | `.env` | `TMDB_API_KEY` | `supabase secrets set` | **never** |
| `ANTHROPIC_API_KEY` | **yes** | `.env` (dev key) | `ANTHROPIC_API_KEY_DEV` / `_PROD` | `supabase secrets set` if used there | **never** |
| `EMBEDDING_API_KEY` | **yes** | `.env` | `EMBEDDING_API_KEY_DEV` / `_PROD` | `supabase secrets set` | **never** |
| `AVAILABILITY_API_KEY` | **yes** | `.env` (Phase 2+) | `AVAILABILITY_API_KEY_DEV` / `_PROD` | `supabase secrets set` | **never** |
| `EXPO_PUBLIC_POSTHOG_KEY`, `EXPO_PUBLIC_POSTHOG_HOST` | public (write-only ingestion key) | `.env` (Phase 3) | not needed in CI | n/a | EAS environment variables per profile |
| `EXPO_TOKEN` | **yes** | not needed (use `eas login`) | `EXPO_TOKEN` (Phase 3) | n/a | n/a |
| `JOB_HEARTBEAT_URL` | yes (anyone holding it can fake a heartbeat) | not needed | `JOB_HEARTBEAT_URL_DEV` / `_PROD` (Phase 2) | `supabase secrets set` | **never** |

Rules:
- Anything prefixed `EXPO_PUBLIC_` ships inside the app binary and web bundle. Only the Supabase URL, the anon key, and the PostHog ingestion key may carry that prefix. PostHog is used only by the app.
- Use separate dev and prod keys for every vendor that allows it (Supabase always does). For Anthropic, create two keys in the Console so either can be revoked on its own and spend can be split by key.
- **Only `deploy-prod*.yml` workflows may reference `*_PROD` secrets, and they must be `workflow_dispatch`-only.** The CI `secret-scan` job has a *prod-secret tripwire* step. It fails if any other workflow references a PROD secret (any `secrets.` name containing `PROD`), reads secrets in bulk (`secrets[...]`, `toJSON(secrets)`, `secrets: inherit`), or invokes a `deploy-prod` workflow (`gh workflow run … deploy-prod…`, `…/workflows/deploy-prod…`).
  - The **dispatch-only rule for `deploy-prod*.yml` is a convention for now.** No such workflow exists until Phase 3. A trigger check will be added with the first one, as an allowlist (only `workflow_dispatch` permitted) rather than a list of banned events, and it will be part of that phase's FMEA.
  - This is a tripwire against accidental misuse, not access control. It is a text match, so deliberate evasion is possible. On GitHub Free, any workflow on any branch can read all repo secrets, and anyone with write access can edit the tripwire itself, so **write access to the repo is the real boundary**. Recommendation: no collaborators with write access (Hari decides who gets write access).
- **Rotation:** rotate any key that shows up in a log, screenshot, chat, or commit. Revoke first, then rotate, then check the vendor's usage page for abuse. Detailed steps go in `docs/RUNBOOK.md` once there are live keys.
- **Detection:** GitHub secret scanning and push protection aren't available for private user-owned repos on any individual plan (section 3). What we use instead:
  1. The CI `secret-scan` job runs gitleaks over the full git history on every push to `main` and every PR into `main`.
  2. *Optional, recommended:* a local gitleaks pre-commit hook, which catches a secret before it ever lands in history:
     ```sh
     # one-time, per clone (needs gitleaks installed locally, e.g. `brew install gitleaks`)
     printf '#!/bin/sh\nexec gitleaks git --pre-commit --staged --redact --verbose\n' > .git/hooks/pre-commit
     chmod +x .git/hooks/pre-commit
     ```
  3. If something leaks, **rotate the key**. Rewriting history isn't enough, because clones and CI logs keep it.

---

## 3. CI (`.github/workflows/ci.yml`)

Runs on every push to `main`, every PR into `main`, and manual dispatch. It uses no secrets.

| Job | What it does | Expected duration |
|---|---|---|
| `pipeline` | Python 3.12, `pip install -e .[dev]`, `ruff check`, `pytest` in `pipeline/` | about 1 min |
| `app` | Passes as a no-op until `app/package.json` exists, then runs `npm ci`, `npm run lint --if-present`, `npm test --if-present` on Node 22 | under 10 s now |
| `secret-scan` | Installs a pinned, checksum-verified gitleaks 8.30.1, scans the full history, then runs the prod-secret tripwire | under 30 s |

**CI is advisory.** Branch protection and rulesets aren't available for private repos on GitHub Free, so a red CI run does **not** block a merge or a direct push. Before merging, whoever merges must check that every job on the PR (or on the commit) is green. QA's review includes confirming this. Permissions (DECISIONS 2026-09-26):
- **Merging to `main`:** Hari, or the coordinator for QA-passed work.
- **Prod deploys:** only Hari triggers them. Nothing enforces this on a private GitHub Free repo, so it is a rule, not a control (section 2).

**What GitHub Free leaves out for this repo, and what GitHub Pro would add.** Source: GitHub's docs, read from the `github/docs` source repo (`data/reusables/gated-features/*.md` and `data/reusables/billing/actions-included-quotas.md`, commit `18945a31`, 2026-09-25). docs.github.com itself is blocked from the agent sandbox.

| Feature (private, user-owned repo) | GitHub Free | GitHub Pro |
|---|---|---|
| Branch protection / rulesets | No | Yes |
| Environments + environment secrets | No | Yes |
| Environment required reviewers / wait timers | No | **No** (public repos only on Free, Pro, and Team) |
| Secret scanning / push protection | No | **No** (user-owned private repos need Enterprise Cloud with managed users) |
| Dependabot alerts | Yes | Yes |
| Actions minutes included | 2,000 / month | 3,000 / month |

Conventions:
- Actions are pinned to commit SHAs. Update them deliberately (Dependabot version updates for `github-actions` can be added later).
- Workflow permissions default to `contents: read`.
- Deploy workflows (migrations, Edge Functions, EAS builds) come later as separate files. Prod ones are `deploy-prod*.yml` and `workflow_dispatch`-only. They are not part of this skeleton.
- **Minutes budget:** GitHub Free includes 2,000 Actions minutes a month for private repos. Billing rounds each job up to a whole minute, so one CI run costs about 3 billed minutes, which allows roughly 650 runs a month. `sre` will report usage in the monthly cost report.

Local equivalent of the `pipeline` job:

```sh
cd pipeline
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && pytest
```

---

## 4. Hari's account-setup checklist

Items marked **[OPEN]** need a decision, and **$** means a recurring or one-time cost. The phase column shows the latest point each item can wait until.

| # | Action (Hari) | Needed by | Cost | Notes |
|---|---|---|---|---|
| 1 | Push this repo to GitHub as **Private** on GitHub Free | Phase 0 | Free | **Decided** (DECISIONS 2026-09-25) |
| 2 | Turn on **Dependabot alerts** (Settings, then Code security) | Phase 0 | Free | Secret scanning isn't available (section 3). The CI gitleaks job replaces it. *Optional:* install gitleaks locally and add the pre-commit hook (section 2) |
| 3 | Check CI before every merge; CI is **advisory** | Phase 0 onward | Free | **[OPEN] option:** GitHub Pro (about $4/month, **$**) adds branch protection and Environments (environment secrets) for private repos. It does **not** add required reviewers or secret scanning. Not assumed |
| 4 | Password manager vault for every account below; turn on 2FA everywhere | Phase 0 | | Recovery codes stored offline |
| 5 | Install **Docker Desktop** (free for small businesses under Docker's terms: fewer than 250 staff and under $10M revenue) or **Colima** (free), and the **Supabase CLI** | Phase 0/1 | Free | No account needed. This is the whole database for Phases 0–1 |
| 6 | Anthropic Console: create org, set a **monthly spend limit**, create key `laminary-dev` (`laminary-prod` at Phase 3) | Phase 1 | **$** usage-based | Pilot of 500 titles first, run locally (PLAN 5.4). Hari approves scale-up spend |
| 7 | TMDB account + API key; **confirm commercial terms** (PLAN 8) | Phase 1 | Free for non-commercial use | A commercial agreement may cost money **[OPEN]** |
| 8 | Embedding provider account **[OPEN]** (data-pipeline to propose) | Phase 1 | **$** usage-based | |
| 9 | Create Supabase org + project `laminary-dev` on the **Free** plan, US region (assumes US launch, PLAN 10.1 [OPEN]); enable `pgvector` | Phase 2 | Free | Storage estimate is 250–400 MB at 20K titles (DECISIONS 2026-09-26), which fits the free tier. Free projects pause after about a week of inactivity, which is acceptable for dev. Store the DB password in the password manager |
| 10 | Create a Supabase personal access token; add the repo secret `SUPABASE_ACCESS_TOKEN`, plus the `*_DEV` secrets from section 2 | Phase 2 | Free | Only once the dev deploy workflow exists |
| 11 | Availability vendor contract **[OPEN]** (PLAN 5.3, 10.3) | Phase 2 | **$** likely the largest recurring cost | Vendor comparison due before Phase 2 |
| 12 | Job-heartbeat / uptime monitor **[OPEN]**, e.g. a free-tier dead-man's-switch service | Phase 2 (before the first scheduled job) | Free tier | sre will propose options with a comparison. Needed to alert on failed or stale availability syncs |
| 13 | Create Supabase project `laminary-prod` on **Pro**, US region (assumes US launch, PLAN 10.1 [OPEN]); add the `*_PROD` repo secrets | Phase 3 (when TestFlight testers arrive) | **$** about $25/month | Pro plan and Phase 3 timing decided (DECISIONS 2026-09-26); region open. Confirm current pricing at signup. Pro gives no pausing and daily backups |
| 14 | PostHog Cloud project(s), US region (assumes US launch, PLAN 10.1 [OPEN]) | Phase 3 | Free tier | Used by the app only |
| 15 | Expo account + EAS; create `EXPO_TOKEN` | Phase 3 | Free tier; **$** if build minutes run out | |
| 16 | Apple Developer Program | Phase 3 (TestFlight) | **$** $99/year | |
| 17 | Google Play Console | Phase 4 | **$** $25 one-time | |

When an item is done, enter the values **directly in the vendor dashboard or GitHub settings, not in chat**. Agents only need to know that a secret exists and what it is called.

---

## 5. Not done yet (tracked for later phases)

- `docs/RUNBOOK.md`: rotation, restore, and job-failure procedures (once there is something to run).
- `docs/SLOS.md`: targets for rec latency and availability data age (before Phase 2 exit).
- Migrations check in CI (when `supabase/migrations/` has its first file).
- Backups: local data in Phases 0–1 isn't backed up by any service. The pilot's annotation output should be re-creatable from versioned inputs (PLAN 5.4), or exported (`supabase db dump`) after each pilot run. A tested restore procedure for prod comes with item 13.

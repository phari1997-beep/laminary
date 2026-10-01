# Progress

Current phase: **Phase 1 — Narrative data pilot** (started 2026-09-30)

(Agents: check off tasks from PLAN.md here with date and owner.)

## Phase 0
- [x] 2026-09-26 — Repo, environments, CI skeleton [sre] — QA: pass with nits after two fix rounds. See docs/ENVIRONMENTS.md.
  - Deferred to Phase 3 (sre, with the first deploy-prod workflow): allowlist trigger check; tripwire misses multi-line `gh workflow run \` and dispatch by display name — use pattern `deploy[-]prod[A-Za-z0-9_.-]*\.ya?ml`.
- [x] 2026-09-29 — Taxonomy + JSON schema [data-pipeline] — v1.0.0 approved by Hari (QA: pass). See docs/NARRATIVE_SCHEMA.md.
  - Phase 1 requirements carried forward [data-pipeline]: prompt builder uses the fail-closed Wikipedia gate, checks sha256(text sent) == content_sha256 and the 150-word minimum before each call; first step is one live structured-output call (needs Hari's spend approval).
  - Open [Hari]: TMDB written authorization for LLM use and commercial licence.
- [x] 2026-09-30 — 16 user interviews (Hari) — findings in docs/research/PHASE0_INTERVIEWS.md
- [ ] Landing page signups (Hari) — interview questions and landing copy in Google Drive (Laminary folder)
  - Landing page: designed in Figma (direction approved 2026-09-29), built in Lovable ("Story Shape Site", signups in Lovable Cloud DB) by frontend; QA passed 2026-09-29. Published 2026-09-29 at https://laminary.lovable.app.
  - Deferred until the first email is sent: double opt-in; optional signup throttle. Other polish items parked (see QA notes).

## Phase 1
- [ ] Ingestion (Wikidata candidates, Wikipedia plots) + gold-set tooling [data-pipeline] — in progress
- [ ] Annotation pipeline + evaluation script (mocked; no live calls) [data-pipeline] — in progress
- [ ] Live smoke call: one structured-output request (needs Hari: API key + spend approval)
- [ ] Gold set of ~100 titles labeled (Hari + friends)
- [ ] 500-title pilot annotation (needs Hari's spend approval) → agreement vs gold, cost per title
- [ ] Prompt iteration until ≥80% primary-plot agreement
- Blocked on environment: en.wikipedia.org, www.wikidata.org, query.wikidata.org not allowed by network policy; no ANTHROPIC_API_KEY yet.

## Handoff: moving from the cloud session to a local Claude Code session (2026-09-30)
- **Unreviewed Phase 1 code is on branch `claude/laminary-repo-setup-zyy6go`, not on main yet.** It covers ingestion, gold tooling, annotation and evaluation, and a QA review was in progress in the cloud session. If that review didn't finish and merge, the local session should:
  1. `git fetch origin && git checkout claude/laminary-repo-setup-zyy6go`, and compare it with main (`git diff main --stat`). It holds main plus the Phase 1 code.
  2. Have `qa` review the Phase 1 code (ingest/, gold/, annotate/, evaluate/, prompts/, GOLD_LABELING_GUIDE.md, the .gitignore block). Then merge to main.
- **Pending small change (sre):** add `anthropic>=1.9,<2` to pipeline/pyproject.toml so the 2 skipped client tests run.
- **Local setup (Hari):**
  - Python 3.12, then `cd pipeline && pip install -e ".[dev]" && pip install "anthropic>=1.9,<2"`.
  - Put `ANTHROPIC_API_KEY=...` in `.env` at the repo root (gitignored; check with `git check-ignore .env`). Do NOT export it globally: Claude Code itself would pick it up and bill its own usage to that key. Load it only for pipeline commands: `set -a; source .env; set +a` in the same command. Never print it.
  - Wikipedia and Wikidata need no setup locally.
- **Next Phase 1 steps, in order:**
  1. Live ingestion smoke run: `python -m laminary_pipeline.ingest candidates --limit 20`, then `plots --limit 20`. Free: check class ids, query time and the 150-word pass rate.
  2. Full candidates and plots; review the report.
  3. `python -m laminary_pipeline.gold select` and `template`, upload the sheets to Drive (Laminary folder), then Hari labels about 100 titles.
  4. Hari reviews pipeline/data/config/similarity_pairs.csv (42 proposed pairs).
  5. The ONE live structured-output smoke call on Opus 5.5 (about $0.09–0.17). **Needs Hari's explicit spend approval first.**
  6. Gold run (100 titles), then the 500-title pilot. Each needs spend approval, and `--budget-usd` is required.
- **Open questions from the Phase 1 agents for Hari:**
  - Titles with no TMDB id in Wikidata are excluded; alternatively, relax the schema to key on the Wikidata QID.
  - Pilot mix: 350 films and 150 series, with regional quotas.
  - Per-field confidence in the gold sheet?
  - About 15 titles double-labeled?
  - Confirm the CC BY-SA 3.0 → 4.0 date (2023-06-29).
  - Add a cache-write token field in schema 1.1.0.
- **Scratch files are not in the repo:** the landing SVGs are in Drive (Laminary → Landing page arc SVGs); the privacy text is live on the site.

# Progress

Current phase: **Phase 1 — Narrative data pilot** (started 2026-09-30)

(Agents: check off tasks from PLAN.md here with date and owner.)

## Phase 0
- [x] 2026-09-30 — Laptop bootstrap `scripts/bootstrap.sh` + Brewfile, docs/ENVIRONMENTS.md section 6 [sre] — QA passed (3 rounds). Hari ran it end to end on his Mac (Docker Desktop chosen; all tools PASS, daemon running). Fresh-machine tap-trust branch untested. See DECISIONS 2026-09-30.
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
- [x] 2026-10-01 — Ingestion (Wikidata candidates, Wikipedia plots) + gold-set tooling [data-pipeline] — QA: pass after three rounds; merged to main (345d2f5).
- [x] 2026-10-01 — Annotation pipeline + evaluation script (mocked; no live calls) [data-pipeline] — QA: pass after three rounds; merged to main (345d2f5). Budget enforced per request/round against a worst case, run lock, Wikipedia-only request gate re-verified.
  - Follow-ups from QA round 3 (run.lock bypass, 1.05 margin, namespace/interwiki gate): fixed in 17c5eaa, QA passed 2026-10-01.
  - [x] 2026-10-01 — Hari's decisions implemented [data-pipeline], QA passed, merged to main (cc7ff71): 0.95 display threshold; two-part exit gate + double labeling; prompt annotate-1.1.0 with Wikidata release year; schema 1.1.0 (series_status unknown, article-only refs); gold texts + manifest for Drive; season articles for thin series (3,000-word cap); genre filter (selector 1.1.0).
  - [x] 2026-10-01 — First-available-season exception (6,000-word ceiling), gate caps on sources/words, slot-2 can't become the reference, nits [data-pipeline]; pre-commit hook (ruff + pytest, enabled by bootstrap) [sre] — QA passed, merged to main (68cf2a4).
  - [x] 2026-10-01 — Hook follow-ups (PYTEST_ADDOPTS, git-diff failure, docs) [sre]; NARRATIVE_SCHEMA numbering + gate wording [data-pipeline]; lead-block season rule (stub seasons under 150 words kept and joined with the first full season, up to 6,000 words; gate allows season input up to 6,000) [data-pipeline] — QA passed, merged to main.
  - [x] 2026-10-01 — Lead-block stub threshold 500 words (fetcher 1.2.0); gate bounds via-season-articles input (6,000; lone list page 3,000); boundary tests [data-pipeline]; bootstrap usage + hook comment [sre] — QA passed, merged to main.
  - [x] 2026-10-01 — Lead-block fallback: stubs + first full season over 6,000 words → stubs alone under the 3,000 cap; lone full season over 6,000 still skipped (fetcher 1.3.0); gold guide 1.3.0 explains stubs-only files [data-pipeline] — QA passed, merged to main.
  - Nits (non-blocking): guide rule 1 could say "or skip under rule 4"; next ingest should note `--refresh` is needed to revisit old season_too_long records (none exist today). Optional future question: should stubs-only too_short results try the episode-list page?
  - Next: ingestion re-run (candidates, plots with season fallback, backfill, report) — on hold until Hari says go.
  - [x] 2026-10-01 — `anthropic` declared as the `annotate` extra; CI installs it [sre] (cf59f64, QA passed, merged).
- [x] 2026-10-01 — Live ingestion smoke run (free) [coordinator]: `candidates --limit 20` took 4m24s (WDQS); pool 1,735; 20 picked (15 films, 5 series, 13 buckets), 19 are gold seeds; all have TMDB and IMDb ids. Missing seeds: True Detective, Paatal Lok, Your Lie in April, Mushishi. `plots --limit 20`: 20/20 fetched, 20/20 pass the 150-word rule. Nit: two languages print as raw QIDs (Q188, Q7976) in the report.
- [x] 2026-10-01 — Full ingestion run (free) [coordinator]: 654 candidates (500 pilot + reserves); 500 plots fetched, 445 pass the 150-word rule (89%). Films 344/350 (98%), series 101/150 (67%; tv:english 62/95, tv:korean 9/15). 49 too short, 6 with no plot section. `--backfill` not yet run. Non-narrative titles slipped the genre filter (e.g. MythBusters, Zoboomafoo).
- [ ] Live smoke call: one structured-output request (needs Hari: API key + spend approval)
- [ ] Gold set of ~100 titles labeled (Hari + friends)
- [ ] 500-title pilot annotation (needs Hari's spend approval) → agreement vs gold, cost per title
- [ ] Prompt iteration until the two-part gate passes: ≥85% primary-plot agreement (or within 5 points of human agreement) and ≥95% accuracy on labels shown at ≥0.95 confidence (DECISIONS 2026-09-30)

## Handoff: moving from the cloud session to a local Claude Code session (2026-09-30)
- **Unreviewed Phase 1 code is on branch `claude/laminary-repo-setup-zyy6go`, not on main yet.** It covers ingestion, gold tooling, annotation and evaluation, and a QA review was in progress in the cloud session. If that review didn't finish and merge, the local session should:
  1. `git fetch origin && git checkout claude/laminary-repo-setup-zyy6go`, and compare it with main (`git diff main --stat`). It holds main plus the Phase 1 code.
  2. Have `qa` review the Phase 1 code (ingest/, gold/, annotate/, evaluate/, prompts/, GOLD_LABELING_GUIDE.md, the .gitignore block). Then merge to main.
- ~~Pending small change (sre): add `anthropic` to pyproject.toml~~ Done 2026-10-01 (cf59f64, QA passed, merged): optional extra `annotate = ["anthropic>=1.9,<2"]`; CI installs `.[dev,annotate]` and fails if anthropic is missing.
- **Local setup (Hari):**
  - Python 3.12, then `cd pipeline && pip install -e ".[dev,annotate]"` (or `scripts/bootstrap.sh`, which uses uv).
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
  - ~~About 15 titles double-labeled?~~ Decided 2026-09-30: 20–25 titles double-labeled.
  - Confirm the CC BY-SA 3.0 → 4.0 date (2023-06-29).
  - Add a cache-write token field in schema 1.1.0.
- **Scratch files are not in the repo:** the landing SVGs are in Drive (Laminary → Landing page arc SVGs); the privacy text is live on the site.

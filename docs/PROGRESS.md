# Progress

Current phase: **Phase 0 — Validate**

(Agents: check off tasks from PLAN.md here with date and owner.)

## Phase 0
- [x] 2026-09-26 — Repo, environments, CI skeleton [sre] — QA: pass with nits after two fix rounds. See docs/ENVIRONMENTS.md.
  - Deferred to Phase 3 (sre, with the first deploy-prod workflow): allowlist trigger check; tripwire misses multi-line `gh workflow run \` and dispatch by display name — use pattern `deploy[-]prod[A-Za-z0-9_.-]*\.ya?ml`.
- [ ] Taxonomy + JSON schema draft [data-pipeline] — v0.3.0 on main 2026-09-26 (QA: pass with nits). Waiting on Hari's answers to open Q1, Q3, Q4 (Q2 TMDB is his to confirm), then bump to 1.0.0.
  - With the 1.0.0 bump [data-pipeline]: tie the gate's ref pattern and kind to the schema in a test (QA N1); decide whether to drop the single-leg `reduced_shape` clause or measure it in the pilot (QA N2).
  - Phase 1 acceptance for the prompt builder [data-pipeline]: sha256 of the exact text sent must equal the gated source's content_sha256; check the 150-word minimum before the call (QA N3). First Phase 1 step is one live structured-output call (needs Hari's spend approval).
- [ ] 15 user interviews; landing page signups (Hari) — form questions and landing copy drafted

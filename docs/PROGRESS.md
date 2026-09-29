# Progress

Current phase: **Phase 0 — Validate**

(Agents: check off tasks from PLAN.md here with date and owner.)

## Phase 0
- [x] 2026-09-26 — Repo, environments, CI skeleton [sre] — QA: pass with nits after two fix rounds. See docs/ENVIRONMENTS.md.
  - Deferred to Phase 3 (sre, with the first deploy-prod workflow): allowlist trigger check; tripwire misses multi-line `gh workflow run \` and dispatch by display name — use pattern `deploy[-]prod[A-Za-z0-9_.-]*\.ya?ml`.
- [x] 2026-09-29 — Taxonomy + JSON schema [data-pipeline] — v1.0.0 approved by Hari (QA: pass). See docs/NARRATIVE_SCHEMA.md.
  - Phase 1 requirements carried forward [data-pipeline]: prompt builder uses the fail-closed Wikipedia gate, checks sha256(text sent) == content_sha256 and the 150-word minimum before each call; first step is one live structured-output call (needs Hari's spend approval).
  - Open [Hari]: TMDB written authorization for LLM use and commercial licence.
- [ ] 15 user interviews; landing page signups (Hari) — interview questions and landing copy in Google Drive (Laminary folder)
  - Landing page: designed in Figma (direction approved 2026-09-29), built in Lovable ("Story Shape Site", signups in Lovable Cloud DB) by frontend; QA passed 2026-09-29 (ready to publish). Not yet published — waiting on Hari's go.
  - Deferred until the first email is sent: double opt-in; optional signup throttle. Other polish items parked (see QA notes).

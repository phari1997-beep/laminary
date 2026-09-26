# Decisions log

| Date | Decision | Reason |
|---|---|---|
| 2026-09-25 | Solo build; Hari is PM; Claude Code with 5 subagents (data-pipeline, backend, frontend, sre, qa) | Keep human control of direction, delegate execution |
| 2026-09-25 | Frontend owns all design; no separate designer | One owner for look and build |
| 2026-09-25 | No generic AI aesthetics (see DESIGN.md) | Brand credibility with film-literate users |
| 2026-09-25 | Marketing kept separate, parked for later | Focus on build first |
| 2026-09-25 | GitHub repo stays private | Hari's call; keeps the work private before launch |
| 2026-09-26 | Local-first data: Supabase CLI (Docker) locally for Phases 0–1; hosted free-tier dev project at Phase 2; hosted prod (Pro) only when TestFlight testers arrive | Storage estimate ~250–400 MB at 20K titles fits free tier; defers ~$25/mo until real users need uptime and backups |
| 2026-09-26 | Coordinator may merge QA-passed work to `main` | Hari's call; keeps flow moving, QA gate still applies |
| 2026-09-26 | Only Hari triggers prod deploys | Prod touches real users; no enforcement available on a private GitHub Free repo |
| 2026-09-26 | Story-shape labels (arc, plot, blueprint) and the arc line are shown by default; plot spoilers stay hidden | Shapes hint at an ending without revealing events; hiding them would hide most browse rows |
| 2026-09-26 | Mythic blueprint uses Vogler's 12 stages | Simpler to label than Campbell's 17 |
| 2026-09-26 | Keep Booker's Rebellion Against "The One" and Mystery in the v1 plot taxonomy (9 plots) | Hari's call; overrides the recommendation to drop them |
| 2026-09-26 | Mood filter names proposed by frontend in Phase 3; the schema's tones stay internal until then | Mood naming is a design/interaction decision |
| 2026-09-26 | Title pages attribute Wikipedia where analysis is derived from its plot summaries | CC BY-SA; honest sourcing |
| 2026-09-26 | Annotate only titles with a plot summary of at least 150 words | Accuracy over catalog size |
| 2026-09-26 | Add 10–20 more beat tags only after the gold set shows which are reliable | Avoid unreliable tags |
| 2026-09-26 | v1 covers movies and TV at series level only, never episode by episode; ongoing series are annotated on episodes aired so far and refreshed when the summary changes | Answers PLAN 10.2; keeps scope small |
| 2026-09-26 | Gold-set labelers work from the same summary the model sees | Measures the model, not the summary |
| 2026-09-26 | Keep all 15 proposed browse rows for now; prune after the 500-title pilot shows row sizes | Not enough data to cut yet |
| 2026-09-26 | Per-term spoiler levels (none / mild / major) approved as drafted | Hari reviewed; consistent with showing story shapes by default |
| 2026-09-26 | Label display threshold raised from 0.70 to 0.80 confidence (tunable after the 500-title pilot) | Prefer fewer, more reliable labels; pilot will show the coverage cost |

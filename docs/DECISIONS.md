# Decisions log

| Date | Decision | Reason |
|---|---|---|
| 2026-09-25 | Solo build; Hari is PM; Claude Code with 5 subagents (data-pipeline, backend, frontend, sre, qa) | Keep human control of direction, delegate execution |
| 2026-09-25 | Frontend owns all design; no separate designer | One owner for look and build |
| 2026-09-25 | No generic AI aesthetics (see DESIGN.md) | Brand credibility with film-literate users |
| 2026-09-25 | Marketing kept separate, parked for later | Focus on build first |
| 2026-09-25 | GitHub repo stays private | Hari's call; keeps the work private before launch |
| 2026-09-26 | Local-first data: Supabase CLI (Docker) locally for Phases 0–1; hosted free-tier dev project at Phase 2; hosted prod (Pro) only when TestFlight testers arrive | Storage estimate ~250–400 MB at 20K titles fits free tier; defers ~$25/mo until real users need uptime and backups |

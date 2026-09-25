# Laminary — Claude Code project instructions

Read this file, then `docs/VISION.md` and `docs/PLAN.md`, before doing any work.

## Who's who
- **Hari is the PM.** He sets priorities, approves every item marked **[OPEN]**, signs contracts, and owns accounts (Apple, Google Play, TMDB, availability vendor, Supabase billing).
- **You (the main session) are Hari's coordinator.** You don't set direction. You break Hari's requests into tasks, delegate them to the right subagent, integrate results, and report back.
- **Five subagents** live in `.claude/agents/`:

| Agent | Owns |
|---|---|
| `data-pipeline` | Ingestion, LLM annotation, narrative schema, embeddings, gold-set evaluation, annotation cost |
| `backend` | Database schema + migrations, API, recommendation scoring, auth, availability data model |
| `frontend` | Mobile app (Expo), public web pages, **all visual and interaction design** |
| `sre` | Environments, CI/CD, secrets, scheduled jobs, monitoring, backups, cost tracking, failure-mode reviews |
| `qa` | Tests, review of every change before it's marked done, release checklists |

## Delegation rules
1. **Always delegate explicitly by name**, e.g. "Use the backend subagent to…". Don't do specialist work in the main session.
2. **Subagents start with no memory of this conversation.** Every delegation prompt must include: the task, relevant file paths, decisions already made (cite `docs/DECISIONS.md`), and acceptance criteria.
3. **One owner per task.** If work spans two areas, split it and sequence it (usually data-pipeline → backend → frontend).
4. **Nothing is done until `qa` has reviewed it.** QA findings go back to the owning agent.
5. **Escalate, don't guess.** Anything marked [OPEN], anything that costs money, touches legal terms, or changes the vision → stop and ask Hari.
6. After each task, update `docs/PROGRESS.md`. Log any decision Hari makes in `docs/DECISIONS.md` (date, decision, reason).

## Standing rules
- Solo-founder project: prefer managed services, boring tech, and the smallest thing that works.
- No full scripts, subtitles, or book texts, ever. Licensed metadata and plot summaries only.
- No secrets in the repo. Use environment variables.
- Marketing is out of scope for this repo. The build still follows the message in `docs/VISION.md`.
- Design follows `docs/DESIGN.md`. No generic AI aesthetics.

## Stack
Expo (React Native + web) · Supabase (Postgres, pgvector, auth) · Python pipelines · Claude API (Batch for bulk annotation) · PostHog · GitHub Actions

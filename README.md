# Laminary

Find stories shaped like the ones you love, on the services you already pay for.

Laminary is a watch app that recommends movies and series by story structure, not genre. Background: `docs/VISION.md`, `docs/PLAN.md`, and `docs/DESIGN.md`.

## Repo layout

| Path | Owner | What's there |
|---|---|---|
| `app/` | frontend | Expo app (iOS, Android, web). Not scaffolded yet |
| `supabase/` | backend | Postgres migrations, Edge Functions, scheduled jobs. Not initialized yet |
| `pipeline/` | data-pipeline | Python: ingestion, LLM annotation, embeddings, evaluation |
| `docs/` | coordinator + owners | Vision, plan, design, decisions, progress, environments |
| `.github/workflows/` | sre | CI (`ci.yml`) |
| `.claude/agents/` | coordinator | Subagent definitions |
| `.env.example` | sre | Every env var the project uses, with empty values |

How the project is run: `CLAUDE.md`. Environments and secrets: `docs/ENVIRONMENTS.md`.

## Local dev quickstart (pipeline)

Needs Python 3.12.

```sh
cp .env.example .env          # fill in dev values only; .env is gitignored
cd pipeline
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest
```

CI runs the same checks, plus a gitleaks secret scan, on every push and PR to `main`.

## Rules

- No secrets in the repo. Use environment variables (see `.env.example`).
- No full scripts, subtitles, or book texts, ever. Only licensed metadata and plot summaries.

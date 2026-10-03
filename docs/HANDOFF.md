# Handoff: Phase 1 coordinator session (2026-10-03)

Read `CLAUDE.md`, then this file, then `docs/PROGRESS.md` (Phase 1) and the 2026-10-02/03 rows at the end of `docs/DECISIONS.md`. Every decision Hari made in this session is logged there with its reason.

## Where things stand
- **`main` = `94af88b` on GitHub.** Everything on main is QA-passed. 945 tests pass, and the pre-commit hook runs ruff and pytest.
- **No paid Anthropic calls have been made, ever.** Annotation has not started. Every paid run needs Hari's explicit spend approval and a `--budget-usd`.
- **Pilot:** 498/500 slots hold annotatable titles. The 10 priority series are all in (`ingest/priority.py`). Fetcher 1.5.9, prompt `annotate-1.3.0`, schema 1.3.0, guide 1.6.0.
- **Gold set:** 100 titles (70 films, 30 series), 25 double-labeled (Game of Thrones, Seinfeld and The Good Place are forced in). Not yet uploaded to Drive.
- **Similarity pairs:** 39 of 42 scorable. N13 (Triangle, British, Q1783930) and M16 (The Housemaid 1960, Q49729) are pinned. Code Geass is excluded (wrong enwiki article). M04, M19 and M20 wait on task 2 below.

## Data is NOT in git: copy it before switching machines
`pipeline/data/` (~56 MB) is gitignored. It holds the plot files, the HTTP cache, the pilot lists, the pairs set and the gold outputs. **Copy the whole folder to the other machine.** Don't re-ingest: a re-run fetches newer Wikipedia revisions, which changes the gold texts and hashes. On this Mac:

```
cd ~/src/laminary && tar czf ~/laminary-pipeline-data-2026-10-03.tgz pipeline/data
```

On the other machine, after cloning, run `scripts/bootstrap.sh` and then `tar xzf laminary-pipeline-data-2026-10-03.tgz` from the repo root. Keep `.env` (the API key) out of git, and load it only for pipeline commands: `set -a; source .env; set +a`.

## Unmerged work
1. **Branch `gold-xlsx`, pushed, NOT QA-passed.**
   - Commits:
     - 0b0ec8d: sre adds the `openpyxl` "gold" extra.
     - f5b3907 and 4f21ecd: data-pipeline's formatted `gold_labels.xlsx`, with real dropdowns, frozen header, locked grey prefilled columns, Read me and Lists tabs, an `.xlsx` importer, and `gold_selection.jsonl` moved to `data/gold_internal/`.
   - 953 tests pass on the branch.
   - The output `pipeline/data/gold/gold_labels.xlsx` already exists on this Mac.
   - QA's review was cut off by the usage limit. It had confirmed that the selection, texts and lists are byte-identical. The rest still needs checking.
   - **To do:**
     - QA review it.
     - Rebase on main (possible conflict in `pipeline/data/README.md`).
     - Merge.
     - Install the extra: `uv pip install -e ".[dev,annotate,gold]"`.
   - **Coordinator suggestions not yet sent to data-pipeline:**
     - Human-readable column headers, with the code names kept in a hidden row so the importer still works.
     - Narrow or collapse the long source/link columns.
     - Make `gold import` write `gold_labels.jsonl` to `data/gold_internal/`, not into the upload folder.
2. **Episode tables A+B and the film/series check: NOT done.**
   - Decided 2026-10-03 (see DECISIONS).
   - A partial work-in-progress diff is saved at `docs/handoff/episodes-main-article-wip.patch`. It was cut off mid-rewrite and will probably not pass tests. Use it as a reference, or restart.
   - Brief:
     - **(A)** When the main article's prose and the season/list pages fail, read the episode tables in the main article's own top-level "Episodes" section.
     - **(B)** Treat episode tables with no "Season N" heading (a single table, or "Part I/II") as season 1, in table order. Skip specials tables (The Pacific).
     - Same caps, trivia, pilot-as-E0, coverage and gate rules. English Wikipedia only.
     - **Film/series check:** skip a TV item whose page's `wikibase-shortdesc` names a film (and the reverse for movies). Then drop Code Geass from the pairs `EXCLUDED` stopgap.
     - Bump to FETCHER 1.6.0.
     - **Expected rescues:**
       - (A): Ozark, Ted Lasso, Shrinking, Severance, Big Little Lies, Kingdom, Russian Doll, Fleabag.
       - (B): Death Note, Shōgun, WandaVision, Crash Landing on You, Reply 1988, Scam 1992, Snowdrop and others.
     - **QA leftovers from the pairs review, to include:**
       - Test the `pairs_owned` guard (`ingest/__main__.py:207`).
       - Make the pairs resumability test assert the files are unchanged.
       - Test the `not for_pairs` backfill guard.
       - Make `gold.pairs.resolve_pairs` use `pins()` and `active()`.
     - **After QA:** run a free `plots` re-fetch, then `plots --backfill`, then `report`. Rescued pilot titles displace the lowest-ranked reserves. Gold must not change.
3. **Labeler guide `.docx`: NOT saved, redo it.**
   - frontend was building "Laminary gold labeling guide.docx" into `pipeline/data/gold/` from `docs/GOLD_LABELING_GUIDE.md`, the readme and lists CSVs. It was almost done when it was stopped, but nothing was written.
   - It should contain:
     - a welcome;
     - steps;
     - the golden rules;
     - a label reference table with plain-English meanings;
     - skip reasons and confidence;
     - a one-page quick card.
   - It uses the approved tokens (Screen #F2F3EF, Ink #17191E, Grain #555B63, Gate #7E848B, Dye #B0175C, Alert #A2380C) and Archivo Narrow + Atkinson Hyperlegible.
   - Use the `anthropic-skills:docx` skill.

## Hari's to-dos
- **Upload the gold set** once the `.xlsx` and `.docx` are QA-passed. Upload `gold_labels.xlsx`, the `.docx` and the `texts/` folder from `pipeline/data/gold/`. Never upload `gold_internal/` (it holds the guessed labels).
  - In Google Sheets, check that a dropdown survived, and protect the grey columns (Data → Protect sheets and ranges).
  - Then fill one row and export it, so `gold import` can be checked before the sheet goes to labelers.
- **Review** `pipeline/data/config/similarity_pairs.csv` (41 proposed, 1 seed).
- **Spend approvals, when ready:**
  - one smoke call, about $0.09–0.17;
  - the gold run, about $3–8 expected;
  - the 500-title pilot;
  - the 34 pairs titles, $1.25–3.19 expected and about $21 worst case.

## Working rules learned this session
- **Delegate as the coordinator.** QA reviews every change before it merges. Push only QA-passed work to main.
- **Don't let two agents commit in the same checkout at once.** Give agents `isolation: worktree`, or sequence them. A coordinator commit once got swept into an agent's commit.
- **Pre-commit hook.** Chain commit commands with `&&`. Never use `--no-verify`.
- **Permission classifier.** If the auto-mode classifier blocks a subagent's commit, the subagent stops and saves patches. Don't route around it; ask Hari. He has authorised the coordinator to commit QA-passed patches.
- **Worktrees** need `pipeline/.venv` symlinked to the main venv (untracked). Run tests with `.venv/bin/python -m pytest`; ad hoc scripts need `PYTHONPATH=.`.
- **Clean up merged worktrees and branches** (`git worktree remove --force`, `git branch -d`).

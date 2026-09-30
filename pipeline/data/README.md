# pipeline/data

Local-first pipeline data (DECISIONS 2026-09-26: JSON/JSONL files, no database for the pilot).
Everything here is gitignored except this README and `config/`.

| Path | Written by | What |
|---|---|---|
| `config/gold_seed_titles.csv` | hand-written | Well-known titles forced into the pilot so the gold set can be drawn from them. `guessed_plot`/`guessed_arc` only steer balance; they are never shown to labelers. |
| `config/similarity_pairs.csv` | hand-written | "Should match" / "should NOT match" pairs for similarity evaluation (interview risk 1). Hari reviews `status`. |
| `pilot_candidates.jsonl` | `ingest candidates` | One row per title: QID, title, year, type, TMDB/IMDb ids from Wikidata, bucket, role (`pilot` or `reserve`), region, language, decade, genre. |
| `plots/<QID>.json` | `ingest plots` | `status: ok` with `text` and a schema-shaped `source`, or `status: skipped` with `skip_reason`. |
| `cache/http/` | HTTP client | Cached API responses. Pinned-revision responses never expire; SPARQL pools expire after 30 days, latest-revision lookups after 7. Safe to delete. |
| `reports/` | all commands | `candidates_summary.json`, `plots_summary.json`, `gold_selection_summary.json`. |
| `gold/` | `gold select/template/import` | `gold_selection.jsonl`, the sheet CSVs, and imported `gold_labels.jsonl`. |

Skip reasons in `plots/<QID>.json`:

| Reason | Meaning |
|---|---|
| `too_short` | No plot-like section reaches 150 words (DECISIONS 2026-09-26). |
| `no_plot_section` | No Plot / Synopsis / Premise / (series) overview heading. |
| `no_enwiki_article` | No English Wikipedia article for the QID. |
| `missing_page` | The enwiki title doesn't resolve to a page. |
| `disambiguation_page` | The title is a disambiguation page. |
| `qid_mismatch` | The page's Wikidata item isn't the candidate's QID (a different work). |
| `fetch_error` | Network or API failure; retried on the next run. |

Use `--data-dir` or `LAMINARY_DATA_DIR` to put data elsewhere.

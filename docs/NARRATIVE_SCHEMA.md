# Narrative taxonomy and annotation schema

**Status: v1.2.0.** Owner: data-pipeline. Includes Hari's decisions of 2026-09-26, 2026-09-29, 2026-09-30, 2026-10-01 and 2026-10-02 (`docs/DECISIONS.md`). The one remaining **[OPEN]** item (TMDB authorization) is in section 16 and does not block 1.0.0, because the schema already enforces Wikipedia-only input.

- Stored record contract: `pipeline/laminary_pipeline/schema/annotation.schema.json` (JSON Schema draft 2020-12, shipped as package data)
- Model-facing output schema: derived in code, `laminary_pipeline/model_output.py` (section 11)
- Arc derivation rule: `laminary_pipeline/arc.py` (section 8)
- Validation (schema + semantic checks): `laminary_pipeline/annotation.py` (section 12)
- Illustrative examples (test data, not shipped): `pipeline/tests/examples/`
- Consistency tests: `pipeline/tests/test_schema.py` checks that every vocabulary table here matches the schema's enums and spoiler levels exactly, and that every backticked identifier in this document exists in the schema. Edit both together.

This document is the single source of definitions for the annotation prompt, the gold-set labeling guide, and the frontend's display rules. Definitions are written so that a model or a person reading only a plot summary can apply them the same way.

---

## 1. Ground rules

1. **Wikipedia plot sections only.** The only annotation input is Wikipedia plot sections (CC BY-SA). Never scripts, subtitles, or book texts. **Never send TMDB overviews or any other TMDB text to Claude, and never embed them**: search results quoting TMDB's terms say that use with LLM/AI query-response systems needs TMDB's written authorization, and that training or validating ML/AI systems on TMDB content is prohibited. The schema enforces this: LLM and gold records may only cite `wikipedia_plot` sources with a CC BY-SA `license` and an `https://en.wikipedia.org/wiki/` article `ref` (schema 1.1.0). **Phase 1 requirement:** the prompt builder and the embedding input must pass their sources through `require_wikipedia_sources()` in `laminary_pipeline/annotation.py`. It fails closed: one non-Wikipedia, non-CC-BY-SA or non-en.wikipedia.org source rejects the whole title, and an empty list is rejected too. It is unit-tested, and a test ties its ref pattern and allowed kind to the schema's rule for LLM and gold records. **Phase 1 requirement for the prompt builder:** the SHA-256 of the exact text sent to the model must equal the gated source's `content_sha256`, and the 150-word minimum is checked on that text before the call, not after. TMDB is used only for IDs and display metadata, subject to Hari's commercial agreement (PLAN §8). The summary text is not stored in the record; the record stores a reference, revision and hash of each source (section 10). **One non-Wikipedia field (DECISIONS 2026-09-30, prompt `annotate-1.1.0` on):** the release year from Wikidata (CC0) goes in the request header, because the `historical_past` / `contemporary` definitions are relative to it. It is sent only as a validated integer from 1880 to the current year + 2 (any other value refuses the title), `verify_request()` checks the header exactly, and a title with no year is not annotated, because every record requires `release_year` (ingest already excludes candidates without one). The prompt still says how to judge the period without a year line, which the builder omits if it ever gets a title without one. The title's display name is still never sent.
2. **Minimum summary: 150 words** (decided 2026-09-26). Titles with less summary text are skipped before any model call, and get no narrative data. The schema enforces this on LLM and gold records. Since TMDB text is excluded, this in practice requires a Wikipedia plot section of at least 150 words.
3. **No guessing from a title.** Annotate only what the supplied summary supports. Do not fill gaps from outside knowledge of the title. If the summary can't support the four layers, the correct output is `outcome: "abstained"`, not a low-confidence guess.
4. **Own words.** Every free-text field is written fresh. Never copy or closely paraphrase sentences from the source.
5. **Attribution (decided 2026-09-26).** Title pages attribute Wikipedia wherever displayed analysis is derived from its plot summaries. Display requirement for frontend: show attribution when any entry in `provenance.sources` has `kind` of `wikipedia_plot`, and credit every such article (a series may have several, below).
6. **Series from season articles (decided 2026-10-01).** When a series' main English Wikipedia article fails the 150-word rule or has no plot section, its per-season English Wikipedia articles may be used instead. Season pages are found from the main article's links and from title patterns ("<X> season N", "<X> (season N)", "<X> series N", "List of <X> episodes"), must be main-namespace enwiki pages, and must be verified on Wikidata: the page's item states P179 (part of the series) or P361 (part of) with the series' QID. Unverifiable pages are skipped. From each season page, ingest takes the plot, summary or synopsis sections and a prose season overview, with episode tables dropped, and joins seasons in season order up to about **3,000 words**, stopping at a season boundary (a season that would cross the cap is left out, never cut). Lead block (decided 2026-10-01, replacing the same day's "first-season exception" and "stub season 1 doesn't count" rules; stub threshold raised from 150 to 500 words the same day): a *stub* season is one whose own text is under **500 words** (`STUB_SEASON_WORDS` in `laminary_pipeline/ingest/seasons.py`), judged per season. The first *full* season (500 words or more: season 1, or the first later season when earlier ones are missing, unverified, have no text or are stubs) is used in full together with any stub seasons before it, in order; this lead block may run past the cap up to a hard ceiling of **6,000 words** (for example 300 + 3,500 words, or 300 + 400 + 3,000 words, are both used whole). Over the ceiling (fallback, decided 2026-10-01): when stub seasons lead the block, the full season and every later page are left out and the stubs are used alone, joined under the ~3,000-word cap as stubs only (for example 450 + 5,551 = 6,001 words → season 1 alone, 450 words; 100 + 5,901 → the 100-word stub alone, which fails the 150-word minimum, so too_short). A lone full season over the ceiling, with no stubs before it, skips the title, with no fallback to the episode-list page (ingest skip reason season_too_long). If the lead block is over the cap nothing more is added; otherwise later seasons join under the cap as above. With no full season at all (or when the 20-source limit is reached before one), the stubs join under the cap as usual. Ingest records the threshold in each season record's `season_articles.stub_season_words` (fetcher version 1.2.0; 1.3.0 adds the stubs-alone fallback). The worst-case cost bound prices each request from its actual size, so it covers this; at the ceiling it is about $0.23 per batch attempt (one or several sources) against $0.21 at 3,000 words, because the output cap dominates. The 150-word minimum is a separate rule and still applies to the joined text. Each article is its own source with its own `ref`, `revision`, `content_sha256` and `season`; the request carries one summary block per source, opened by a marker naming the article, which is the season marker. The gate and `verify_request()` check every block, and a change to any source's hash triggers re-annotation (section 10). Before any paid call, the input gate (`check_source_limits()` in `laminary_pipeline/annotate/inputs.py`) also refuses inputs these rules can't produce: more sources than a record may hold (the schema's `provenance.sources.maxItems`, 20); any season-article input (`via: "season_articles"`, several sources, or one with a `season`) over the 6,000-word ceiling; and a lone episode-list page from season articles (no `season`) over the ~3,000-word cap, since ingest only joins one under the cap. The ~3,000-word cap is a joining rule, not an input limit, since a lead block may be several sources up to the ceiling. A single main article has no upper word limit.
7. **Describe, don't grade.** Text explains the story's shape. No quality judgments.
8. **Scope (decided 2026-09-26).** Movies, and TV at series level only: one record per whole series, never per episode or season. Ongoing series are annotated on the episodes aired so far and refreshed when the summary changes (section 10).
9. **Partial season coverage (decided 2026-10-02, prompt `annotate-1.2.0` on).** When a series summary is built from season articles and covers only some seasons (seasons left out over the cap, or missing, unverified or without plot text), the request header states it, for example "Summary covers seasons 1–4 of 7." or, for a run that doesn't start at season 1, "Summary covers seasons 3–6 of 7." The line is built from validated integers only: the season numbers are the sources' `season` fields, and the total is Wikidata's "number of seasons" (P2437, CC0, fetched with the candidate details) when it is a whole number at least as large as every verified season article, otherwise the highest verified season article found. Fetcher 1.4.0 always records the total; with a prompt that sends this line, a season-article plot file without a total, or any plot file from before fetcher 1.4.0, is refused before any call (it would get a wrong line, and it predates the non-plot filter). A summary that covers every season, a main article and an episode-list page get no line. `verify_request()` rebuilds the line and checks the header exactly. The prompt tells the model that the missing seasons may be at the start, middle or end, to annotate only the covered seasons, and never to infer events, including the ending, from seasons it can't see. The record stores the same facts in `provenance.coverage` (section 10), and gold labelers see the line in the sheet's "summary_coverage" column (the summary text files stay byte-identical to the model's summary blocks).
10. **Non-plot text in series sections (decided 2026-10-02).** A series article's plot heading may hold production, ratings, broadcast or reception text. Ingest keeps only plot-like parts: subsections whose headings name non-plot content are dropped (a plot heading such as "Season 5: Revival" always counts as plot; "mythology" is not on the list, since it can be a series' main storyline); under an "Episodes", "Seasons" or "Series overview" heading only season, part, arc, plot and specials subsections are kept, the text directly under "Episodes"/"Seasons" is dropped when a quarter or more of it reads as production writing, and a season part's first paragraph is dropped when it reads as production writing (renewal and premiere dates); parts that are mostly production writing (by a list of cue words) are dropped; and such a broad section that keeps under half its words doesn't count as a plot section at all. A section whose heading markup isn't recognised is not used. The 150-word rule and the season-article fallback then apply as usual. What was dropped, and why, is recorded in the plot file. MediaWiki error messages ("Cite error: ...") are stripped by their HTML class. Film plot sections are used whole.
11. **Episode-table summaries for big series (decided 2026-10-02, fetcher 1.5.0).** A last fallback, only for a series whose main article fails and whose season-article prose join (item 6) yields no usable text (under the 150-word minimum, or nothing), or is season_too_long (a season article over the 6,000-word ceiling; decided 2026-10-02, the title stays season_too_long when the episode tables don't reach 150 words): the per-episode summaries in the episode tables (MediaWiki wikiepisodetable) of the same verified pages are used instead, from its verified season pages, or, when no season page has any summary, its verified "List of <X> episodes" page or pages: the plain one, and pages split by a year range, season range or part ("List of <X> episodes: 1998–2009", "(1998–2009)", "(seasons 1–5)", "(part 1)"), each verified the same way and read in order of their years, seasons or parts, with the seasons used keeping a rising order across pages (an earlier page whose seasons have no summaries doesn't block a later one; its headings still count for the season total). When a later page's numbering restarts below seasons an earlier page already named (Doctor Who's classic "Season" and revival "Series" runs), those seasons are not used and the restart is recorded, because which run the "of N" total would count is unclear. **Per-title run rules (decided 2026-10-02, fetcher 1.5.1):** a short hand-kept table keyed by QID (`SERIES_RUN_RULES` in `laminary_pipeline/ingest/runs.py`) can name the one run a series uses for both the season-article prose (item 6) and these episode tables; its only entry, doctor_who_revival, uses Doctor Who's 2005 revival alone ("<X> series N" pages, episode-list pages split by a year range from 2005, "Series N" headings), ignores every classic "Season N" page and heading and the 1963–1989 list page before verification, and takes the "of N" total from the run's verified pages and headings only, never Wikidata P2437; verification is unchanged, a relaunch that restarts the numbering again is out of scope (skipped by the rising-season rule), and the plot file records the rule (series_run_rule) and the pages it ignored (series_run_ignored). Verification (P179/P361, ordinals), evidence recording and the English Wikipedia CC BY-SA rule are unchanged; no other source is used. **Text:** only each episode's summary cell (`td.description`), through the same plain-text cleaning (references, tables and "Cite error" messages dropped), with a compact marker of season, episode and title from the title cell, one paragraph per episode, for example `S2E5 "The Robbery": <summary>`; air dates, ratings, writers, directors, production codes and every other cell are dropped, and nothing outside the tables is used. Seasons come in season order and episodes in table order; on an episode-list page a table's season is the nearest enclosing "Season N" / "Series N" heading at any level (a "Part 1" subheading inside "Season 5" stays season 5), tables under other headings (specials) are skipped, and a season number that doesn't rise (a revival restarting at "Series 1") stops the page; on a season page tables under a specials heading are skipped. A season's paragraphs are one source with that `season`, so an episode-list page gives one source per season (same `ref` and `revision`). **Cap:** episodes join while the total stays within the ~3,000-word cap, stopping at an **episode** boundary: the first episode that would cross it ends the join, so the last season may be included only in part. The last season used also counts as only in part when episode rows after its last used episode have no summary (episodes not yet aired or summarized). A season boundary would drop a whole long first season for one episode too many, and a 22-episode season of ~150-word summaries is already over the cap. Because the join can stop at any episode, no lead block is needed: episode-table input never exceeds the cap, at most 20 sources, and the 150-word minimum applies to the total. **Coverage:** the header line from item 9 covers these summaries too; when the last season is only partly included the line says so, for example "Summary covers seasons 1–3 of 9 (season 3 only in part; the summary stops before that season ends).", built from validated integers (the plot file's coverage.partial_season, which must equal the last source's season) and checked exactly by `verify_request()`. Such a summary always gets the line, even when it touches every season. With no Wikidata P2437, the season total also counts the season headings of a verified episode-list page used for the text. **Gate:** the plot file says via "season_articles" and via_detail "episode_table"; `check_source_limits()` refuses such input over the ~3,000-word cap or with a source without a `season`, and `parse_plot()` refuses a malformed via_detail or partial_season, or episode-table input from a fetcher before 1.5.0. **Provenance:** each page is a source as before; the plot file records pages used and their Wikidata evidence, episodes used per season, the partly included season, episodes and seasons left out over the cap, and pages never fetched (its episode_tables entry). Gold labelers read exactly the joined model input, as for item 6 (guide 1.5.0). The plots report counts titles via episode tables per bucket and lists any of the ten priority series (DECISIONS 2026-10-02) that ended skipped; backfill never replaces one.

TMDB's written authorization for LLM use is **[OPEN]**, owned by Hari (section 16). Until TMDB authorizes it in writing, the rule above stands.

---

## 2. Record at a glance

```
schema_version        "1.0.0"
record_kind           llm_annotation | gold_label | illustrative_example
title                 media_type, name, release_year, tmdb_id, wikidata_id, series_status (TV only)
provenance            annotated_at, annotator{...}, sources[...], input_word_count, usage{...}, notes, coverage{...}
outcome               annotated | abstained
abstain_reason        (only when abstained)
layers                (only when annotated)
  surface_story         setting_period, tones[], protagonist_structure, safe_text{...}, spoiler_text{...}
  archetypal_plot       primary{label, confidence}, plots{<every plot>: judgment}, safe_text, spoiler_text
  mythic_blueprint      blueprint{label, confidence}, stages{<every stage>: judgment}, safe_text, spoiler_text
  structural_skeleton   emotional_arc{label, confidence, method, ...}, arc_points[11], chronology, safe_text, spoiler_text
beat_tags             (only when annotated) tags{<every tag>: judgment}, spoiler_text{evidence[{tag, note}]}
```

A **judgment** is `{present: true|false, confidence}` (section 4). Every object is closed (`additionalProperties: false`), so a typo'd or invented field fails validation.

**Who fills what.** The model produces `outcome`, `abstain_reason`, `layers` (minus `emotional_arc`) and `beat_tags`, in the model-facing shape of section 11. The pipeline fills `schema_version`, `record_kind`, `title`, `provenance`, and derives `emotional_arc` from the arc points (section 8).

---

## 3. Spoiler handling

Promise 3 in VISION.md: spoilers are hidden unless the viewer opts in.

<!-- vocab:spoiler_level -->
| Value | Name | Definition |
|---|---|---|
| `none` | Safe | Reveals only the premise: what a trailer, poster or the first quarter of the story would tell you. |
| `mild` | Shape | Reveals the overall direction or tone of the ending (up or down, twist or no twist) without saying what happens. |
| `major` | Spoiler | Reveals specific events after the setup: who dies, what the twist is, how the conflict resolves. |

**Default visibility.** Labels fall into two display groups. Each group has a highest level shown by default. Everything `major`, and all `spoiler_text`, is hidden in both groups until the viewer opts in.

| Group | Fields | Shown by default up to |
|---|---|---|
| Story shape | `primary`, `plots`, `blueprint`, `stages`, `emotional_arc`, `arc_points`, `setting_period`, `protagonist_structure`, `chronology` | `mild` (decided 2026-09-26) |
| Beat tags and tones | `tags`, `tones` | `none` (decided 2026-09-29) |

So `mild` beat tags (`twist_ending`, `redemption_arc`, `ambiguous_ending`) and all `mild` tones stay hidden until the viewer opts in (decided 2026-09-29). Changing either group later is a one-line change to this table. The same table would also cover a viewer setting that hides story shapes (hypothetical; none is planned): it would set the first row to `none`.

"Visible level" below means the level shown for the label's group.

**Rule 1: free text is structurally split.** Every block that carries text has two sibling objects:

- `safe_text`: always displayable. Must stay at spoiler level `none`. If a sentence only makes sense to someone who has seen past the first quarter of the story, it belongs in `spoiler_text`.
- `spoiler_text`: hidden by default. May reveal anything.

No free-text field exists outside these two objects (enforced by a test). A frontend that drops every `spoiler_text` key, at any depth, shows no spoiler text.

**Rule 2: each controlled term has a fixed level**, given in the vocabulary tables below and machine-readable in the schema under `x-laminary-spoiler-levels.vocabularies`. Per-instance overrides are not allowed.

**Rule 3: single-choice fields are hidden whole.** A single-choice field (the primary plot, the blueprint, the emotional arc, setting period, protagonist structure, chronology) has a field-level spoiler level equal to the highest level in its vocabulary (`x-laminary-spoiler-levels.fields`). When that level is hidden, the field shows the same placeholder whatever its value, so "hidden" can't itself reveal a dark ending. The arc line (`arc_points`) is level `mild`.

**Rule 4: presence sets hide per term, silently.** For `tones`, `plots`, `stages` and `tags`, terms above their group's visible level are simply not mentioned. The UI never shows a count, gap or "and 1 more" that would reveal a hidden term exists.

**Rule 5: derived content inherits the highest level it uses.** Any text or grouping computed from labels (why-lines, browse-row membership, share cards, "more like this" explanations) carries the highest spoiler level of the labels it uses. Default views may only use labels at or below their group's visible level. Example: a why-line built on `mentor_dies` is `major` and can't appear in a default view, even if every other label it uses is `none`.

The per-term spoiler levels in the vocabulary tables were approved as drafted (decided 2026-09-26), including `redemption_arc` at `mild`.

---

## 4. Confidence

Two kinds of scored judgment:

- **Single-choice labels** (`primary`, `blueprint`, `emotional_arc`): always present. `confidence` in [0, 1] is the chance the label matches a careful human reading of the same summary. Below 0.50 means "best available fit, and it is weak."
- **Presence judgments** (every term in `plots`, `stages`, `tags`): the annotator judges **every** term present or absent, and `confidence` in [0.5, 1] is confidence in that judgment. It can't go below 0.5, because then the opposite judgment would be the better one.

Because every term gets an explicit judgment, "absent" is always stated. A key that is missing can only happen in a record from an older schema version that predates the term, and it means **not assessed** (section 13).

Descriptive surface fields (setting period, tones, protagonist structure, chronology) carry no confidence. On gold labels, confidence is optional throughout.

Bands, used in both the prompt and the labeling guide:

| Range | Meaning |
|---|---|
| 0.90 to 1.00 | The summary states it directly, or it is unmistakable. |
| 0.70 to 0.89 | Strongly implied; a careful reader would agree. |
| 0.50 to 0.69 | Plausible; reasonable readers could disagree. |
| below 0.50 | Weak (single-choice labels only). |

Display rule (decided 2026-09-30, replacing 0.80 from 2026-09-26): show a label on a title page or use it for a browse row only at confidence **0.95** or higher (`DISPLAY_CONFIDENCE_THRESHOLD` in `laminary_pipeline/annotation.py`). In the bands above, that is the top of "states it directly, or it is unmistakable", so fewer labels are shown than at 0.80. It can be re-tuned after the 500-title pilot, based on gold-set calibration: of labels given 0.95, about 95% should match (section 14). The evaluation's calibration bands split exactly at the threshold (0.95 to 1.00, 0.90 to 0.94, 0.80 to 0.89, ...). The prompt and the labelers use the bands above unchanged.

---

## 5. Layer 1: Surface Story

What happens on the surface: who, where, when, and what they are up against.

| Field | Type | Meaning |
|---|---|---|
| `setting_period` | enum | When the story mainly takes place, relative to its release. |
| `tones` | 1 to 3 enums | The dominant feel while watching. Internal labels for now; frontend proposes user-facing mood names in Phase 3 (decided 2026-09-26). |
| `protagonist_structure` | enum | How many people the story is centered on. |
| `safe_text.logline` | ≤240 chars | One or two sentences stating the premise. Setup only. |
| `safe_text.protagonist` | ≤160 chars | Who the main character is at the start. |
| `safe_text.setting` | ≤120 chars | Place and time in plain words. |
| `safe_text.central_conflict` | ≤200 chars | The question the story sets up. |
| `spoiler_text.resolution` | ≤400 chars | How the central conflict resolves. |

<!-- vocab:setting_period -->
| Value | Name | Spoiler | Definition |
|---|---|---|---|
| `historical_past` | Historical | none | Set in a real-world period clearly before the title's release (roughly 20 or more years earlier). |
| `contemporary` | Contemporary | none | Set in the real world at, or within about 20 years before, the title's release. |
| `near_future` | Near future | none | Recognizably our world, moved forward in time with some changed technology or society. |
| `far_future` | Far future | none | A future so distant or transformed that present-day society is no longer the reference point. |
| `invented_world` | Invented world | none | A secondary world with its own geography and history, not tied to real-world time (high fantasy, space opera without an Earth timeline). |
| `mixed` | Mixed | none | Two or more of the above carry comparable weight (for example a simulated present inside a far-future reality). |

<!-- vocab:tone -->
| Value | Name | Spoiler | Definition |
|---|---|---|---|
| `tense` | Tense | none | Sustained suspense or danger keeps the viewer on edge. |
| `hopeful` | Hopeful | none | Characters and viewer are invited to expect things can get better. |
| `playful` | Playful | none | Light, comic or mischievous in manner, whatever the stakes. |
| `warm` | Warm | none | Affectionate toward its characters; relationships and kindness are foregrounded. |
| `eerie` | Eerie | none | Uncanny, unsettling or dread-laden without necessarily being violent. |
| `melancholic` | Melancholic | none | A pervasive sadness or wistfulness, often about loss or time. |
| `cerebral` | Cerebral | none | Driven by ideas, puzzles or philosophical questions the viewer is meant to work through. |
| `bleak` | Bleak | mild | Unrelentingly dark; little relief or hope is offered. |
| `bittersweet` | Bittersweet | mild | Joy and loss arrive together, especially in how things end. |
| `triumphant` | Triumphant | mild | Builds toward a rousing, cathartic win. |

Confusion: `playful` is a tone and says nothing about plot. A playful film can be Booker `comedy` or not; see Layer 2.

<!-- vocab:protagonist_structure -->
| Value | Name | Spoiler | Definition |
|---|---|---|---|
| `single` | Single lead | none | One character's goals and change drive the story, even if others get subplots. |
| `dual` | Two leads | none | Two characters share the center with comparable weight (buddy stories, two-handers, romances told from both sides). |
| `ensemble` | Ensemble | none | Three or more characters share the center and no one of them dominates. |

---

## 6. Layer 2: Archetypal Plot (Booker's basic plots)

Which of Christopher Booker's plots best describes the story's engine: what drives events from start to finish. v1 uses all nine: the seven basic plots plus Rebellion Against "The One" and Mystery (decided 2026-09-26).

| Field | Meaning |
|---|---|
| `primary` | The single best-fitting plot, with confidence. Always present. |
| `plots` | A judgment for every plot: is it present as a major thread? The primary must be judged present. At most two other plots may be present (the "secondary" plots). |
| `safe_text.rationale` | Why, using setup-level evidence only. ≤280 chars. |
| `spoiler_text.rationale` | Full reasoning. ≤400 chars. |

<!-- vocab:booker_plot -->
| Value | Name | Spoiler | Definition |
|---|---|---|---|
| `overcoming_the_monster` | Overcoming the Monster | none | The protagonist confronts a powerful threatening force (a creature, person, group or system) that endangers them or their community; the story is driven by that threat and the fight against it. |
| `rags_to_riches` | Rags to Riches (plot) | mild | A lowly or overlooked protagonist gains status, love or self-worth, loses it or hits a crisis, and then earns a fuller, lasting fulfillment. The engine is the protagonist's growth into who they could be. |
| `the_quest` | The Quest | none | The protagonist, usually with companions, sets out on purpose toward a distant goal (a place, object or person) and must overcome a series of obstacles to reach it; the destination drives the story. |
| `voyage_and_return` | Voyage and Return | none | The protagonist is taken or falls into an unfamiliar world, is first fascinated then threatened by it, and makes it back home changed; the point is the round trip and what it teaches. |
| `comedy` | Comedy (Booker) | none | Confusion, misunderstanding, disguise or social obstacles keep people apart; the story ends when the confusion is cleared and they are united or reconciled. Not the same as "funny." |
| `tragedy` | Tragedy | mild | A flaw, ambition or transgression draws the protagonist down a path that ends in their destruction or death. Apply only when the downfall is the ending. |
| `rebirth` | Rebirth | mild | The protagonist falls under a dark power or deadened state (curse, bitterness, spiritual numbness) and is freed from it by another person or by a transforming realization; the story is about that release. |
| `rebellion_against_the_one` | Rebellion Against "The One" | none | The protagonist defies an all-powerful authority or system that governs their whole world (a state, an institution, a controlling order); the story follows that defiance through to escape, overthrow, or the rebel's defeat and submission. |
| `mystery` | Mystery | none | A character, often an outsider such as a detective, reporter or curious bystander, investigates a puzzling event, usually a crime; the investigation and the uncovering of the truth drive the story. |

Common confusions:

- **Rags to Riches the plot vs Rags to Riches the arc.** The plot (here) is about a lowly person's growth and usually has a mid-story crisis, so its arc is often `cinderella`, not the `rags_to_riches` arc. The arc (Layer 4) is only the shape of fortune over time: a steady rise with no major setback.
- **Tragedy vs a falling arc.** `tragedy` is about cause and ending: a self-made downfall. `riches_to_rags` or `icarus` is only the shape. A disaster film where good people suffer bad luck may have a falling arc without being a Tragedy.
- **Rebirth vs the `redemption_arc` beat tag.** Rebirth is the whole story's engine. A redemption arc tag can apply to any character, including a secondary one, inside any plot.
- **Quest vs Voyage and Return.** In a Quest the protagonist chooses a goal and the story ends on reaching it. In Voyage and Return the protagonist is thrown into the other world and the story ends on getting home.
- **Comedy vs playful tone.** A very funny film about defeating a villain is `overcoming_the_monster` with a `playful` tone.
- **Rebellion Against "The One" vs Overcoming the Monster.** The Monster is an outside threat that invades or endangers the protagonist's world and is fought in order to destroy it. "The One" *is* the order of the protagonist's world, and the story is about refusing to be absorbed by it. When a hero fights a controlling system's enforcers and wins, both may be present; pick the primary by what the story spends most of its time on. "The One" is always the ruling power, never the hero. A hero called "the One" by the story (as in The Matrix) says nothing about this plot.
- **Mystery vs `twist_ending`.** A late surprise doesn't make a Mystery. Mystery requires an investigation that drives the plot. A thriller built around a hidden secret is not a Mystery unless a character's inquiry is the engine.
- **Mystery vs The Quest.** Both involve searching. In a Quest the goal is known and the story is the journey to reach it. In a Mystery the question is what happened, and the story is working it out. A hunt for a missing person is a Quest if their whereabouts drive a journey, and a Mystery if uncovering what happened to them is the point.
- **Mystery vs Overcoming the Monster.** A detective hunting a killer can be both. If the story is organized around working out what happened, it's Mystery; if it is organized around stopping an identified threat, it's Overcoming the Monster.

---

## 7. Layer 3: Mythic Blueprint

Whether the story follows a mythic journey pattern, which one, and which Hero's Journey stages it covers.

| Field | Meaning |
|---|---|
| `blueprint` | The best-fitting journey pattern, with confidence. Always present. |
| `stages` | A judgment for every Hero's Journey stage. Recorded for every blueprint, including variants, so coverage is comparable across titles. |
| `safe_text.rationale` / `spoiler_text.rationale` | As in Layer 2. |

<!-- vocab:mythic_blueprint -->
| Value | Name | Spoiler | Definition |
|---|---|---|---|
| `heros_journey` | Hero's Journey | none | The protagonist leaves a familiar world, is tested in an unfamiliar one, survives a decisive ordeal largely through their own growth, and returns changed, bringing back something of value to others. |
| `heroines_journey` | Heroine's Journey | none | A journey whose strength comes through connection: the protagonist is cut off or descends, gathers or rebuilds a network of allies, and resolves the story by restoring or forming a community rather than through a solitary victory. Not about the protagonist's gender. Follows Gail Carriger, The Heroine's Journey (2020). |
| `anti_hero_descent` | Anti-hero descent | mild | A morally compromised protagonist's journey runs downward: each step takes them deeper into wrongdoing or self-destruction, and there is no return that benefits others. In-house Laminary definition, not from a published model. |
| `no_clear_blueprint` | No clear blueprint | none | None of the three patterns organizes the story (slice-of-life, pure procedurals, many ensemble dramas). Stages may still be judged present individually. |

Confusions: an unlikable protagonist who ends up redeemed is not an `anti_hero_descent`; that is usually `heros_journey` or `rebirth` with a `redemption_arc` tag. A found-family story is not automatically a Heroine's Journey; it must also resolve through the network.

Stages follow Christopher Vogler's 12-stage film version of Campbell's monomyth, from The Writer's Journey (decided 2026-09-26). Names are Vogler's own.

<!-- vocab:journey_stage -->
| Value | Name | Spoiler | Definition |
|---|---|---|---|
| `ordinary_world` | Ordinary World | none | The protagonist's normal life is shown before the adventure, establishing what they lack or want. |
| `call_to_adventure` | Call to Adventure | none | An event, message or challenge disrupts normal life and invites or forces a new course. |
| `refusal_of_the_call` | Refusal of the Call | none | The protagonist hesitates, resists or declines the call, at least at first. |
| `meeting_with_the_mentor` | Meeting with the Mentor | none | A guide figure gives advice, training, a gift or confidence needed for the journey. |
| `crossing_the_first_threshold` | Crossing the First Threshold | none | The protagonist commits and enters the unfamiliar world or situation; there is no easy way back. |
| `tests_allies_enemies` | Tests, Allies, Enemies | none | In the new world, the protagonist faces trials and learns who can be trusted. |
| `approach_to_the_inmost_cave` | Approach to the Inmost Cave | mild | Preparation for and movement toward the place or moment of greatest danger. |
| `ordeal` | Ordeal | mild | The central crisis: a confrontation with death, defeat or the protagonist's greatest fear. |
| `reward` | Reward (Seizing the Sword) | mild | Having survived the ordeal, the protagonist gains something: an object, knowledge, reconciliation or a new strength. |
| `the_road_back` | The Road Back | mild | The protagonist sets out to return or finish, often chased or facing consequences of the ordeal. |
| `resurrection` | Resurrection | mild | A final, climactic test where the protagonist is nearly destroyed and emerges transformed. |
| `return_with_the_elixir` | Return with the Elixir | mild | The protagonist comes home or to a new equilibrium bringing something that benefits others. |

A stage counts as present only if the summary shows an identifiable story event that performs its function. Ordeal vs Resurrection: the ordeal is the midpoint-to-late central crisis; resurrection is the final climactic test. If the summary shows only one such crisis near the end, mark `resurrection` present and `ordeal` absent.

---

## 8. Layer 4: Structural Skeleton

The shape of the story over time and how it is told.

| Field | Meaning |
|---|---|
| `arc_points` | Exactly 11 numbers: the protagonist's fortune at t = 0.0, 0.1, ..., 1.0. Drawn as the story-shape arc line. Spoiler level `mild`. Written by the annotator. |
| `emotional_arc` | Which of the six core arcs the points trace. **Derived by the pipeline** from `arc_points` with the rule below (method `derived`); the model never picks it. |
| `chronology` | How story events are ordered in the telling. |
| `safe_text.rationale` / `spoiler_text.rationale` | As in Layer 2. |

### Arc points

- **t** is position in the telling, from the first scene (0.0) to the last (1.0), in presentation order, not in-world chronology. For a TV series, t spans the whole aired run. t is implicit: the i-th value is at t = i/10.
- **Fortune** is the protagonist's actual situation as the story presents it: safety, status, relationships, prospects. Range -1 (the worst state the story puts them in) to +1 (the best); 0 is neither good nor bad. For `ensemble` stories use the central group's collective fortune.
- **Situation, not mood.** Fortune tracks the protagonist's circumstances, not how they feel in the moment. Enjoying oneself while still trapped does not raise fortune; only a real change in the situation does (escape, a gain that lasts, a relationship that changes). Example: a character stuck in a time loop who spends a stretch indulging himself is still trapped, so fortune stays low.
- Values need not start at 0. Use one or two decimal places.
- For `ongoing` and `unknown` series, the points cover episodes aired as of the source revision.

### Deriving the emotional arc

The six core arcs (Reagan et al., 2016, after Vonnegut) are defined by the directions of the story's **major moves**. The same rule is used by the pipeline, the gold-set guide and the checker (`laminary_pipeline/arc.py`):

1. **Major move.** A rise or fall of at least 0.3 from the most recent peak or trough. Smaller wobbles are ignored. Differences are rounded to 6 decimals before comparing, so a move of exactly 0.3 counts.
2. **One to three major moves** map directly to an arc (table below).
3. **Four or more major moves** (common in long series): raise the threshold in steps of 0.1 (0.4, 0.5, ...) until at most three moves remain, then map. The threshold stops at 2.0, the full fortune range. The threshold used is stored in `emotional_arc.threshold_used`. The record sets `reduced_shape` to true exactly when the threshold used is 0.6 or more. Without this, a W or M shape collapses into one confident leg: the W in the tests reduces to `rags_to_riches` at 0.8. Noisy climbs that settle by 0.4 or 0.5 stay unflagged; the tests include one that is an unflagged `rags_to_riches` at 0.4.
4. **No major moves** (a flat or gentle story), **or still four or more at 2.0** (for example, swings between the extremes): fall back to the direction of the net change, last point minus first point. A net rise maps to `rags_to_riches`; a net fall, or exactly zero, maps to `riches_to_rags`. The record sets `net_change_fallback` to true. This label is a hidden placeholder: it is never shown and never used in shape rows (decided 2026-09-29). There is no "flat" arc term in 1.0.0; a "Steady" shape will be revisited after the 500-title pilot. Arc points are stored, so adding one later needs no re-annotation.

The move detection is symmetric: reversing the points in time, or flipping their sign, reverses or flips the moves. A sub-threshold opening move is dropped exactly like a sub-threshold closing one. This is property-tested.

**Consumer rule: flagged arc labels are ignored.** Browse rows, why-lines, "more like this", share cards and title pages ignore an `emotional_arc` label when `net_change_fallback` or `reduced_shape` is true, whatever its confidence and the display threshold. The arc line itself may still be drawn, since the points are valid. Stored confidence for these labels is also capped at 0.49, but the rule does not depend on that. Helper: `usable_arc_label()` in `laminary_pipeline/annotation.py`.

Stored confidence for a derived arc is the annotator's `arc_confidence` (how well the points capture the story), capped at 0.49 for flagged labels. It is required on LLM records.

<!-- vocab:emotional_arc -->
| Value | Name | Spoiler | Definition |
|---|---|---|---|
| `rags_to_riches` | Rags to Riches (arc) | mild | One major move: a sustained rise. Also the fallback label for a story with no major moves and a net rise. |
| `riches_to_rags` | Riches to Rags | mild | One major move: a sustained fall. Also the fallback label for a story with no major moves and a net fall or no net change. |
| `man_in_a_hole` | Man in a Hole | mild | Two major moves: fall, then rise. Things go wrong, then recover. |
| `icarus` | Icarus | mild | Two major moves: rise, then fall. Success builds and then collapses. |
| `cinderella` | Cinderella | mild | Three major moves: rise, fall, rise. Early gains are lost in a crisis, then regained. |
| `oedipus` | Oedipus | mild | Three major moves: fall, rise, fall. An early blow, a recovery, then a final collapse. |

<!-- vocab:arc_method -->
| Value | Name | Definition |
|---|---|---|
| `derived` | Derived | Label computed from `arc_points` by the rule above. Required on LLM records; `threshold_used`, `net_change_fallback` and `reduced_shape` must match the rule. |
| `labeler_assigned` | Labeler-assigned | A gold labeler chose the label directly (arc points optional). |

**Gold guide for flat stories.** A gold labeler who judges that a story has no major move assigns the net-change label by the same rule (`rags_to_riches` for a net rise, `riches_to_rags` otherwise) and sets `net_change_fallback` to true, using method `labeler_assigned`. If a "Steady" shape is added after the pilot, the flagged gold titles are the ones to relabel; their arc label was only a placeholder.

**Comparing gold and model arcs.** The primary arc metric compares the model's derived label with the gold label, whichever method the gold labeler used. Titles where either side is flagged (`net_change_fallback` or `reduced_shape`) are scored and reported separately, not mixed into the headline accuracy. When the gold record also has arc points, the evaluation additionally reports (a) agreement between labels derived from both point sets, and (b) mean absolute difference between the two point sets, as a shape distance.

Confusions:

- **Rags to Riches arc vs Booker plot:** see Layer 2. The arc is a steady rise only.
- **Man in a Hole vs Cinderella:** Cinderella has a distinct rise *before* the fall. If the story opens stable and things go wrong early, it is Man in a Hole.

<!-- vocab:chronology -->
| Value | Name | Spoiler | Definition |
|---|---|---|---|
| `linear` | Linear | none | Events are told mostly in the order they happen; brief flashbacks or a teaser opening don't change this. |
| `in_medias_res` | In medias res | none | Opens in the middle of the action, then goes back to show how it got there, then continues forward. |
| `frame_narrative` | Frame narrative | none | The main story is told inside an outer story (a narrator recounting, an interview, a found record). |
| `nonlinear` | Nonlinear | none | Timelines are interleaved or shuffled so that order of telling is a major part of the experience. |
| `reverse_chronology` | Reverse chronology | none | The main line of events is told end-first, moving backward in time. |

---

## 9. Beat tags

Recurring story beats that cut across the four layers. `beat_tags.tags` holds a judgment for every tag. `beat_tags.spoiler_text.evidence` optionally holds one sentence of evidence per present tag, as a list of `tag` and `note` pairs, each tag at most once and only for tags judged present. Evidence is always spoiler text, even for `none`-level tags.

No tags will be added until the gold set shows which are reliable; then 10 to 20 more may be added as a minor version (decided 2026-09-26).

<!-- vocab:beat_tag -->
| Value | Name | Spoiler | Definition |
|---|---|---|---|
| `mentor_dies` | Mentor dies | major | A character who guides, trains or protects the protagonist dies during the story, and the loss matters to the protagonist's path. |
| `twist_ending` | Twist ending | mild | A late revelation substantially changes the meaning of what came before. Not merely a surprise event. |
| `unreliable_narrator` | Unreliable narrator | major | The story is told through a narrator or point-of-view character whose account the viewer later learns is false, distorted or incomplete in a way that matters. |
| `found_family` | Found family | none | Unrelated characters form bonds of loyalty and care that function as a family, and that bond is central to the story. |
| `redemption_arc` | Redemption arc | mild | A character who has done serious wrong makes a meaningful turn toward atonement or goodness during the story. Can apply to any major character. |
| `pyrrhic_victory` | Pyrrhic victory | major | The protagonist wins the central conflict at a cost so high it undercuts or outweighs the win. |
| `time_loop` | Time loop | none | One or more characters relive the same stretch of time repeatedly, keeping memories between repetitions. |
| `heist_structure` | Heist structure | none | The story is organized around planning and executing a theft or elaborate con by a team. |
| `ensemble_convergence` | Ensemble convergence | none | Several storylines that start separately are brought together, and their intersection is the payoff. |
| `ambiguous_ending` | Ambiguous ending | mild | The ending deliberately leaves a central question unresolved or open to competing readings. |

Confusions:

- **Twist ending vs unreliable narrator:** an unreliable narrator often produces a twist; judge both present when both apply. A twist that doesn't come from a narrator's distortion is `twist_ending` only.
- **Ambiguous ending vs twist ending:** a twist answers a question differently than expected; an ambiguous ending declines to answer.
- **Pyrrhic victory vs Tragedy:** in a pyrrhic victory the protagonist does win. In a Tragedy they are destroyed; both may apply.
- **Time loop vs nonlinear chronology:** a time loop is an in-world event; nonlinear is a way of telling.

---

## 10. Provenance, record kinds and abstaining

<!-- vocab:record_kind -->
| Value | Name | Definition |
|---|---|---|
| `llm_annotation` | LLM annotation | Produced by the annotation pipeline. Requires `annotator.model_version`, `prompt_version`, `run_id`, `provenance.usage`, an integer `tmdb_id`, at least one source (Wikipedia only), `input_word_count` of 150 or more, and a derived `emotional_arc`. |
| `gold_label` | Gold label | Hand-labeled for evaluation from the same summary the model sees (decided 2026-09-26). Requires `annotator.labeler_id`, `guide_version`, an integer `tmdb_id`, at least one source (Wikipedia only) and `input_word_count` of 150 or more. Only the scored labels are required (below). |
| `illustrative_example` | Illustrative example | Hand-written for documentation and tests. Never loaded into the product. Requires `annotator.labeler_id`. |

**Gold records are light.** An annotated gold record must have: `primary`, `plots`, `blueprint`, `stages`, `emotional_arc` (either method) and `tags`. Everything else (Surface Story, free text, arc points, chronology, evidence, confidences) is optional.

<!-- vocab:media_type -->
| Value | Name | Definition |
|---|---|---|
| `movie` | Movie | A feature film. `tmdb_id` is in TMDB's movie namespace. |
| `tv_series` | TV series | A whole series, annotated at series level. `tmdb_id` is in TMDB's tv namespace. `series_status` is required. |

<!-- vocab:series_status -->
| Value | Name | Definition |
|---|---|---|
| `ended` | Ended | No further seasons are expected. |
| `ongoing` | Ongoing | More seasons may come. The annotation covers episodes aired as of the source revision. |
| `unknown` | Unknown | Not known whether more seasons will come. Treated like `ongoing` for "as of" displays and refresh. |

**What counts as evidence (schema 1.1.0, decided 2026-09-30).** Wikidata often has no end date for series that have ended, so a missing end time does not mean `ongoing`. Ingest sets the status from Wikidata (CC0) and records the basis in the candidate's series_status_basis field (pipeline data, not part of the record):

- `ended`: the series has an end time (P582) with a year (basis P582).
- `ongoing`: Wikidata explicitly states the end time has no value (basis P582_novalue), meaning an editor asserted the series has not ended.
- `unknown`: neither (basis none), or both, which conflict (basis conflict). A recent season start date is not counted as evidence of `ongoing`, because it cannot tell a final season from a continuing one.

Candidate and plot files written before 1.1.0 have no basis; their `ongoing` is read as `unknown` (their `ended` stands, since it came from P582). Re-running `ingest candidates` refreshes them.

**Annotated as of, and refresh (decided 2026-09-26).** A record's "as of" date is the latest `retrieved_at` among its `sources`. Displays for `ongoing` series should say so ("covers episodes aired up to" that date). Ingestion re-fetches sources on its normal schedule; when any source's `content_sha256` changes, the title is re-annotated and the new record replaces the old one. Ended series and movies follow the same rule, which also catches substantial Wikipedia rewrites.

Each entry in `provenance.sources` records `kind`, `ref` (a Wikipedia URL; `tmdb:movie/<id>` style refs appear only in illustrative records, since TMDB text is not an allowed input), `revision` (Wikipedia revision id), `retrieved_at`, `license`, `word_count` and `content_sha256` of the exact text sent to the model, plus `season` for a per-season article (section 1 item 6; sources are then in season order, at most 20). With `model_version`, `prompt_version` and the hash, any record can be reproduced or detected as stale.

<!-- vocab:source_kind -->
| Value | Name | Definition |
|---|---|---|
| `wikipedia_plot` | Wikipedia plot section | The "Plot" (or "Synopsis" / "Premise") section of the English Wikipedia article, as plain text; for a series summarized from season articles, one season article's plot, summary or synopsis sections (section 1 item 6). CC BY-SA; triggers the attribution requirement in section 1. |
| `tmdb_overview` | TMDB overview | The overview field from TMDB's API. Kept so the vocabulary is stable, but **not allowed as an annotation input** until TMDB authorizes LLM use in writing; the schema rejects it on LLM and gold records. |

<!-- vocab:source_license -->
| Value | Name | Definition |
|---|---|---|
| `CC-BY-SA-4.0` | CC BY-SA 4.0 | Wikipedia text from revisions under CC BY-SA 4.0 (current). |
| `CC-BY-SA-3.0` | CC BY-SA 3.0 | Wikipedia text from older revisions under CC BY-SA 3.0. |
| `TMDB-API-terms` | TMDB API terms | TMDB content, used under the TMDB API terms (attribution required; commercial agreement pending, PLAN §8). |

<!-- vocab:input_license -->
| Value | Name | Definition |
|---|---|---|
| `CC-BY-SA-4.0` | CC BY-SA 4.0 | Allowed license for a source on an LLM or gold record (Wikipedia, current revisions). |
| `CC-BY-SA-3.0` | CC BY-SA 3.0 | Allowed license for a source on an LLM or gold record (Wikipedia, older revisions). |

The `input_license` subset is what LLM and gold records may cite, together with a `wikipedia_plot` kind and an `https://en.wikipedia.org/wiki/` article ref. `TMDB-API-terms` stays in `source_license` for illustrative records and future use.

`provenance.usage` records token counts per title (and whether the Batch API was used) so cost per title is known after every run; it is required on LLM records. `provenance.notes` is internal only.

`provenance.coverage` (schema 1.2.0, optional) records a partial season-article summary (section 1 item 9): `seasons` (the season numbers covered, equal to the sources' `season` fields), `total_seasons` and `total_seasons_basis` when the total is known, and `statement`, the exact header line the annotator saw. It is absent when the summary is not known to be partial. For episode-table summaries (section 1 item 11) `statement` may end "(season N only in part; the summary stops before that season ends)" when the last season was cut at the word cap or its later episodes have no summary; `seasons` still lists that season, since its source is present. Display: a title page for such a record should say the analysis covers only those seasons.

<!-- vocab:season_total_basis -->
| Value | Name | Definition |
|---|---|---|
| `wikidata_P2437` | Wikidata number of seasons | The series' Wikidata "number of seasons" (P2437, CC0), used when it is a whole number at least as large as every verified season article. |
| `verified_season_pages` | Verified season articles | The highest season number among the season articles that Wikidata places in the series (P179/P361), used when P2437 is missing or smaller. For episode-table summaries from a verified episode-list page (section 1 item 11), that page's season headings count too. |

<!-- vocab:outcome -->
| Value | Name | Definition |
|---|---|---|
| `annotated` | Annotated | The summary supported a full annotation. `layers` and `beat_tags` are required. |
| `abstained` | Abstained | The annotator declined. `abstain_reason` is required and no layers or tags are present. Abstaining is correct behavior, not a failure. |

<!-- vocab:abstain_reason -->
| Value | Name | Definition |
|---|---|---|
| `summary_too_thin` | Summary too thin | The summary passes the word gate but covers only the premise, or too little of the story to judge arc, plot and ending. |
| `summary_contradictory` | Summary contradictory | Sources disagree on major events, or the summary is internally inconsistent. |
| `not_a_narrative` | Not a narrative | The title has no story to annotate (concert film, stand-up special, most documentaries, reality or competition TV). |
| `summary_title_mismatch` | Summary/title mismatch | The summary appears to describe a different work (a remake, a namesake, the source novel). |

---

## 11. Model-facing output schema

Claude's structured outputs accept only a subset of JSON Schema. Per the structured-outputs documentation (platform.claude.com, "JSON Schema limitations" and "Schema complexity limits", read 2026-09-26): no `minimum`/`maximum`, no `minLength`/`maxLength`, no array constraints except `minItems` of 0 or 1, `additionalProperties` only `false` (so no key-to-value maps), no `if`/`then`, at most 24 optional properties, and at most 16 union-typed properties per request.

`model_output_schema()` in `laminary_pipeline/model_output.py` derives the schema sent to the API from the stored schema:

- All `$ref`s are inlined, and unsupported keywords are stripped.
- Presence judgments are fixed-key objects (every term is a required key), not maps. This works within the limits, and it forces a judgment on every term. A nullable confidence per term (29 of them) would exceed the 16-union limit, which is why a judgment is `{present, confidence}` rather than a confidence or null.
- `arc_points` is an object with 11 required keys, `t00` to `t10`, so the grammar enforces the count. The model also returns `arc_confidence`. It does not return `emotional_arc`.
- `layers` and `beat_tags` are nullable (null when abstaining), and `abstain_reason` accepts null when annotating, so every property is required. That makes 2 union-typed properties and 0 optional ones.

`to_record()` converts a model output to a stored record (deriving the arc), and `annotation.validate_record()` then checks it. **Constraints enforced only on the client side**, after the model responds:

- Numeric ranges: `confidence` in [0, 1], judgment confidence in [0.5, 1], `arc_points` in [-1, 1].
- String lengths on all text fields.
- `tones`: at most 3, no repeats.
- `evidence`: at most 10 entries.
- Every semantic check in section 12.

A failure on any of these is treated like invalid output: the title is retried or sent for review, never stored.

**First step of Phase 1: one live structured-output call.** The schema's grammar complexity ("Schema is too complex for compilation" and the compile timeout) can only be tested against the API. Before building the batch pipeline, send one real request with `model_output_schema()` and a single Wikipedia summary, and record the result, latency and token usage. This spends money, so it goes through Hari's Phase 1 spend approval with the pilot.

---

## 12. Checks beyond JSON Schema

Implemented in `annotation.semantic_errors()` and run by `validate_record()` on every record:

1. The `primary` plot is judged present in `plots`, and at most two other plots are present.
2. `evidence` lists each tag at most once, and only tags judged present.
3. For a derived `emotional_arc`: `label`, `threshold_used`, `net_change_fallback` and `reduced_shape` equal what the rule in section 8 gives for `arc_points`, and a flagged label has confidence below 0.5.
4. For LLM records: `input_word_count` equals the sum of `sources[].word_count`.
6. `provenance.coverage` (schema 1.2.0): `seasons` are in increasing order and equal to the sources' `season` numbers; `total_seasons` comes with `total_seasons_basis` and is at least the highest covered season.
5. Date-times are checked as RFC 3339 (the validator registers its own `date-time` format check, because jsonschema skips it without an optional package).

Done at ingestion, not here: `tmdb_id` exists in TMDB and its title and year match.

---

## 13. Versioning

`schema_version` is semantic versioning, fixed by `const` in the schema so a record can't claim a version it doesn't conform to. The current version is 1.2.0.

- **Major** (1.0.0 → 2.0.0): a field or term is removed, renamed or redefined. Old records must be migrated or re-annotated. Backend is told first; they own the migration.
- **Minor** (1.0.0 → 1.1.0): a new optional field or a new vocabulary term. For fixed-key judgments a new term is a new required key, so old records validate against their own version only. They are treated as **not assessed** for the new term until re-run, never as "absent".
- **Patch** (1.0.0 → 1.0.1): wording in this document only, with no change to what a label means.

`prompt_version` changes whenever the annotation prompt text changes (including definition wording pulled from this document), and `model_version` records the exact model id. Evaluation results are always reported per (schema_version, prompt_version, model_version).

**Changelog, 1.1.0 → 1.2.0 (2026-10-02, DECISIONS 2026-10-02):** `provenance` gains an optional `coverage` object (`seasons`, `total_seasons`, `total_seasons_basis`, `statement`) and the vocabulary `season_total_basis`, for series summaries that cover only some seasons (section 1 item 9); semantic check 6 in section 12. A minor change: no field is removed or renamed. Records at 1.1.0 validate against 1.1.0 only; none exist outside tests (no LLM or gold run has happened), and no database table uses these fields yet. Backend mirrors `provenance.coverage` when it builds the tables, and frontend may show "covers seasons 1–4 of 7" on title pages.

**Changelog, 1.0.0 → 1.1.0 (2026-09-30, DECISIONS 2026-09-30):** `series_status` gains `unknown`; a missing Wikidata end date now means `unknown`, not `ongoing` (section 10 says what counts as evidence). The `ref` pattern for LLM and gold sources is tightened from any `https://en.wikipedia.org/` URL to an article URL, `^https://en\.wikipedia\.org/wiki/[^\s?#<>\[\]{}|"]+$`, the same pattern the input gate (`require_wikipedia_sources()`) enforces. Sources gain an optional `season` (DECISIONS 2026-10-01, season articles), and `provenance.sources` may hold up to 20 entries instead of 5. All are minor changes: no field is removed or renamed. Records at 1.0.0 validate against 1.0.0 only. None exist outside tests yet, and no database table uses these fields yet; backend mirrors both changes when it builds the tables.

**Doc-only note, 2026-09-30:** the display threshold rose from 0.80 to 0.95 (DECISIONS 2026-09-30). The JSON schema and `schema_version` are unchanged; only `DISPLAY_CONFIDENCE_THRESHOLD`, the evaluation's calibration bands and this document changed.

**Changelog, 0.1.0 → 1.0.0:** the Phase 0 drafts reviewed by QA and approved by Hari. Presence maps became fixed-key judgments. `emotional_arc` is derived from the arc points, with `net_change_fallback` and `reduced_shape` flags. Booker has nine plots, and Vogler's stage names are exact. Gold records are light. Input is Wikipedia-only, with the 150-word gate. `usage` is required on LLM records. 1.0.0 itself changes only the version, and narrows `reduced_shape` to "threshold used is 0.6 or more".

0.2.0 → 0.3.0 changes: `emotional_arc.reduced_shape` added; the threshold search stops at 2.0; `confidence` is required on LLM arcs; LLM and gold sources must have a CC BY-SA license and an en.wikipedia.org ref.

0.1.0 → 0.2.0 changes: presence maps became fixed-key judgments; `emotional_arc` is derived; Booker has nine plots; Vogler stage names are exact; gold records are lighter; `usage` is required on LLM records; the 150-word gate is enforced.

---

## 14. Phase 1 evaluation plan

Run on the ~100-title gold set after each prompt iteration, reported per (schema_version, prompt_version, model_version) together with cost per title.

| Category | Metric |
|---|---|
| `primary` plot | Exact-match accuracy and Cohen's kappa against gold. |
| `plots`, `stages`, `tags` | Per-term precision, recall and F1 of "present", plus accuracy per term. |
| `blueprint` | Exact-match accuracy. |
| `emotional_arc` | Exact-match accuracy of the derived label vs gold, on titles where neither side is flagged. Fallback titles and reduced-shape titles are reported separately (count and agreement). Shape distance when gold has points (section 8). |
| Surface fields | Accuracy of `setting_period`, `protagonist_structure`, `chronology`; overlap of `tones`. Informational only. |
| Calibration | Accuracy per confidence band (section 4); the display threshold is tuned from this. |
| Abstention | Abstention rate, and abstentions on titles gold labelers could label. |
| Flat and reduced arcs | Rate of `net_change_fallback` titles and of `reduced_shape` titles, **split by movies and series**, over the gold set and the whole pilot batch. This informs the "Steady" shape decision after the pilot. |
| **Spoiler leaks** | Gold labelers flag any `safe_text` that goes past the setup, graded `mild` or `major`. Target: 0 `major` leaks; report the `mild` rate. |

If the leak metric misses its target, the fix is prompt work (tighter setup-only instructions, examples of leaks) or a separate cheap pass that rewrites `safe_text` from the setup portion of the Wikipedia plot. The second option costs an extra call per title and would be proposed with a cost estimate. TMDB overviews are not an option (section 1).

**Phase 1 exit gate (decided 2026-09-30, replacing the 2026-09-29 target of 80%).** Measured against the gold set by `python -m laminary_pipeline.evaluate`. Phase 1 passes only if A, B and coverage all pass:

- **Field:** "archetype" means `archetypal_plot.primary.label`.
- **Matching rule (strict):** the model's primary equals the gold primary. A model abstention, or a gold title with no model record, counts as a miss. Gold titles the labeler skipped have no primary and are not scored. Titles whose model record and gold label come from different summaries (source hashes differ) are excluded and listed.
- **A, overall:** accuracy at least **85%**, or, when at least **20** double-labeled titles are scored, at least human–human agreement minus **5 points**. The report gives both numbers, plus human agreement and its N whenever double labels exist.
- **B, shown labels:** among scored titles whose model primary confidence is at least the display threshold (0.95, section 4), accuracy at least **95%**. Fewer than **30** such titles is "insufficient data", and B does not pass.
- **Coverage:** at least **80** scored titles and at least **90%** of the gold titles labeled `annotated` (DECISIONS 2026-09-30). The count and share of scored titles at or above the display threshold are always reported, since that is the share of titles that would show a primary plot.
- **Double labeling:** 25 gold titles (DECISIONS 2026-09-30: 20 to 25) are labeled independently by two people. Slot 1 is the reference the model is scored against. Human–human agreement uses the same strict rule with slot 2 in the model's place, so a slot-2 skip is a miss and titles slot 1 skipped are not scored. Every disagreement is listed for Hari to adjudicate.
- **Emotional arc reported separately, not gating:** exact-match accuracy of the derived `emotional_arc` label against gold, with fallback and reduced-shape titles broken out.
- **Also reported, not gating:** the flat and reduced arc rates above, lenient plot agreement (the model's primary is judged present in the gold `plots`), and kappa.

Why two parts: a flat 95% overall likely exceeds how often two careful people agree on overlapping Booker plots, so the 95% bar is applied to the labels users actually see.

---

## 15. Browse rows

Decided 2026-09-26: keep all 15 for now and prune after the 500-title pilot shows row sizes. Row names are working titles; final copy belongs to frontend and Hari. Each row uses only labels at or above the 0.95 display threshold (DECISIONS 2026-09-30; re-tunable after the pilot).

The "Spoiler" column is the highest level any label in the row's query carries, and the group it comes from (section 3). Arc rows use only usable arc labels (section 8 consumer rule), so fallback and reduced-shape labels never appear in rows 1 to 4.

| # | Working row name | Query | Spoiler | Rationale |
|---|---|---|---|---|
| 1 | Down and back up | arc `man_in_a_hole` | mild | The most common satisfying shape; big, varied row that anchors the concept. |
| 2 | Rise and fall | arc `icarus` | mild | Instantly understood; covers crime sagas, biopics and cautionary tales across genres. |
| 3 | Lost it all, won it back | arc `cinderella` | mild | Distinct from row 1 (the early rise), so it teaches that shape matters, not just the ending. |
| 4 | Slow climb | arc `rags_to_riches` | mild | Underdog stories without a big setback. Most exposed to flagged labels (both fallbacks and collapsed W/M shapes land here), which the consumer rule excludes. |
| 5 | Monsters to beat | primary `overcoming_the_monster` | mild | Largest Booker bucket; spans horror, action and sports. |
| 6 | There and back again | primary `voyage_and_return` | mild | Clear shape that crosses kids' films, sci-fi and dramas. |
| 7 | The long road | primary `the_quest` | mild | Pairs naturally with Hero's Journey titles. |
| 8 | Second chances | primary `rebirth`, or `redemption_arc` present | mild (tag half: beat-tag group) | Emotionally distinct pull that genre browsing can't express. |
| 9 | Beautiful downfalls | primary `tragedy` | mild | For viewers who want weight; honest labeling of what they're getting. |
| 10 | The full hero's journey | blueprint `heros_journey` with 9 or more stages present | mild | Shows off the Mythic Blueprint layer. `mild` because the query uses late stages. |
| 11 | Stories that go dark | blueprint `anti_hero_descent` | mild | Prestige-TV heavy; strong for series-level TV. |
| 12 | Chosen families | `found_family` present | none | Very popular; cuts across genre. |
| 13 | Again and again | `time_loop` present | none | Small but beloved niche. |
| 14 | The big job | `heist_structure` present | none | Structure-first row that genre lists only half-cover. |
| 15 | Paths that cross | `ensemble_convergence` present, or chronology `nonlinear` | none | Represents the telling-order side of the Structural Skeleton. |

Re-check against the new rules:

- **No row uses a `major` term.**
- **Rows 5 to 7 changed from `none` to `mild`.** Membership reveals the `primary` plot, and `primary` is a single-choice field whose level is the highest in its vocabulary (`mild`, section 3 rule 3).
- **Row 8:** `redemption_arc` is a `mild` beat tag, hidden by default (decided 2026-09-29), so the default view of row 8 uses only primary `rebirth`. The tag half applies only for viewers who opt in.
- **Row 10 changed from `none` to `mild`**, because counting stage coverage uses late stages such as `resurrection` (rule 5).
- **Row 8:** `redemption_arc` on a villain reveals that the villain turns; Hari approved it at `mild`, so the row stands.
- **Rows left out:** rows built on `major` tags and on `twist_ending` stay out, since the row name would spoil every title in it.
- **New plots:** `rebellion_against_the_one` and `mystery` are not rows yet. Both are spoiler-safe and are natural candidates if the pilot shows enough titles.

---

## 16. Decisions and open questions

Decided by Hari on 2026-09-26, 2026-09-29 and 2026-09-30 (logged in `docs/DECISIONS.md`) and reflected above:

- **Spoiler visibility:** story shapes and the arc line are visible by default, and plot spoilers are hidden (section 3).
- **Spoiler levels:** the per-term levels are approved as drafted (section 3).
- **Display threshold:** 0.95 (2026-09-30, replacing 0.80), to be re-tuned after the pilot (section 4).
- **Journey stages:** Vogler's 12 stages (section 7).
- **Booker plots:** keep Rebellion Against "The One" and Mystery, for 9 plots (section 6).
- **Tones:** internal labels only; frontend proposes mood names in Phase 3 (section 5).
- **Attribution:** Wikipedia attribution on title pages (section 1).
- **Summary gate:** 150-word minimum (section 1).
- **Beat tags:** no new tags until after the gold set (section 9).
- **TV scope:** movies plus series-level TV, with ongoing series refreshed on summary change (sections 1, 10).
- **Gold protocol:** gold labelers use the same summary as the model (section 10).
- **Browse rows:** keep all 15 until the pilot (section 15).
- **TMDB interim rule:** Wikipedia plot sections are the only annotation input, and TMDB is used for IDs and display metadata only, until TMDB authorizes LLM use in writing (section 1).
- **Phase 1 exit gate (2026-09-30, replacing the 80% target of 2026-09-29):** A, `archetypal_plot.primary.label` exact match of at least 85%, or within 5 points of human–human agreement on at least 20 double-labeled titles (abstentions count as misses); B, at least 95% accuracy on labels at or above the 0.95 display threshold, with at least 30 such titles; coverage of at least 80 scored titles and 90% of the gold set. `emotional_arc` accuracy is reported separately, not gating (section 14).
- **Mild beat tags and tones hidden by default (2026-09-29):** story shapes, the arc line and journey stages stay visible (section 3).
- **Flat stories (2026-09-29):** a flagged net-change placeholder that is never shown or used in shape rows; a "Steady" shape is revisited after the pilot (section 8).

Still **[OPEN] — Hari owns:**

- **TMDB written authorization for LLM use.** Until TMDB authorizes it in writing, no TMDB text is sent to Claude or embedded; the schema and `require_wikipedia_sources()` enforce this, so it does not block 1.0.0. If TMDB restricts use more broadly, `tmdb_id` (currently required) may need to give way to another primary identifier. Wikidata (CC0) carries identifiers for many films and series (including TMDB and IMDb ids) and could supply cross-IDs, though its coverage of newer or niche titles needs checking. Moving off `tmdb_id` would be a schema change coordinated with backend. Display metadata (posters, runtimes) would then need another licensed source; that question sits with PLAN §5.1, not this schema.

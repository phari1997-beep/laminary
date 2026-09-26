# Narrative taxonomy and annotation schema

**Status: DRAFT v0.1.0, awaiting Hari's approval.** Owner: data-pipeline. Once approved, the schema becomes v1.0.0.

- Machine contract: `pipeline/schema/annotation.schema.json` (JSON Schema draft 2020-12)
- Illustrative examples: `pipeline/schema/examples/`
- Consistency test: `pipeline/tests/test_schema.py` checks that every vocabulary table in this document matches the schema's enums and spoiler levels exactly. Edit both together.

This document is the single source of definitions for three things: the annotation prompt, the gold-set labeling guide, and the frontend's display rules. Definitions are written so that a model or a person reading only a plot summary can apply them the same way.

---

## 1. Ground rules

1. **Licensed summaries only.** Input is Wikipedia plot sections (CC BY-SA) and TMDB overviews. Never scripts, subtitles, or book texts. The summary text is not stored in the annotation record; the record stores a reference, revision and hash of each source (section 10).
2. **No guessing from a title.** Annotate only what the supplied summary supports. Do not use outside knowledge of the title to fill gaps. If the summary is too thin to support the four layers, the correct output is `outcome: "abstained"`, not a low-confidence guess. The pipeline also skips titles below a word-count gate before calling the model (proposed 150 words total; open question 6).
3. **Own words.** Every free-text field is written fresh. Never copy or closely paraphrase sentences from the source. (Also avoids share-alike complications with Wikipedia text; see open question 5.)
4. **Describe, don't grade.** Text explains the story's shape. No quality judgments ("brilliant", "weak third act").
5. **Scope.** One record per movie or per whole TV series (all aired seasons together). No episode-level or season-level records in v1.

---

## 2. Record at a glance

```
schema_version        "0.1.0"
record_kind           llm_annotation | gold_label | illustrative_example
title                 media_type, name, release_year, tmdb_id, wikidata_id, series_status (TV only)
provenance            annotated_at, annotator{...}, sources[...], input_word_count, usage{...}, notes
outcome               annotated | abstained
abstain_reason        (only when abstained)
layers                (only when annotated)
  surface_story         setting_period, tones[], protagonist_structure, safe_text{...}, spoiler_text{...}
  archetypal_plot       primary{label,confidence}, secondary{plot: confidence}, safe_text, spoiler_text
  mythic_blueprint      blueprint{label,confidence}, stages_present{stage: confidence}, safe_text, spoiler_text
  structural_skeleton   emotional_arc{label,confidence}, arc_points[11], chronology, safe_text, spoiler_text
beat_tags             (only when annotated) tags{tag: confidence}, spoiler_text{evidence{tag: text}}
```

Every object is closed (`additionalProperties: false`), so a typo'd or invented field fails validation.

**Who fills what.** The model produces `outcome`, `abstain_reason`, `layers` and `beat_tags`. The pipeline fills `schema_version`, `record_kind`, `title` and `provenance`; the model never writes provenance.

---

## 3. Spoiler handling

Promise 3 in VISION.md: spoilers are hidden unless the viewer opts in. The schema enforces this in two ways.

**Free text is structurally split.** Every block that carries text has two sibling objects:

- `safe_text`: always displayable. Must stay at spoiler level `none`.
- `spoiler_text`: hidden by default. May reveal anything.

A frontend that drops every `spoiler_text` key, at any depth, is guaranteed to show no spoiler text. No free-text field exists outside these two objects.

**Controlled labels carry a fixed spoiler level.** Some labels reveal the ending by their nature (a Tragedy label says it ends badly). Each term's level is fixed in the vocabulary tables below and machine-readable in the schema under `x-laminary-spoiler-levels`. The arc line (`arc_points`) is level `mild`, since its end point shows whether things end well.

<!-- vocab:spoiler_level -->
| Value | Name | Definition |
|---|---|---|
| `none` | Safe | Reveals only the premise: what a trailer, poster or the first quarter of the story would tell you. |
| `mild` | Shape | Reveals the overall direction or tone of the ending (up or down, twist or no twist) without saying what happens. |
| `major` | Spoiler | Reveals specific events after the setup: who dies, what the twist is, how the conflict resolves. |

Rule for `safe_text`: if a sentence would only make sense to someone who has seen past the first quarter of the story, it belongs in `spoiler_text`.

Default visibility of `mild` labels (story shapes, the arc line) is **open question 1**: the product's core browse feature is built on them, so hiding them by default would hide the product.

---

## 4. Confidence

Every judgment that is scored in evaluation carries a `confidence` in [0, 1]: the primary plot, secondary plots, the blueprint, each Hero's Journey stage, the emotional arc, and each beat tag. Descriptive surface fields (setting period, tones, protagonist structure, chronology) carry none.

Confidence means "how likely is this label to match a careful human reading of the same summary." Bands, used in both the prompt and the labeling guide:

| Range | Meaning |
|---|---|
| 0.90 to 1.00 | The summary states it directly, or it is unmistakable. |
| 0.70 to 0.89 | Strongly implied; a careful reader would agree. |
| 0.50 to 0.69 | Plausible; reasonable readers could disagree. |
| below 0.50 | Weak. |

Two kinds of judgment:

- **Single-choice labels** (`primary`, `blueprint`, `emotional_arc`): always present; pick the best fit. A confidence below 0.50 means "best available fit, and it is weak."
- **Presence sets** (`secondary`, `stages_present`, `beat_tags.tags`): include an item only if you judge it present (confidence 0.50 or higher). An item missing from the object is a judgment that it is absent. An empty object is valid.

Proposed display rule (tunable after the pilot, not a product decision): show a label on a title page or in a browse row only at confidence 0.70 or higher. Calibration is checked against the gold set: of labels given 0.8, about 80% should match the gold label.

---

## 5. Layer 1: Surface Story

What happens on the surface: who, where, when, and what they are up against.

| Field | Type | Meaning |
|---|---|---|
| `setting_period` | enum | When the story mainly takes place, relative to its release. |
| `tones` | 1 to 3 enums | The dominant feel while watching. Feeds the mood filter. |
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

## 6. Layer 2: Archetypal Plot (Booker's seven basic plots)

Which of Christopher Booker's seven plots best describes the story's engine: what drives events from start to finish.

| Field | Meaning |
|---|---|
| `primary` | The single best-fitting plot, with confidence. Always present. |
| `secondary` | Up to two more plots that are clearly present as major threads. Must not repeat the primary. |
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

Common confusions:

- **Rags to Riches the plot vs Rags to Riches the arc.** The plot (here) is about a lowly person's growth and usually has a mid-story crisis, so its arc is often `cinderella`, not the `rags_to_riches` arc. The arc (Layer 4) is only the shape of fortune over time: a steady rise with no major setback.
- **Tragedy vs a falling arc.** `tragedy` is about cause and ending: a self-made downfall. `riches_to_rags` or `icarus` is only the shape. A disaster film where good people suffer bad luck may have a falling arc without being a Tragedy.
- **Rebirth vs the `redemption_arc` beat tag.** Rebirth is the whole story's engine. A redemption arc tag can apply to any character, including a secondary one, inside any plot.
- **Quest vs Voyage and Return.** In a Quest the protagonist chooses a goal and the story ends on reaching it. In Voyage and Return the protagonist is thrown into the other world and the story ends on getting home.
- **Comedy vs playful tone.** A very funny film about defeating a villain is `overcoming_the_monster` with a `playful` tone.
- **Not in the list.** Booker's later additions (Rebellion Against "The One", Mystery) are excluded in v1 (open question 3). Stories that fit none of the seven still get the best fit with a low confidence.

---

## 7. Layer 3: Mythic Blueprint

Whether the story follows a mythic journey pattern, which one, and how many Hero's Journey stages it covers.

| Field | Meaning |
|---|---|
| `blueprint` | The best-fitting journey pattern, with confidence. Always present. |
| `stages_present` | Hero's Journey stages judged present, each with confidence. Recorded for every blueprint, including variants, so coverage is comparable across titles. |
| `safe_text.rationale` / `spoiler_text.rationale` | As in Layer 2. |

<!-- vocab:mythic_blueprint -->
| Value | Name | Spoiler | Definition |
|---|---|---|---|
| `heros_journey` | Hero's Journey | none | The protagonist leaves a familiar world, is tested in an unfamiliar one, survives a decisive ordeal largely through their own growth, and returns changed, bringing back something of value to others. |
| `heroines_journey` | Heroine's Journey | none | A journey whose strength comes through connection: the protagonist is cut off or descends, gathers or rebuilds a network of allies, and resolves the story by restoring or forming a community rather than through a solitary victory. Not about the protagonist's gender. |
| `anti_hero_descent` | Anti-hero descent | mild | A morally compromised protagonist's journey runs downward: each step takes them deeper into wrongdoing or self-destruction, and there is no return that benefits others. |
| `no_clear_blueprint` | No clear blueprint | none | None of the three patterns organizes the story (slice-of-life, pure procedurals, many ensemble dramas). Stages may still be listed if individually present. |

Confusions: an unlikable protagonist who ends up redeemed is not an `anti_hero_descent`; that is usually `heros_journey` or `rebirth` with a `redemption_arc` tag. A found-family story is not automatically a Heroine's Journey; it must also resolve through the network.

Stages follow Christopher Vogler's 12-stage film version of Campbell's monomyth (open question 2):

<!-- vocab:journey_stage -->
| Value | Name | Spoiler | Definition |
|---|---|---|---|
| `ordinary_world` | Ordinary World | none | The protagonist's normal life is shown before the adventure, establishing what they lack or want. |
| `call_to_adventure` | Call to Adventure | none | An event, message or challenge disrupts normal life and invites or forces a new course. |
| `refusal_of_the_call` | Refusal of the Call | none | The protagonist hesitates, resists or declines the call, at least at first. |
| `meeting_the_mentor` | Meeting the Mentor | none | A guide figure gives advice, training, a gift or confidence needed for the journey. |
| `crossing_the_threshold` | Crossing the Threshold | none | The protagonist commits and enters the unfamiliar world or situation; there is no easy way back. |
| `tests_allies_enemies` | Tests, Allies, Enemies | none | In the new world, the protagonist faces trials and learns who can be trusted. |
| `approach_to_inmost_cave` | Approach to the Inmost Cave | mild | Preparation for and movement toward the place or moment of greatest danger. |
| `ordeal` | Ordeal | mild | The central crisis: a confrontation with death, defeat or the protagonist's greatest fear. |
| `reward` | Reward | mild | Having survived the ordeal, the protagonist gains something: an object, knowledge, reconciliation or a new strength. |
| `road_back` | The Road Back | mild | The protagonist sets out to return or finish, often chased or facing consequences of the ordeal. |
| `resurrection` | Resurrection | mild | A final, climactic test where the protagonist is nearly destroyed and emerges transformed. |
| `return_with_elixir` | Return with the Elixir | mild | The protagonist comes home or to a new equilibrium bringing something that benefits others. |

A stage counts as present only if the summary shows an identifiable story event that performs its function. Ordeal vs Resurrection: the ordeal is the midpoint-to-late central crisis; resurrection is the final climactic test. If the summary shows only one such crisis near the end, mark `resurrection` and not `ordeal`.

---

## 8. Layer 4: Structural Skeleton

The shape of the story over time and how it is told.

| Field | Meaning |
|---|---|
| `emotional_arc` | Which of the six core emotional arcs best matches the protagonist's fortune over the story, with confidence. |
| `arc_points` | Exactly 11 numbers: the protagonist's fortune at t = 0.0, 0.1, ..., 1.0. Drawn as the story-shape arc line. Spoiler level `mild`. |
| `chronology` | How story events are ordered in the telling. |
| `safe_text.rationale` / `spoiler_text.rationale` | As in Layer 2. |

### Arc points

- **t** is position in the telling, from the first scene (0.0) to the last (1.0), in presentation order, not in-world chronology. For a TV series, t spans the whole aired run. t is implicit: the i-th value is at t = i/10.
- **Fortune** is the protagonist's overall situation and well-being as the story presents it: safety, status, relationships, hope. Range -1 (the worst state the story puts them in) to +1 (the best). 0 is neither good nor bad. For `ensemble` stories use the central group's collective fortune.
- Values need not start at 0. Use one or two decimal places.
- For `ongoing` series, the arc covers the aired run as of the source revision.

### Emotional arcs

The six core arcs (Reagan et al., 2016, after Vonnegut). They are defined by the direction of the **major moves** in fortune. A major move is a rise or fall of at least 0.3 from the most recent peak or trough; smaller wobbles are ignored. The label must agree with `arc_points` under this rule; the pipeline checks it (section 11).

<!-- vocab:emotional_arc -->
| Value | Name | Spoiler | Definition |
|---|---|---|---|
| `rags_to_riches` | Rags to Riches (arc) | mild | One major move: a sustained rise. Fortune ends clearly higher than it starts, with no major setback along the way. |
| `riches_to_rags` | Riches to Rags | mild | One major move: a sustained fall. Fortune ends clearly lower than it starts, with no major recovery. |
| `man_in_a_hole` | Man in a Hole | mild | Two major moves: fall, then rise. Things go wrong, then recover, usually above where they fell from. |
| `icarus` | Icarus | mild | Two major moves: rise, then fall. Success builds and then collapses. |
| `cinderella` | Cinderella | mild | Three major moves: rise, fall, rise. Early gains are lost in a crisis, then regained, usually for good. |
| `oedipus` | Oedipus | mild | Three major moves: fall, rise, fall. An early blow, a recovery, then a final collapse. |

Confusions:

- **Rags to Riches arc vs Booker plot:** see Layer 2. The arc is a steady rise only.
- **Man in a Hole vs Cinderella:** Cinderella has a distinct rise *before* the fall. If the story opens stable and things go wrong early, it is Man in a Hole.
- **Four or more major moves** (common in long series): choose the arc matching the largest-scale shape and lower the confidence. Smooth the `arc_points` so they express that dominant shape only if the summary supports it; do not invent moves to fit a label.

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

Recurring story beats that cut across the four layers. `beat_tags.tags` holds the tags judged present with confidence; `beat_tags.spoiler_text.evidence` optionally holds one sentence of evidence per present tag (keys must be a subset of `tags`). Evidence is always spoiler text, even for `none`-level tags.

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

- **Twist ending vs unreliable narrator:** an unreliable narrator often produces a twist; tag both when both apply. A twist that doesn't come from a narrator's distortion is `twist_ending` only.
- **Ambiguous ending vs twist ending:** a twist answers a question differently than expected; an ambiguous ending declines to answer.
- **Pyrrhic victory vs Tragedy:** in a pyrrhic victory the protagonist does win. In a Tragedy they are destroyed; both may apply.
- **Time loop vs nonlinear chronology:** a time loop is an in-world event; nonlinear is a way of telling.

---

## 10. Provenance, record kinds and abstaining

<!-- vocab:record_kind -->
| Value | Name | Definition |
|---|---|---|
| `llm_annotation` | LLM annotation | Produced by the annotation pipeline. Requires `annotator.model_version`, `prompt_version`, `run_id`, an integer `tmdb_id`, and at least one source. |
| `gold_label` | Gold label | Hand-labeled for evaluation. Requires `annotator.labeler_id`, `guide_version`, an integer `tmdb_id`, and at least one source (the summary the labeler was shown). |
| `illustrative_example` | Illustrative example | Hand-written for documentation and tests. Never loaded into the product. Requires `annotator.labeler_id`. |

<!-- vocab:media_type -->
| Value | Name | Definition |
|---|---|---|
| `movie` | Movie | A feature film. `tmdb_id` is in TMDB's movie namespace. |
| `tv_series` | TV series | A whole series, annotated at series level. `tmdb_id` is in TMDB's tv namespace. `series_status` is required. |

<!-- vocab:series_status -->
| Value | Name | Definition |
|---|---|---|
| `ended` | Ended | No further seasons are expected. |
| `ongoing` | Ongoing | More seasons may come; the annotation covers the aired run as of the source revision and should be re-run when the summary changes. |

Each entry in `provenance.sources` records `kind`, `ref` (Wikipedia URL or `tmdb:movie/<id>`), `revision` (Wikipedia revision id or TMDB retrieval date), `retrieved_at`, `license`, `word_count` and `content_sha256` of the exact text sent to the model. With `model_version`, `prompt_version` and the hash, any record can be reproduced or detected as stale.

<!-- vocab:source_kind -->
| Value | Name | Definition |
|---|---|---|
| `wikipedia_plot` | Wikipedia plot section | The "Plot" (or "Synopsis" / "Premise") section of the English Wikipedia article, as plain text. CC BY-SA. |
| `tmdb_overview` | TMDB overview | The `overview` field from TMDB's API for the title. |

<!-- vocab:source_license -->
| Value | Name | Definition |
|---|---|---|
| `CC-BY-SA-4.0` | CC BY-SA 4.0 | Wikipedia text from revisions under CC BY-SA 4.0 (current). |
| `CC-BY-SA-3.0` | CC BY-SA 3.0 | Wikipedia text from older revisions under CC BY-SA 3.0. |
| `TMDB-API-terms` | TMDB API terms | TMDB content, used under the TMDB API terms (attribution required; commercial agreement pending, PLAN §8). |

`provenance.usage` records token counts per title (and whether it went through the Batch API) so cost per title can be computed after every run. `provenance.notes` is internal only.

<!-- vocab:outcome -->
| Value | Name | Definition |
|---|---|---|
| `annotated` | Annotated | The summary supported a full annotation. `layers` and `beat_tags` are required. |
| `abstained` | Abstained | The annotator declined. `abstain_reason` is required and no layers or tags are present. Abstaining is correct behavior, not a failure. |

<!-- vocab:abstain_reason -->
| Value | Name | Definition |
|---|---|---|
| `summary_too_thin` | Summary too thin | The summary covers only the premise, or too little of the story to judge arc, plot and ending. |
| `summary_contradictory` | Summary contradictory | Sources disagree on major events, or the summary is internally inconsistent. |
| `not_a_narrative` | Not a narrative | The title has no story to annotate (concert film, stand-up special, most documentaries, reality or competition TV). |
| `summary_title_mismatch` | Summary/title mismatch | The summary appears to describe a different work (a remake, a namesake, the source novel). |

---

## 11. Checks beyond JSON Schema

JSON Schema covers shape, enums and ranges. These semantic checks run in the pipeline validator (Phase 1) and, for the examples, in `pipeline/tests/test_schema.py`:

1. `archetypal_plot.secondary` does not contain `primary.label`.
2. `beat_tags.spoiler_text.evidence` keys are a subset of `beat_tags.tags` keys.
3. `emotional_arc.label` agrees with `arc_points` under the major-move rule (moves of 0.3 or more). In production a mismatch flags the record for review or retry rather than silently passing.
4. `tmdb_id` exists in TMDB and its title and year match (ingestion time).
5. For LLM records: `input_word_count` equals the sum of `sources[].word_count` and is at or above the gate.

---

## 12. Versioning

`schema_version` is semantic versioning, fixed by `const` in the schema so a record can't claim a version it doesn't conform to.

- **Major** (1.0.0 → 2.0.0): a field or term is removed, renamed or redefined. Old records must be migrated or re-annotated. Backend is told first; they own the migration.
- **Minor** (1.0.0 → 1.1.0): a new optional field or a new vocabulary term. Old records still validate, but they were never asked about the new term, so evaluation and browse rows must treat them as "not assessed," not "absent," until they are re-run.
- **Patch** (1.0.0 → 1.0.1): wording in this document only, with no change to what a label means.

Separately, `prompt_version` changes whenever the annotation prompt text changes (including definition wording pulled from this document), and `model_version` records the exact model id. Evaluation results are always reported per (schema_version, prompt_version, model_version). v0.1.0 is the draft; approval makes it 1.0.0.

---

## 13. Proposed top-level browse rows **[OPEN] — Hari decides**

Candidates for the 10 to 15 rows in "Browse by Story Shape" (PLAN §3, §4). Row names are working titles in the VISION voice; final copy belongs to frontend and Hari. Each row is a query over labels at confidence 0.70 or higher. The spoiler column is the highest level the row's name reveals about every title in it.

| # | Working row name | Query | Spoiler | Rationale |
|---|---|---|---|---|
| 1 | Down and back up | arc `man_in_a_hole` | mild | The most common satisfying shape; big, varied row that anchors the concept. |
| 2 | Rise and fall | arc `icarus` | mild | Instantly understood; covers crime sagas, biopics and cautionary tales across genres. |
| 3 | Lost it all, won it back | arc `cinderella` | mild | Distinct from row 1 (the early rise), so it teaches that shape matters, not just the ending. |
| 4 | Slow climb | arc `rags_to_riches` | mild | Underdog stories without a big setback; good for comfort viewing. |
| 5 | Monsters to beat | Booker `overcoming_the_monster` | none | Largest Booker bucket; spans horror, action and sports. Spoiler-safe. |
| 6 | There and back again | Booker `voyage_and_return` | none | Clear, spoiler-safe shape that crosses kids' films, sci-fi and dramas. |
| 7 | The long road | Booker `the_quest` | none | Spoiler-safe; pairs naturally with Hero's Journey titles. |
| 8 | Second chances | Booker `rebirth` or tag `redemption_arc` | mild | Emotionally distinct pull that genre browsing can't express. |
| 9 | Beautiful downfalls | Booker `tragedy` | mild | For viewers who want weight; honest labeling of what they're getting. |
| 10 | The full hero's journey | blueprint `heros_journey` with 9+ stages present | none | Shows off the Mythic Blueprint layer; stage coverage makes it more than a genre. |
| 11 | Stories that go dark | blueprint `anti_hero_descent` | mild | Prestige-TV heavy; strong for series-level TV. |
| 12 | Chosen families | tag `found_family` | none | Spoiler-safe and very popular; cuts across genre. |
| 13 | Again and again | tag `time_loop` | none | Small but beloved niche; very "a little nerdy about stories." |
| 14 | The big job | tag `heist_structure` | none | Structure-first row that genre lists only half-cover. |
| 15 | Paths that cross | tag `ensemble_convergence` or `chronology` `nonlinear` | none | Represents the Structural Skeleton's telling-order side. |

Deliberately left out: rows built on `major` tags (`mentor_dies`, `unreliable_narrator`, `pyrrhic_victory`) and `twist_ending`, because a row name like "Twist endings" spoils every title in it. `ambiguous_ending` and `oedipus` / `riches_to_rags` are reasonable reserves if pilot counts show enough titles. Row sizes can't be known until the 500-title pilot; any row with fewer than about 20 available titles in a user's services should probably be hidden.

---

## 14. Open questions for Hari

1. **Are story shapes spoilers?** Arc labels, the arc line, Tragedy, Rebirth and a few tags are `mild`: they reveal the direction of the ending but no events. Recommendation: show `none` and `mild` by default (the product is built on shape), hide `major` labels and all `spoiler_text` until the viewer opts in. Alternative: hide `mild` too, which would hide browse rows 1 to 4, 8, 9 and 11 and the arc line by default.
2. **Hero's Journey stage model.** Proposed: Vogler's 12 stages (film-oriented, easier to label from summaries) rather than Campbell's 17. Heroine's Journey is defined operationally (strength through connection and community) rather than by Murdock's 10 stages, which are hard to judge from plot summaries. Approve both?
3. **Booker's two later plots** (Rebellion Against "The One", Mystery): leave out of v1? Recommendation: leave out; add in a minor version if gold-set labelers keep reaching for them.
4. **Tone vocabulary** (10 terms, section 5) will drive the Phase 3 mood filter. Approve the list, or should frontend propose mood names first?
5. **LLM-written spoiler text derived from Wikipedia plots.** Resolutions and rationales are written in the model's own words, but they are derived from CC BY-SA text. Do we treat displayed analysis as needing Wikipedia attribution on the title page (frontend already owns Wikipedia attribution, PLAN §8)? This is a legal-terms question.
6. **Minimum summary length.** Proposed gate: at least 150 words of summary in total. TMDB overviews alone rarely reach this, so in practice most annotated titles will need a Wikipedia plot section. This trades catalog coverage for accuracy; titles below the gate get no narrative data and can't appear in shape rows.
7. **Beat tag list is thin.** Ten tags (from PLAN §4) is a small base for "more like this" and one-line whys. OK to propose 10 to 20 more after the gold set shows which are reliable, as a minor version?
8. **Ongoing TV series.** Proposed: annotate the aired run and re-annotate when the summary changes (the content hash detects it). Their arc may change as seasons air. Acceptable, or exclude ongoing series from shape rows?
9. **Gold-set instructions.** Should labelers label from the same summary the model sees (measures annotation accuracy against the text) or from their memory of watching (measures truth, but penalizes the model for gaps in the summary)? Recommendation: label from the summary, and let labelers flag where the summary itself is misleading.

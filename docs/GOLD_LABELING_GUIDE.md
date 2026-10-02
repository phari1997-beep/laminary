# Gold labeling guide

**Version 1.5.0** (this number goes in the sheet's `guide_version` column). Owner: data-pipeline. Definitions come from `docs/NARRATIVE_SCHEMA.md` v1.2.0; if the two ever disagree, the schema wins and this guide gets fixed.

## What this is for

Laminary uses an AI model to read each film's or series' plot summary and label its story shape. To know whether the model is any good, we need an answer key: about 100 titles labeled carefully by people. That's you.

Your labels are compared with the model's. The main score is simple: does the model pick the same **primary plot** as you? Phase 1 passes a two-part check (DECISIONS 2026-09-30, replacing the 80% target of 2026-09-29):

- **Overall:** the model matches your primary plot on at least 85% of titles. If two people label at least 20 of the same titles, it is enough for the model to come within 5 points of how often those two people agree with each other.
- **Labels the app would show:** when the model is at least 95% confident in a primary plot (the confidence it needs for the app to show it), it must match you at least 95% of the time, measured on at least 30 such titles.

The check also needs at least 80 titles scored, and at least 90% of your labeled titles. Everything else you fill in is measured too, but only the primary plot decides.

Labels are internal. Nobody sees them in the app, so spoilers are fine here.

## The golden rules

1. **Read only the summary file.** Each row names a text file (`summary_text_file`, for example `texts/Q83495.txt`). You'll find it in the **Laminary** folder on Google Drive that Hari shares with you, in the `texts` subfolder next to the gold sheet (DECISIONS 2026-09-30). Read that file and nothing else. It holds exactly the text the model reads: the Wikipedia plot section with tables, image captions, "Main article" notes and footnote markers removed (and, for series, without the episode tables). Some series have a thin main article, so their file joins several season articles in season order, up to about 3,000 words; if the first full season article (normally season 1) is longer on its own, the file holds it in full, up to about 6,000 words. Short season articles (under 500 words) before the first full season are kept in front of it: for example a 300-word season 1 and a 3,500-word season 2 are both in the file (DECISIONS 2026-10-01). If the full season is too long to go with them (over about 6,000 words together), the file holds the short seasons alone, so a series file may have only a few short seasons; label from those, or skip it under rule 4 if they don't support a label. Each season starts with a line like `<summary part="2" source="English Wikipedia plot section" article="Succession (season 2)">` and ends with `</summary>`: that is exactly how the model sees the seasons. Label the whole series from all the parts. For these titles `plot_section` says "Season articles", and `wikipedia_revision_link` and the `source_` columns list one value per article, separated by `|`. **Episode summaries (guide 1.5.0):** for a few big series with no usable season prose, the file is instead the short per-episode summaries from Wikipedia's episode tables, one paragraph per episode in season and episode order, each starting with a marker like `S2E5 "The Robbery":` (season 2, episode 5, and its title). Each season is one part, with the same `<summary ...>` opening line; the parts may all name the same "List of ... episodes" article. Air dates, ratings, writers and directors are never in the file. These files stop at about 3,000 words, so the last season may stop part-way (rule 7). For these titles `plot_section` says "Episode tables (N seasons)". Label the whole series from the episodes you can read, exactly as for any other series file. Don't label from the Wikipedia page itself; it shows things the model never sees. The `wikipedia_revision_link` column is there only to credit the source (the text is CC BY-SA). We're measuring the model, not the summary (DECISIONS 2026-09-26).
2. **Forget what you know.** If you've seen the film and the summary leaves something out, label what the summary says. Don't fill gaps from memory, reviews, or other pages.
3. **Judge every box.** Every Y/N column needs a Y or an N. N is a real answer ("I looked, it isn't there"), not a blank.
4. **If you can't label it, skip it** with a `skip_reason` (below) rather than guessing.
5. **One row per title per person.** Don't copy someone else's row. About 25 titles have two rows (`label_slot` 1 and 2) so two people can label them; each person takes the slot Hari assigns.
6. **Label independently; don't confer.** Don't discuss a title with other labelers, and don't look at anyone else's row, until Hari says labeling is finished. The two-person titles measure how often careful people agree on their own; comparing notes makes that number meaningless. Where the two of you disagree, Hari settles it afterwards.
7. **Partial series: label only what the summary covers.** Some series files cover only some seasons, because the rest were left out to keep the file short or have no usable article. Then the grey `summary_coverage` column says so, for example "Summary covers seasons 1–4 of 7." or "Summary covers seasons 3–6 of 6."; the model gets exactly the same line (DECISIONS 2026-10-02). The missing seasons may be at the start, in the middle or at the end. For an episode-summary file the line can also say that the last season stops part-way, for example "Summary covers seasons 1–3 of 9 (season 3 only in part)."; then season 3's later episodes are missing too, so you can't see how that season ends either. Label only the covered seasons, with the arc running from the first covered season to the last. Don't guess what happens in seasons you can't see: not how the story began, not what happened in a gap, and not the ending if the last season isn't covered. An ending-dependent label (Tragedy, the last journey stages, the ending tags, the end of the arc) needs the text itself to show that ending. If what's left can't support a label, skip it under rule 4 with "Summary too thin". An empty `summary_coverage` means the file is not known to be partial.

Expect about 15 to 25 minutes a title once you're used to it.

## Filling in one row

Grey columns are filled in for you; don't edit them. White columns are yours, left to right:

| Step | Columns | What to enter |
|---|---|---|
| 1 | `labeler_id` | Your code from Hari (for example `L03`). Not your name. |
| 2 | `skip_reason` | Leave blank, unless you can't label this title (see "Skipping a title"). |
| 3 | `primary_plot` and the 9 `plot_` columns | The one plot that best describes the story's engine, then Y/N for each plot. |
| 4 | `blueprint` and the 12 `stage_` columns | The journey pattern, then Y/N for each Hero's Journey stage. |
| 5 | `arc_shape` (or the 11 `arc_t` columns) | The story's shape of fortune over time. |
| 6 | the 10 `tag_` columns | Y/N for each story beat. |
| 7 | `confidence` | How sure you are of the primary plot. |
| 8 | `notes` | Optional. Anything odd about the summary or your call. |

### Step 3: Plot (Booker's plots)

Pick the **primary plot**: the one plot that best describes what drives events from start to finish. Then go through all nine plot columns and mark each **Y** if it's a major thread of this story, **N** if not.

- The primary plot must be **Y**.
- At most **two** other plots can be Y. Most stories have zero or one.

| Plot | Mark it when... |
|---|---|
| Overcoming the Monster | The hero confronts a powerful threatening force (a creature, person, group or system) that endangers them or their community, and the story is driven by that threat and the fight against it. |
| Rags to Riches | A lowly or overlooked person gains status, love or self-worth, loses it or hits a crisis, then earns a fuller, lasting fulfillment. The engine is their growth into who they could be. |
| The Quest | The hero, usually with companions, sets out on purpose toward a distant goal (a place, object or person) and overcomes a series of obstacles to reach it. The destination drives the story. |
| Voyage and Return | The hero is taken into, or falls into, an unfamiliar world, is fascinated and then threatened by it, and makes it back home changed. The round trip is the point. |
| Comedy | Confusion, misunderstanding, disguise or social obstacles keep people apart; the story ends when the confusion clears and they're united or reconciled. **This is not the same as "funny".** |
| Tragedy | A flaw, ambition or wrongdoing draws the hero down a path that ends in their destruction or death. Only when the downfall is the ending. |
| Rebirth | The hero falls under a dark power or deadened state (a curse, bitterness, numbness) and is freed from it by another person or by a transforming realization. The release is the story. |
| Rebellion Against the One | The hero defies an all-powerful authority or system that rules their whole world (a state, an institution, a controlling order), through to escape, overthrow, or the rebel's defeat. |
| Mystery | Someone, often an outsider like a detective, reporter or curious bystander, investigates a puzzling event, usually a crime. The investigation and uncovering the truth drive the story. |

Easy mix-ups:

- **Funny isn't Comedy.** A hilarious film about beating a villain is Overcoming the Monster. Comedy means confusion keeps people apart until it's cleared up.
- **Quest vs Voyage and Return.** Quest: the hero chooses a goal and the story ends on reaching it. Voyage and Return: the hero is thrown into another world and the story ends on getting home.
- **Tragedy vs things just going badly.** Tragedy is a self-made downfall that ends the story. Good people suffering bad luck isn't Tragedy.
- **Rebirth vs someone turning good.** Rebirth is the whole story's engine. A side character who redeems themselves is the "Redemption arc" tag, not Rebirth.
- **Rebellion vs Monster.** The Monster invades or threatens the hero's world from outside. "The One" *is* the order of the hero's world. If both apply, pick as primary whichever the story spends more time on. A hero whom the story calls "the One" (as in The Matrix) says nothing about this plot.
- **Mystery vs a twist.** A late surprise doesn't make a Mystery. Someone's investigation has to drive the plot.
- **Mystery vs Quest.** A search for a missing person is a Quest if finding where they are drives a journey, and a Mystery if working out what happened to them is the point.
- **Mystery vs Monster.** A detective hunting a killer: Mystery if the story is organized around working out what happened; Overcoming the Monster if it's organized around stopping a known threat.
- **Rags to Riches the plot vs the arc.** The plot is about a lowly person's growth and usually has a mid-story crisis. The arc of the same name (step 5) is only a steady rise.

### Step 4: Blueprint and journey stages

Pick the **blueprint**, the journey pattern that best organizes the story:

| Blueprint | Choose it when... |
|---|---|
| Hero's Journey | The hero leaves a familiar world, is tested in an unfamiliar one, survives a decisive ordeal largely through their own growth, and returns changed, bringing back something of value to others. |
| Heroine's Journey | The strength comes through connection: the hero is cut off or brought low, gathers or rebuilds a network of allies, and resolves the story by restoring or forming a community rather than by a solo victory. **Not about the hero's gender.** |
| Anti-hero descent | A morally compromised hero's journey runs downward: each step takes them deeper into wrongdoing or self-destruction, and there's no return that benefits others. |
| No clear blueprint | None of the three organizes the story (slice-of-life, pure procedurals, many ensemble dramas). You still judge the stages below. |

Mix-ups: an unlikable hero who ends up redeemed is **not** an Anti-hero descent (that's usually Hero's Journey or Rebirth, with the Redemption arc tag). A found-family story isn't automatically a Heroine's Journey; it also has to be resolved through the group.

Then mark each of the 12 stages **Y** or **N**, whatever blueprint you chose. A stage is Y only if the summary shows an actual event that does its job.

| Stage | Y when the summary shows... |
|---|---|
| Ordinary World | The hero's normal life before the adventure, showing what they lack or want. |
| Call to Adventure | An event, message or challenge that disrupts normal life and invites or forces a new course. |
| Refusal of the Call | The hero hesitating, resisting or saying no, at least at first. |
| Meeting with the Mentor | A guide figure giving advice, training, a gift or the confidence needed. |
| Crossing the First Threshold | The hero committing and entering the unfamiliar world or situation, with no easy way back. |
| Tests, Allies, Enemies | Trials in the new world, and the hero learning who to trust. |
| Approach to the Inmost Cave | Preparation for, and movement toward, the place or moment of greatest danger. |
| Ordeal | The central crisis: facing death, defeat or their greatest fear. |
| Reward | Having survived the ordeal, the hero gains something: an object, knowledge, a reconciliation or new strength. |
| The Road Back | The hero setting out to return or finish, often chased or facing the ordeal's consequences. |
| Resurrection | A final, climactic test where the hero is nearly destroyed and comes out transformed. |
| Return with the Elixir | The hero coming home, or reaching a new balance, bringing something that benefits others. |

**Ordeal vs Resurrection:** the Ordeal is the central crisis around the middle or later; Resurrection is the final climactic test. If the summary only shows **one** big crisis, near the end, mark Resurrection **Y** and Ordeal **N**.

### Step 5: Emotional arc (story shape)

Think of the main character's **fortune** over the story, from the first scene to the last, in the order the story tells it: their actual situation (safety, status, relationships, prospects), from the worst the story puts them in (-1) to the best (+1).

- **Situation, not mood.** Someone trapped in a time loop who spends a stretch enjoying themselves is still trapped, so their fortune stays low. Only a real change in their situation moves it.
- For an ensemble, use the central group's shared fortune. For a series, the whole run so far.

A **big move** is a rise or fall of at least 0.3 on that -1 to +1 scale (about a sixth of the full range) from the last high or low point. Ignore small wobbles. Count the big moves and pick the shape:

| Shape (`arc_shape`) | Big moves |
|---|---|
| Rags to Riches (steady rise) | One: a sustained rise. |
| Riches to Rags (steady fall) | One: a sustained fall. |
| Man in a Hole (fall then rise) | Two: things go wrong, then recover. |
| Icarus (rise then fall) | Two: success builds, then collapses. |
| Cinderella (rise, fall, rise) | Three: early gains are lost in a crisis, then regained. |
| Oedipus (fall, rise, fall) | Three: an early blow, a recovery, then a final collapse. |

**Man in a Hole vs Cinderella:** Cinderella has a clear rise *before* the fall. If the story opens stable and things go wrong early, it's Man in a Hole.

**More than three big moves** (common in long series): step back and look at the biggest swings only, until three or fewer remain.

**Flat stories (the fallback rule).** If the story has **no big move at all** (gentle slice-of-life, a steady situation), don't force a shape. Choose one of the two flat options (DECISIONS 2026-09-29):

- **"Flat: ends better than it starts"** if the ending is at least a little better off than the opening;
- **"Flat: ends the same or worse"** otherwise.

These are stored as a flagged placeholder, never shown in the app or used for story-shape rows. If a "Steady" shape is added after the pilot, these are the titles we'll relabel.

**Optional: the 11 points.** Instead of choosing `arc_shape`, you can fill all eleven `arc_t` columns with the fortune at 0%, 10%, 20% ... 100% of the story (numbers from -1 to 1, like `-0.4` or `0.25`). The importer then works out the shape with the same rule the model uses. Fill all eleven or none. If you fill both the points and `arc_shape`, your `arc_shape` wins, and the importer tells you if the points trace a different shape.

### Step 6: Beat tags

Mark each tag **Y** or **N**:

| Tag | Y when... |
|---|---|
| Mentor dies | Someone who guides, trains or protects the hero dies during the story, and the loss matters to the hero's path. |
| Twist ending | A late revelation substantially changes the meaning of what came before. Not just a surprise event. |
| Unreliable narrator | The story is told through a narrator or point-of-view character whose account turns out to be false, distorted or incomplete in a way that matters. |
| Found family | Unrelated characters form bonds of loyalty and care that work like a family, and that bond is central. |
| Redemption arc | A character who has done serious wrong makes a meaningful turn toward atonement or goodness. Any major character, not just the hero. |
| Pyrrhic victory | The hero wins the central conflict at a cost so high it undercuts or outweighs the win. |
| Time loop | One or more characters relive the same stretch of time repeatedly, keeping their memories. |
| Heist structure | The story is organized around planning and carrying out a theft or elaborate con by a team. |
| Ensemble convergence | Several storylines that start separately come together, and where they meet is the payoff. |
| Ambiguous ending | The ending deliberately leaves a central question open or open to competing readings. |

Mix-ups: an unreliable narrator often produces a twist; mark both when both apply. A twist answers a question in an unexpected way; an ambiguous ending declines to answer. In a Pyrrhic victory the hero does win (a Tragedy can also apply). A time loop happens inside the story; telling a story out of order is not a time loop.

### Step 7: Confidence

How sure are you of your **primary plot**?

| Choose | Meaning |
|---|---|
| High | The summary states it directly, or it's unmistakable. |
| Medium | Strongly implied; a careful reader would agree. |
| Low | Plausible; reasonable readers could disagree. |
| Weak | Best available fit, and it's weak. |

Be honest; "Low" is useful information. We use it to check whether the model's confidence means anything.

## Skipping a title

Choose a `skip_reason` and leave the label columns blank when:

| skip_reason | When |
|---|---|
| Summary too thin | It passes the length check but only covers the premise, or too little of the story to judge the plot, arc and ending. |
| Summary contradictory | The summary contradicts itself on major events. |
| Not a narrative | There's no story to label (a concert film, stand-up, most documentaries, reality TV). |
| Summary is about a different work | The summary seems to describe a different film or show (a remake, a namesake, the source novel). |

Skipping is a correct answer, not a failure. We compare it with whether the model also declines.

## Common mistakes the importer will flag

The importer checks every row and names the column. The usual ones:

- A blank Y/N box ("enter Y or N in every column").
- The primary plot isn't marked Y in its own plot column.
- More than two plots besides the primary marked Y.
- A value typed that isn't in the dropdown (use the dropdowns).
- Some but not all of the 11 arc points filled, or a point outside -1 to 1.
- `labeler_id` left blank, or the same person labeling the same title twice (both slots of a two-person title must be different people).
- A slot-2 row filled in while that title's slot-1 row is blank or has a problem. The slot-2 label waits until slot 1 imports, because the model is scored against slot 1.
- Edited grey columns (the importer needs them exactly as they were).

## For Hari: setting up the sheet

1. The coordinator runs `python -m laminary_pipeline.gold select` and `... gold template`. That writes three CSVs in `pipeline/data/gold/` and the `texts` folder: one `<QID>.txt` per title (the exact model input) plus `manifest.csv` (filename, QID, title, SHA-256, word count). Upload the three CSVs and the whole `texts` folder to the Laminary folder on your Drive, and share it with labelers as view-only. The manifest lets anyone check that a file wasn't changed: its SHA-256 must equal the sheet's `source_sha256` (for a title joined from season articles, the manifest's SHA-256 is of the joined file, and `source_sha256` lists each article's hash). Nothing is uploaded automatically.
2. In Google Sheets, import `gold_labels_template.csv` (File > Import > Replace spreadsheet), then import `gold_labels_lists.csv` and `gold_labels_readme.csv` with "Insert new sheet(s)". Rename the tabs Labels, Lists and README.
3. On the Labels tab: View > Freeze > 2 rows (header plus the `#` help row). Select the `plot_`, `stage_` and `tag_` columns and add Data > Data validation > "Dropdown (from a range)" = `Lists!A2:A3` (the `yes_no` column). Do the same for `primary_plot`, `blueprint`, `arc_shape`, `skip_reason` and `confidence`, each pointing at its column on the Lists tab. Set invalid data to "Reject input".
4. Shade the prefilled columns grey (`qid` to `summary_coverage`, including `label_slot`, and `series_status` to `guide_version`) and protect them (Data > Protect sheets and ranges > "Show a warning").
5. Give each labeler a code (`L01`, `L02`, ...) and assign rows. The selector marks 25 titles for double labeling (DECISIONS 2026-09-30), and the template already has two rows for each, `label_slot` 1 and 2. Assign the two slots of a title to different people. Slot 1 is the reference the model is scored against; slot 2 measures human agreement. Tell labelers to work independently (golden rule 6). The evaluation lists every title where the two disagree, for you to adjudicate.
6. When done: File > Download > CSV (Labels tab) and hand it to the coordinator, who runs `python -m laminary_pipeline.gold import <file>.csv`. Any problems come back as a list by row number and title.

## Versions

- **1.0.0** (2026-09-30): first version, for schema v1.0.0.
- **1.1.0** (2026-09-30): labelers read the exact model-input text file (`summary_text_file`), not the Wikipedia page.
- **1.2.0** (2026-10-01): two-part Phase 1 exit check; `label_slot` and double labeling of 25 titles; label independently (rule 6); text files in the Drive Laminary folder with a manifest; series joined from season articles. Label definitions unchanged.
- **1.3.0** (2026-10-01): rule 1 notes that a series file may hold only short season articles when the first full season is too long to join them (DECISIONS 2026-10-01). Label definitions unchanged.
- **1.4.0** (2026-10-02): new grey column `summary_coverage` and golden rule 7: when a series file covers only some seasons, label only the covered run and never judge an ending you can't see; the model gets the same line in its request (prompt annotate-1.2.0, DECISIONS 2026-10-02). Series files no longer include production, ratings or broadcast subsections, or "Cite error" messages. Label definitions unchanged.
- **1.5.0** (2026-10-02): a series file may be per-episode summaries from Wikipedia's episode tables (rule 1), and `summary_coverage` may say a season is included only in part (rule 7) (DECISIONS 2026-10-02: big series must not drop out). Label definitions unchanged.

Changing a definition here changes what a label means. That needs a new guide version, and labels made under the old version stay tagged with it.

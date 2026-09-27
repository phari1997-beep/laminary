# Laminary — Narrative-First Watch Recommender
### Project brief & execution plan (for Claude Cowork)

---

## 0. Instructions for Cowork

You are picking up an existing project. Read this whole file before doing anything.

- Owner: Hari. Decisions marked **[OPEN]** need Hari's sign-off before you act on them.
- Work phase by phase (Section 9). Don't start a phase until the previous phase's exit criteria are met.
- Keep a running `DECISIONS.md` (what was decided, when, why) and `PROGRESS.md` (checkbox status) in this folder.
- Prefer simple, cheap, managed services. This is a solo/small-team build.
- Never scrape sites whose terms forbid it. Never ingest full copyrighted texts (scripts, books, subtitles). Plot **summaries** and licensed metadata only.

---

## 1. The idea in one paragraph

A consumer app where you select the streaming services you actually pay for, and it recommends what to watch based on **story structure**, not just genre. Every title is analyzed through Laminary's four layers (Surface Story, Archetypal Plot, Mythic Blueprint, Structural Skeleton). Users can browse by narrative shape ("Hero's Journey," "Riches to Rags," "Voyage and Return," "mentor dies at the midpoint") across all their services at once, and open any title for an in-depth four-layer breakdown.

**Primary feature:** "What should I watch tonight, on services I already have?"
**Signature feature:** Browse and match by story structure.
**Depth feature:** Four-layer analysis per title.

---

## 2. Sanity check on this direction

### What's better about this than the original Laminary
- **Much bigger audience.** Hundreds of millions of streaming subscribers vs. tens of thousands of screenwriters.
- **Real, daily pain.** Choice paralysis across 4–6 services is a widely shared complaint.
- **Reuses the framework.** The four-layer analysis becomes the recommendation engine, not a niche writer tool.
- **Genuinely differentiated browse.** Nobody mainstream lets you browse by narrative structure.

### What needs correcting

| Assumption | Reality | Adjustment |
|---|---|---|
| "Train a model on every movie, TV show, and book" | Training a model is expensive, unnecessary, and would require full texts you can't legally use | Use an existing LLM to **annotate** licensed plot summaries into structured tags, then use **embeddings** for similarity. No training needed for MVP |
| Cross-platform availability is easy | This is the hardest data problem in the product. Availability changes daily, varies by country | License it (Section 5). Budget for it |
| Recommendations are the moat | JustWatch, Reelgood, and every streamer already recommend | The moat is the **narrative layer**. Lead with it in marketing |
| Books fit in v1 | Books have a totally separate availability world (Kindle Unlimited, Libby, Audible) | Movies + TV first. Books are Phase 4 |
| Four layers can be generated accurately for every title | LLMs will hallucinate structure for obscure titles with thin summaries | Only analyze titles with a solid source summary; show confidence; human-review a gold set |

### Competitors to study
- **JustWatch** — the category leader for "where to stream"; filters by service; monetizes via ads to studios/streamers.
- **Reelgood** — unified watchlist + service filtering.
- **Letterboxd** — social/logging; huge engaged film audience; not structure-aware.
- **Streamers' own recs** — only recommend within their own catalog.

**Positioning line to test:** *"Find stories shaped like the ones you love — on the services you already pay for."*

### Monetization reality
Consumer recommendation apps are hard to monetize with subscriptions. Realistic stack:
1. Free core (recs + structure browse) to grow users.
2. **Laminary Pro** (~$3–5/mo, [OPEN]): full four-layer breakdowns, unlimited "stories like X," taste profile, ad-free.
3. **Affiliate / subscription referral** revenue when users start a service trial through the app (verify each program's terms).
4. Later: aggregated, anonymized narrative-preference insights for studios (B2B). Only with clear consent and privacy policy.

---

## 3. Core user flows (MVP)

1. **Onboarding (< 60 sec)**
   - Pick country.
   - Pick services you have (Netflix, Max, Prime Video, Disney+, Hulu, Apple TV+, Peacock, Paramount+, etc.).
   - Pick 3–5 titles you loved (seeds for taste profile).
2. **Tonight** (home screen)
   - Ranked recommendations available on the user's services only.
   - Each card shows *why*: "Same arc as *Arrival*: Voyage and Return + grief-driven protagonist."
3. **Browse by Story Shape**
   - Rows by structure (Hero's Journey, Tragedy, Rebirth, Rags to Riches, Man in a Hole, etc.).
   - Filters: service, runtime, movie/TV, era, tone.
4. **Title page**
   - Where to watch (deep link out).
   - Four-layer breakdown (Surface summary free; deeper layers Pro, [OPEN]).
   - "More stories shaped like this."
5. **Feedback**
   - 👍/👎 and "watched" to refine the profile.

---

## 4. The narrative taxonomy (browse layer)

Build on established frameworks so the tags are defensible and explainable, then add Laminary's own layer on top.

**Structural shape (emotional arc)** — based on the six core arcs identified in computational story-arc research:
Rags to Riches · Riches to Rags · Man in a Hole · Icarus · Cinderella · Oedipus

**Plot archetype** — based on Christopher Booker's seven basic plots:
Overcoming the Monster · Rags to Riches · The Quest · Voyage and Return · Comedy · Tragedy · Rebirth

**Mythic blueprint** — Hero's Journey stage coverage (Call, Refusal, Mentor, Threshold, Ordeal, Return, etc.) scored per title, plus common variants (Heroine's Journey, anti-hero descent).

**Beat-level tags** (searchable, fun, shareable):
mentor dies · twist ending · unreliable narrator · found family · redemption arc · pyrrhic victory · time loop · heist structure · ensemble convergence · ambiguous ending

**[OPEN]** Final list of top-level browse rows for launch (recommend 10–15 max).

---

## 5. Data strategy

### 5.1 Title metadata
- **TMDB API** — titles, cast, genres, posters, IDs. Note: commercial use requires a commercial agreement with TMDB; confirm before launch. Attribution required.
- **Wikidata** — cross-IDs (IMDb, TMDB), structured facts. CC0.

### 5.2 Plot summaries (input to analysis)
- **Wikipedia plot sections** — CC BY-SA; fine to process; attribute if you display derived text. Covers most notable films and many series.
- TMDB overviews for short synopses.
- Skip titles with no substantial summary rather than guessing.

### 5.3 Streaming availability — [OPEN] vendor choice
Options to evaluate (pricing, country coverage, deep links, update frequency, commercial terms):
- **Watchmode API** — commercial availability API with paid tiers.
- **TMDB watch providers** — sourced from JustWatch, requires JustWatch attribution; check whether commercial use is permitted under your TMDB agreement.
- **JustWatch partnership** — not a public self-serve API; would require a business deal.
- **Streaming Availability API (via RapidAPI)** — another paid option.

Deliverable: a one-page comparison and recommendation before Phase 2.

### 5.4 Narrative annotation pipeline
```
summary text
   → LLM (structured JSON output, fixed schema, batch API for cost)
   → tags + four-layer analysis + confidence score
   → embedding of the analysis ("narrative fingerprint")
   → Postgres (pgvector)
```
- Use a fixed JSON schema; reject/re-run malformed outputs.
- Store model + prompt version on every record so you can re-run later.
- Cost: annotating tens of thousands of titles via a batch API should land in the low hundreds to low thousands of dollars total, depending on model and summary length. Measure on a 500-title pilot before scaling.

### 5.5 Quality control
- Hand-label a **gold set of ~100 well-known titles** (Hari + a couple of film-literate friends).
- Measure agreement between LLM tags and gold tags per category.
- Target ≥80% agreement on top-level archetype before launch. Iterate on prompts until met.

---

## 6. Recommendation engine (v1: simple, explainable)

```
score(title) =
    w1 * cosine(user_narrative_profile, title_fingerprint)
  + w2 * tag_overlap(user_liked_tags, title_tags)
  + w3 * quality_signal (ratings / popularity, normalized)
  - penalties (already watched, disliked shapes)
FILTER: available on user's services in user's country
```
- User profile = average of seed-title fingerprints, updated by 👍/👎.
- Every rec must be able to produce a one-line "why." Explainability is the product.
- No collaborative filtering until there are enough users to make it worthwhile.

---

## 7. Suggested tech stack (solo-friendly)

| Layer | Choice | Why |
|---|---|---|
| Mobile app | React Native + Expo | One codebase for iOS + Android; web preview |
| Backend + DB + auth | Supabase (Postgres + pgvector + auth) | Managed, cheap, vector search built in |
| Pipelines | Python scripts (run locally or as scheduled jobs) | Ingestion, annotation, availability sync |
| LLM | Claude via API (Batch for bulk annotation) | Structured output, cost-efficient bulk runs |
| Analytics | PostHog (free tier) | Funnels, retention |

### Core tables
- `titles` (id, tmdb_id, type, year, runtime, genres, poster, summary_source)
- `narrative_analysis` (title_id, layer1..layer4 JSON, tags[], confidence, model_version, prompt_version)
- `narrative_embeddings` (title_id, vector)
- `availability` (title_id, country, service, link, last_seen)
- `users`, `user_services`, `user_feedback`

---

## 8. Legal & risk checklist

- [ ] TMDB commercial terms confirmed
- [ ] Availability vendor contract reviewed
- [ ] Wikipedia CC BY-SA attribution handled where derived text is shown
- [ ] No full scripts, subtitles, or book texts ingested
- [ ] Posters/images used only under provider terms
- [ ] Privacy policy (viewing preferences are personal data)
- [ ] App Store / Play Store review requirements
- [ ] Trademark search on "Laminary"

**Top risks:** availability data cost/licensing; tag accuracy; low willingness to pay for consumer rec apps; streamers or JustWatch adding structure-based browsing.

---

## 9. Execution plan

### Phase 0 — Validate (2 weeks)
- [ ] One-page landing page with the positioning line + email signup
- [ ] Post "stories shaped like…" lists (e.g., "10 Voyage-and-Return films on Netflix right now") to Reddit/Letterboxd communities; measure engagement
- [ ] 15 short user interviews: how do you pick what to watch? would you browse by story shape?
- **Exit:** 300+ signups or clear qualitative pull. Otherwise revisit positioning.

### Phase 1 — Narrative data pilot (3 weeks)
- [ ] Finalize taxonomy (Section 4) and JSON schema
- [ ] Build gold set of 100 titles
- [ ] Ingest 500 titles (TMDB + Wikipedia summaries)
- [ ] Run annotation pipeline; measure cost per title and agreement vs. gold set
- [ ] Iterate prompts
- **Exit:** ≥80% archetype agreement; cost per title known.

### Phase 2 — Availability + scale (3 weeks)
- [ ] Vendor comparison → decision (Section 5.3)
- [ ] Availability sync job for US (one country first)
- [ ] Scale annotation to ~10–20K most-watched titles available in the US
- [ ] Embeddings + similarity search working
- **Exit:** any title → "stories like this" on a given set of services, in < 1 sec.

### Phase 3 — MVP app (6 weeks)
- [ ] Onboarding, Tonight, Browse by Story Shape, Title page, Feedback
- [ ] Explainable "why" line on every rec
- [ ] TestFlight / internal testing with 30–50 people from the Phase 0 list
- **Exit:** D7 retention and "rec was good" rate measured; top issues fixed.

### Phase 4 — Launch + expand (ongoing)
- [ ] Public launch (App Store, Play Store, Product Hunt)
- [ ] Pro tier + affiliate links
- [ ] TV episode/season-level arcs
- [ ] Additional countries
- [ ] Books (separate availability sources)

---

## 10. Open decisions for Hari

1. Launch country (recommend: US only)
2. Movies only for v1, or movies + TV series? (recommend: both, series-level only)
3. Availability vendor (after Phase 2 comparison)
4. What is free vs. Pro
5. Final browse taxonomy rows
6. Keep the name "Laminary" for a consumer app, or rebrand?
7. Build solo, or bring in a mobile developer for Phase 3?

---

## 11. Metrics that matter

- Onboarding completion rate
- % of sessions where a user taps through to a streaming service
- "Good rec" rate (👍 / total rated)
- D1 / D7 / D30 retention
- Browse-by-structure usage vs. Tonight usage (validates the differentiator)
- Pro conversion (later)

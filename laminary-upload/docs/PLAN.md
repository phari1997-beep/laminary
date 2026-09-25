# Laminary — Product & Build Plan

Solo build. Hari is PM. Execution by five Claude Code subagents (see `CLAUDE.md`). Items marked **[OPEN]** need Hari's decision. Task owners are in brackets.

---

## 1. Product

A watch app where you select your streaming services and get recommendations based on **story structure**, not genre. Every title is analyzed through four layers (Surface Story, Archetypal Plot, Mythic Blueprint, Structural Skeleton). Users browse by story shape across all their services and open any title for a four-layer breakdown.

- **Primary:** "What should I watch tonight, on services I already have?"
- **Signature:** Browse and match by story shape.
- **Depth:** Four-layer analysis per title.

Vision, voice, and promises: `VISION.md`. Design: `DESIGN.md`.

---

## 2. Key constraints (from the sanity check)

| Assumption | Reality | Approach |
|---|---|---|
| Train a model on every plot | Expensive, unnecessary, needs texts you can't legally use | LLM **annotates** licensed summaries; **embeddings** for similarity. No training |
| Availability data is easy | Hardest data problem; changes daily, varies by country | License it (5.3). US only first |
| Books in v1 | Separate availability world | Movies + TV first; books later |
| LLM gets every title right | Hallucinates on thin summaries | Only annotate titles with solid summaries; confidence scores; gold set |

Competitors: JustWatch, Reelgood (availability + recs), Letterboxd (social logging), streamers' own recs. Laminary's edge is the narrative layer.

Monetization **[OPEN]**: free core; Pro (~$3–5/mo) for full breakdowns and unlimited "stories like X"; subscription referral revenue where program terms allow.

---

## 3. Core user flows (MVP)

1. **Onboarding (< 60 sec):** country → services → 3–5 loved titles *or* Letterboxd CSV import
2. **Tonight:** ranked recs on your services only, each with a one-line why
3. **Browse by Story Shape:** rows by shape; filters for service, runtime, movie/TV, era, mood
4. **Title page:** where to watch (deep link), story-shape arc line, four-layer breakdown with spoiler toggle, "more stories shaped like this," add to watchlist
5. **Feedback:** 👍 / 👎 / watched

---

## 4. Narrative taxonomy

- **Emotional arc (story shape):** Rags to Riches · Riches to Rags · Man in a Hole · Icarus · Cinderella · Oedipus
- **Plot archetype (Booker's seven basic plots):** Overcoming the Monster · Rags to Riches · The Quest · Voyage and Return · Comedy · Tragedy · Rebirth
- **Mythic blueprint:** Hero's Journey stage coverage, plus variants (Heroine's Journey, anti-hero descent)
- **Beat tags:** mentor dies · twist ending · unreliable narrator · found family · redemption arc · pyrrhic victory · time loop · heist structure · ensemble convergence · ambiguous ending

**[OPEN]** Final 10–15 top-level browse rows.

---

## 5. Data

### 5.1 Metadata [data-pipeline]
TMDB (commercial use needs a TMDB agreement; attribution required) · Wikidata (CC0) for cross-IDs.

### 5.2 Plot summaries [data-pipeline]
Wikipedia plot sections (CC BY-SA; attribute derived text) · TMDB overviews. Skip titles without substantial summaries.

### 5.3 Availability **[OPEN]** [backend + sre]
Compare: Watchmode API · TMDB watch providers (JustWatch-sourced, attribution, check commercial terms) · JustWatch partnership · Streaming Availability API. One-page comparison before Phase 2.

### 5.4 Annotation pipeline [data-pipeline]
```
summary → Claude (Batch, fixed JSON schema) → tags + four layers + arc points + confidence
        → embedding (narrative fingerprint) → Postgres/pgvector
```
Version every record (model, prompt, source). Pilot 500 titles to measure cost before scaling.

### 5.5 Quality [data-pipeline + qa]
Gold set of ~100 titles hand-labeled by Hari (+ friends). Target ≥80% top-level archetype agreement before launch.

---

## 6. Recommendation engine (v1) [backend]
```
score = w1·cosine(user_profile, title_fingerprint)
      + w2·tag_overlap(liked_tags, title_tags)
      + w3·quality_signal
      − penalties (watched, disliked shapes)
FILTER: on user's services, in user's country
```
Profile = average of seed fingerprints, updated by feedback. Every rec returns a one-line why. No collaborative filtering until there are enough users.

---

## 7. Stack
| Layer | Choice | Owner |
|---|---|---|
| App (iOS/Android/web) | Expo | frontend |
| DB, auth, vectors | Supabase (Postgres + pgvector) | backend |
| Pipelines | Python | data-pipeline |
| LLM | Claude API (Batch for bulk) | data-pipeline |
| CI/CD, jobs, monitoring | GitHub Actions + Supabase scheduled functions | sre |
| Product analytics | PostHog | sre (setup), frontend (events) |

---

## 8. Legal & risk
- [ ] TMDB commercial terms confirmed (Hari)
- [ ] Availability vendor contract reviewed (Hari)
- [ ] Wikipedia attribution handled [frontend]
- [ ] No scripts/subtitles/book texts ingested [data-pipeline, qa]
- [ ] Poster/image use within provider terms [frontend]
- [ ] Privacy policy (Hari; draft by coordinator)
- [ ] App Store / Play Store requirements [sre, qa]
- [ ] Trademark search on "Laminary" (Hari)

Top risks: availability cost/licensing; tag accuracy; low willingness to pay; incumbents adding structure browse.

---

## 9. Execution

### Phase 0 — Validate (2 weeks)
- [ ] Repo, environments, CI skeleton [sre]
- [ ] Taxonomy + JSON schema draft [data-pipeline] → Hari approves
- [ ] 15 user interviews; landing page signups (Hari; marketing work is separate)
- **Exit:** clear pull from interviews/signups

### Phase 1 — Narrative data pilot (3 weeks)
- [ ] Gold set of 100 titles (Hari labels; [data-pipeline] builds tooling)
- [ ] Ingest 500 titles [data-pipeline]
- [ ] Annotation pipeline + evaluation script [data-pipeline]; review [qa]
- [ ] Prompt iteration until target met [data-pipeline]
- **Exit:** ≥80% archetype agreement; cost per title known

### Phase 2 — Availability + scale (3 weeks)
- [ ] Vendor comparison [backend + sre] → Hari decides
- [ ] Schema + migrations [backend]
- [ ] Availability sync job, US only [sre + backend]
- [ ] Scale annotation to 10–20K titles [data-pipeline] (Hari approves spend)
- [ ] Similarity search + scoring API with "why" lines [backend]
- [ ] FMEA on pipelines [sre]
- **Exit:** any title → stories like this, filtered by services, < 1 sec

### Phase 3 — MVP app (6 weeks)
- [ ] Design proposal [frontend] → Hari approves
- [ ] Core flows from section 3 [frontend + backend]
- [ ] **Add-ons (low build risk, reuse existing data):**
  - [ ] Spoiler toggle on all analysis (required, not optional)
  - [ ] Letterboxd CSV import for onboarding seeds
  - [ ] Story DNA share card (taste breakdown as an image)
  - [ ] Watch Together blend (two profiles, shared services)
  - [ ] Mood and runtime filters
  - [ ] Cross-service watchlist
  - [ ] Public story-shape web pages, statically rendered
- [ ] Analytics events [frontend]; dashboards [sre]
- [ ] Release checklist + TestFlight with 30–50 testers [qa + sre]
- **Exit:** D7 retention and "good rec" rate measured; blockers fixed

**Out of scope for MVP:** social feeds, user reviews, AI chat assistant, episode-level TV analysis, books.

### Phase 4 — Launch + expand
- [ ] Store launch [sre + qa]
- [ ] Pro tier [backend + frontend] **[OPEN]** pricing
- [ ] Season-level TV arcs; more countries; books

---

## 10. Open decisions for Hari
1. Launch country (recommend US)
2. Movies + TV series-level in v1? (recommend yes)
3. Availability vendor
4. Free vs. Pro split
5. Final browse rows
6. Keep the name "Laminary" for a consumer app?
7. Design direction (after frontend's proposal)

---

## 11. Metrics
Onboarding completion · tap-through to a streaming service · good-rec rate · D1/D7/D30 retention · browse-by-shape vs. Tonight usage · share-card creation rate · Pro conversion (later)

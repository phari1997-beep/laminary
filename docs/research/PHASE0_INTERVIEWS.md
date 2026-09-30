# Phase 0 interviews: findings

16 responses to the Google Form, collected 2026-09-29. This summary has no names or emails; the raw sheet is in Hari's Drive.

**Sample caveat:** mostly Hari's network. 14 respondents are in the US and 2 in India, mostly young, many from South Indian backgrounds. Treat the numbers as directional, not representative.

The willingness-to-pay question was not asked, so there is no pricing signal yet.

## Numbers
| Question | Result |
|---|---|
| How useful would this be (1–5) | mean 3.9; **11/16 rated 4 or 5** (3 fives, 8 fours, 5 threes, no 1s or 2s) |
| Wanted "another story like that" and couldn't find it | **10/16 yes, often**; 3 a couple of times; 3 not sure |
| Mention story arc when recommending (multi-select) | 13/16. This is probably inflated, because the survey itself was about stories |
| Time to pick something | 6/16 take 15–30 min; 8 take 5–15 min; 2 under 5 min |
| Streaming services paid for | mean 3.75; 12/16 pay for 3 or more |
| Rewatch familiar because nothing new (1–5) | mean 2.6 |
| Use Letterboxd | 1 active, 1 occasional, 14 no |
| Want early access / to hear more | 16/16 |

## What people want
- **"More like X" is a real, recurring pain.** Examples:
  - Sopranos → The Wire → "lost"
  - "nothing like Shrinking"
  - sitcoms like Brooklyn 99
  - crime series ("I've watched them all")
  - Tehran → other spy thrillers set in Iran, found by "dumb luck"
  - "Malayalam thrillers but make it Tamil"
- **Streaming recommendations feel repetitive, generic or badly tuned.** 5+ respondents said so.
- **Only show what I can actually watch.** One respondent hates recommendations that turn out to be buy or rent. This backs product promise 2.
- **Explain the similarity.** One asked for "Better Call Saul is 70% similar to Breaking Bad". This backs the one-line "why".
- **Mood-based picking** ("it should be light and funny"): the mood/tone filter matters.
- **Group watching:** "no consensus when in group" (2 respondents). This backs Watch Together.
- **Low friction:** simple UI, no ads (3 said ads would stop them), short time from search to watch.

## Risks the interviews raise
1. **Structure alone can produce absurd matches.** One respondent's own example of a bad recommendation: *Finding Nemo* and *Taken* ("both about a dad finding his lost kid"). A story-shape engine will produce exactly that unless it also weighs genre, tone and audience. **Action for backend/data-pipeline:** similarity must combine story shape with genre and tone guards (PLAN §6 tag overlap), and the gold set should include pairs like this as "should NOT match".
2. **What people mean by "like X" is often not structure.** They mean setting (Iran), language or region (Tamil), humour style (sitcoms), tone, or quality. Story shape is one axis among several; the "why" line should say which axis matched.
3. **"How is this different from asking an AI chatbot?"** One respondent asked this directly. We need a clear answer in the positioning: only what's on your services, browsable by shape, consistent and explainable, no chat required.
4. **Effort to describe what you want.** One respondent flagged "the mental exercise of thinking of inputs beyond genre". Tonight (zero input) has to work before Browse by shape does.
5. **Letterboxd import is low value for this sample** (2/16 use it). Consider deprioritising it in Phase 3.
6. **Non-US and non-English catalogues** (Tamil, Malayalam and Korean interest; 2 respondents in India; 4/16 pay for Crunchyroll). This fits the "not US-only" decision. Wikipedia coverage of regional-language films needs checking in the pilot.

## Signal vs the Phase 0 exit
- **For:** strong, specific "more like X" pain; 11/16 rated it 4–5; everyone wants early access.
- **Against:**
  - The sample is friendly and small.
  - There's no willingness-to-pay data.
  - The landing page only launched on 2026-09-29 (2 signups so far, including Hari's own test).
  - The original brief's "300+ signups" bar is far off.

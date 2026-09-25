# Design rules

Owner: `frontend` subagent. Hari approves the design direction before any screen is built.

## The rule
**No AI aesthetics.** The app should look like a small, opinionated team that loves films made it, not like a generated template.

## Banned (do not use unless Hari explicitly asks)
- Purple/blue gradients, gradient washes, glows, glassmorphism, blurred blobs
- Sparkle icons, robot/brain icons, "AI" badges, emoji used as icons
- The SaaS card kit: identical rounded cards, one radius on everything, soft grey shadow under each
- Cream background + serif display + terracotta accent
- Near-black background + one acid-green or vermilion accent
- Broadsheet/newspaper hairline-rule layouts
- ALL-CAPS tracked eyebrow labels above headings; "A · B · C" meta strings; "→" on every button
- Monospace for small data labels by default
- Numbered 01 / 02 / 03 markers on content that isn't a sequence
- Fade-and-slide-up on every section; hover animation on every card
- Default type families picked because they're safe

## Direction
- **Spend boldness in one place: the story-shape line.** Each title's emotional arc is drawn as a single line (rise, fall, rise). It's Laminary's visual signature: on title pages, in browse rows, on share cards. Everything around it stays quiet.
- **Let the posters bring the color.** Keep UI chrome restrained so the artwork carries the page.
- **Typography carries the personality.** One or two deliberately chosen families, a clear type scale, sentence case.
- **Dense but calm.** Film people like information. Show runtime, year, and service without clutter.
- Motion only in answer to a user's action, or one orchestrated moment (e.g. the arc line drawing itself on a title page).

## Process (required)
1. Frontend proposes a token system: 4–6 named hex colors, typefaces and roles, layout concept with ASCII wireframes for Tonight, Browse, and Title page.
2. Frontend self-reviews the proposal against the banned list and says what it changed.
3. **Hari approves [OPEN].** Log the result in `DECISIONS.md`.
4. Build. Screenshot and critique each screen against this file before handing to QA.

## Copy
Sentence case, active voice, plain verbs. Buttons say exactly what happens ("Add to watchlist," then "Added"). Errors say what happened and how to fix it. Empty states invite action.

## Quality floor
Works at small phone widths, dark and light mode, visible focus states, respects reduced motion, meets contrast guidelines.

"""Keep only the plot-like parts of a series' plot section (fetcher 1.4.0, DECISIONS 2026-10-02).

Applies to TV series (main articles and season articles). A film's plot section is used whole.

A series article's "Episodes", "Seasons" or "Series overview" section often holds production
history, ratings, broadcast dates, spin-offs or fan theories instead of (or next to) the story.
The fetcher used to take such a section with all its subsections. Now each section is split at
its subsection headings and every part is kept or dropped by three plain rules, applied in
order. Every dropped part is recorded with its reason in the plot file.

1. **Headings.** A heading that names the story ("Season 3", "Series 2: The Reboot", "Part I",
   "... Saga", "... Arc", "Plot", "Plotlines", "Synopsis", "Story"; ``PLOT_HEADING``) is
   plot-like, and that wins over the word denylist. Otherwise a subsection whose heading names
   non-plot content is dropped, with everything under it: production, development, casting,
   music, broadcast, ratings, reception, awards, home media, distribution, merchandise,
   interpretations, themes, influences, spin-offs, crossovers, adaptations, legacy, episode
   lists, catchphrases, documentaries, anniversaries and similar (``NONPLOT_WORDS``,
   ``NONPLOT_HEADINGS``). "Mythology" is not on the list: in some series (The X-Files) it is
   the main storyline, so the content check decides.
2. **Broad sections.** Under a *broad* heading ("episodes", "seasons", "series overview",
   "overview", "season overview"; ``BROAD_HEADINGS``):
   - the text directly under "Episodes" or "Seasons" is presumed to be the episode-list lead
     (run dates, episode counts, networks): it goes through a stricter content check
     (dropped from ``EPISODE_LEAD_MAX_SHARE``, a quarter, of cue-dense words);
   - a direct subsection is kept only if its heading is plot-like, or names specials ("Specials",
     "Christmas special": content check); other headings ("Missing episodes", "CID Special
     Bureau") are dropped;
   - in a kept season part, a first paragraph that reads as production writing (the cue test
     of rule 3) is dropped: such sections often open each season with its renewal and
     premiere dates (Better Call Saul, Blackadder). Later paragraphs are never cut.
   Under any other plot heading ("plot", "synopsis", "premise", ...) the section's own text is
   always kept, and subsections not caught by rule 1 are kept (subject to rule 3 when their
   heading is not plot-like).
3. **Content check.** A part that rule 2 would keep under a broad heading, or a non-plot-like
   subsection under any heading, is dropped when at least half of its words are in
   paragraphs that read as production or broadcast writing: 2 or more cue words from
   ``NONPLOT_CUES`` ("aired", "premiered", "ratings", "viewers", "renewed", "showrunner",
   "filmed", "DVD", "critics", "narration", ...) and at least 2.5 cue words per 100 words.

Finally a broad section counts as a plot section only if the kept parts hold at least half of
its words (``BROAD_MIN_KEPT_SHARE``): a section that is mostly non-plot is rejected as a whole,
so the next heading, the 150-word rule and the season-article fallback apply as usual. A
section with nothing dropped yields exactly the text the fetcher produced before (the parts
are slices of the section's HTML and are rejoined unchanged).

Fail closed on markup: if the section holds heading tags (``<h2>``-``<h6>``) that are not in
MediaWiki's ``mw-heading`` wrapper, the parts can't be told apart, so the section is not used
(``markup_error`` in its check) instead of being taken unfiltered.

Limits, by design: the check works on subsections and on paragraphs' vocabulary only; apart
from a season's first paragraph, a production sentence inside a kept season summary stays,
and a story about a TV show (its characters air episodes and chase ratings) can look like
production writing, which is why the content check is applied only where the heading doesn't
already say "plot".
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from typing import Any

from laminary_pipeline.ingest.text import html_to_text, word_count

# Top-level headings whose content is often not the story (rule 2).
BROAD_HEADINGS = frozenset(
    {"episodes", "seasons", "series overview", "overview", "season overview"}
)
# Broad headings whose own lead is presumed to be the episode-list lead (rule 2).
EPISODE_LIST_HEADINGS = frozenset({"episodes", "seasons"})
EPISODE_LEAD_MAX_SHARE = 0.25  # stricter than CONTENT_MAX_SHARE: non-plot is the default here
BROAD_MIN_KEPT_SHARE = 0.5

# Rule 1: a word or phrase anywhere in a subsection heading.
NONPLOT_WORDS = (
    "production", "development", "conception", "filming", "casting", "cast", "crew",
    "music", "soundtrack", "theme song", "broadcast", "broadcasting", "airing", "ratings",
    "viewership", "reception", "critical response", "accolades", "awards", "nominations",
    "home media", "home video", "dvd", "blu-ray", "release", "releases", "distribution",
    "syndication", "merchandise", "merchandising", "consumer products",
    "interpretation", "interpretations", "themes", "theme", "motifs", "symbolism",
    "influences", "inspiration", "inspirations", "derivations", "spin-off", "spin-offs",
    "spinoff", "spinoffs", "sub-series", "crossover", "crossovers", "backdoor pilot",
    "backdoor pilots", "adaptation", "adaptations", "other media", "cancellation",
    "episode list", "list of episodes", "missing episodes", "catchphrases", "running gags",
    "trivia", "documentary", "anniversary", "live stage", "stage performances",
    "controversy", "controversies", "lawsuit", "references", "notes", "see also",
    "external links", "further reading", "bibliography", "webisodes", "minisodes",
    "prequel shorts", "title sequence", "opening sequence", "opening credits", "revival",
    "reboot",
)
# Rule 1: whole headings only (these words are fine inside a plot heading such as
# "Future Trunks Saga" or "Story and characters").
NONPLOT_HEADINGS = frozenset(
    {"future", "legacy", "characters", "main characters", "recurring characters",
     "supporting characters", "format", "episode format", "series format", "chronological order",
     "style", "impact", "cultural impact"}
)
_NONPLOT_WORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in sorted(NONPLOT_WORDS, key=len, reverse=True))
    + r")\b"
)
# Rule 2: plot-like headings.
PLOT_HEADING = re.compile(
    r"\b(plot|plots|synopsis|synopses|summary|summaries|story|stories|storyline|storylines"
    r"|plotline|plotlines|premise|arc|arcs|saga|sagas)\b"
    r"|\b(season|seasons|series|part|parts|book|volume|chapter)\s+([0-9]+|[ivxlc]+)\b"
    r"|^(season|series|part|book|volume|chapter|act)\b"
)
# Rule 2: specials under a broad heading go to the content check instead of being dropped.
SPECIALS_HEADING = re.compile(r"\bspecials?\b")
# Rule 3: production / broadcast / reception vocabulary.
NONPLOT_CUE_WORDS = (
    # broadcast and release
    r"air(?:s|ed|ing)", r"air dates?", r"broadcasts?", r"broadcasting", r"premiered",
    r"premieres?", r"syndicat(?:ed|ion)", r"reruns?", r"time ?slots?", r"nielsen ratings?",
    r"ratings?", r"viewers", r"viewership", r"audiences?", r"streaming", r"dvds?", r"blu-ray",
    r"home media", r"box set", r"renewed", r"cancell?ed", r"cancellation", r"webisodes",
    # production
    r"produced", r"producers?", r"production", r"writers?", r"written", r"wrote", r"scripts?",
    r"screenwriters?", r"showrunners?", r"series creator", r"directors?", r"directed",
    r"filmed", r"filming", r"casting", r"cast members?", r"actors?", r"actress(?:es)?",
    r"played by", r"portrayed by", r"guest stars?", r"budget", r"soundtrack", r"theme song",
    r"composer", r"merchandise", r"spin-?offs?", r"pilot episode", r"the show's",
    r"the programme", r"improvis\w*", r"narrat(?:or|ors|ion)", r"voice-?overs?",
    r"episode titles?",
    # reception and attribution
    r"critics", r"critical(?:ly)?", r"acclaim(?:ed)?", r"praised", r"reviews?", r"reviewers?",
    r"emmys?", r"awards?", r"nominat(?:ed|ion|ions)", r"accolades", r"interviews?",
    r"commentary", r"stated", r"according to", r"fans", r"fan theor(?:y|ies)",
)
NONPLOT_CUES = re.compile(r"\b(?:" + "|".join(NONPLOT_CUE_WORDS) + r")\b", re.IGNORECASE)
CUE_MIN_HITS = 2  # per paragraph
CUE_MIN_PER_100_WORDS = 2.5
CONTENT_MAX_SHARE = 0.5  # a part with this share of its words in cue-dense paragraphs is dropped

_HEADING_RE = re.compile(
    r'<div class="mw-heading(?: mw-heading\d)?"[^>]*>\s*<h([2-6])\b[^>]*>(.*?)</h\1>', re.S
)
_ANY_HEADING_TAG = re.compile(r"<h[2-6]\b")
_PARAGRAPH = re.compile(r"<p\b[^>]*>.*?</p>", re.S)


def normalize_heading(line: str) -> str:
    """Lower case, markup stripped, whitespace collapsed, trailing ':'/'.' removed."""
    text = re.sub(r"<[^>]+>", "", line)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text.rstrip(":.")


def _subheading(raw: str) -> str:
    return normalize_heading(html_lib.unescape(re.sub(r"<[^>]+>", "", raw)))


@dataclass
class Part:
    level: int
    heading: str
    html: str
    words: int = 0
    parent: int | None = None  # index of the enclosing part
    kept: bool = True
    reason: str | None = None
    trimmed: dict[str, Any] | None = None  # a dropped first paragraph (rule 2)


@dataclass
class SectionFilter:
    """The outcome for one section: the text to use (kept parts only) and what was dropped."""

    heading: str
    kind: str  # "broad" or "plot" (series), "film" (used whole)
    text: str
    words: int
    words_before: int
    accepted: bool
    reason: str | None
    dropped: list[dict[str, Any]] = field(default_factory=list)
    markup_error: bool = False

    def record(self, index: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"heading": self.heading}
        if index is not None:
            out["index"] = index
        out.update(kind=self.kind, words_before=self.words_before, words_kept=self.words,
                   accepted=self.accepted)
        if self.markup_error:
            out["markup_error"] = True
        if self.reason:
            out["reason"] = self.reason
        out["dropped"] = self.dropped
        return out


def split_parts(html: str) -> list[Part]:
    """Slices of the section HTML, one per heading (the first holds anything before it). The
    slices rejoin to the original string."""
    starts = [(m.start(), int(m.group(1)), _subheading(m.group(2)))
              for m in _HEADING_RE.finditer(html)]
    if not starts:
        return [Part(2, "", html)]
    parts: list[Part] = []
    for i, (pos, level, heading) in enumerate(starts):
        begin = 0 if i == 0 else pos
        end = starts[i + 1][0] if i + 1 < len(starts) else len(html)
        parts.append(Part(level, heading, html[begin:end]))
    for i, part in enumerate(parts):
        part.words = word_count(html_to_text(part.html))
        for j in range(i - 1, -1, -1):
            if parts[j].level < part.level:
                part.parent = j
                break
    return parts


def nonplot_heading(heading: str) -> str | None:
    """The denylist entry a subsection heading matches (rule 1), else None. A plot-like heading
    ("Season 5: Revival (2016)") is never denied by a word in it."""
    if heading in NONPLOT_HEADINGS:
        return heading
    if is_plot_heading(heading):
        return None
    m = _NONPLOT_WORD_RE.search(heading)
    return m.group(1) if m else None


def is_plot_heading(heading: str) -> bool:
    return bool(PLOT_HEADING.search(heading))


def is_nonplot_paragraph(para: str) -> bool:
    """The rule 3 cue test for one paragraph of text."""
    words = word_count(para)
    hits = len(NONPLOT_CUES.findall(para))
    return bool(words) and hits >= CUE_MIN_HITS and hits * 100 / words >= CUE_MIN_PER_100_WORDS


def nonplot_share(text: str) -> float:
    """Share of the words of ``text`` in paragraphs that read as production or broadcast
    writing (rule 3)."""
    total = flagged = 0
    for para in text.split("\n\n"):
        words = word_count(para)
        if not words:
            continue
        total += words
        if is_nonplot_paragraph(para):
            flagged += words
    return flagged / total if total else 0.0


def _content_reason(part: Part, limit: float = CONTENT_MAX_SHARE) -> str | None:
    share = nonplot_share(html_to_text(part.html))
    if share >= limit:
        return (f"content: {share:.0%} of its words are in paragraphs with production, "
                "broadcast or reception wording")
    return None


def _trim_first_paragraph(part: Part) -> None:
    """Rule 2: drop a season part's first paragraph if it reads as production writing."""
    for m in _PARAGRAPH.finditer(part.html):
        text = html_to_text(m.group(0))
        if not word_count(text):
            continue  # an empty or markup-only paragraph: look at the next one
        if is_nonplot_paragraph(text):
            part.html = part.html[: m.start()] + part.html[m.end():]
            part.trimmed = {"words": word_count(text), "starts": text[:80]}
        return


def filter_section(html: str, heading: str, media_type: str = "tv_series") -> SectionFilter:
    """Apply rules 1-3 to one top-level section's HTML (the section with its subsections).
    Series only: a film's plot section is returned whole (``kind: "film"``). In the pilot the
    few film plot sections with subsections are all story ("Part 1", "Director's cut",
    "Theatrical release (1975)" in Sholay), so the series rules would only do harm there."""
    words_before = word_count(html_to_text(html))
    if media_type != "tv_series":
        text = html_to_text(html)
        return SectionFilter(heading, "film", text, words_before, words_before, True, None)
    kind = "broad" if heading in BROAD_HEADINGS else "plot"
    parts = split_parts(html)
    found = len(_ANY_HEADING_TAG.findall(html))
    recognised = len(parts) if parts[0].heading else 0
    if found != recognised:
        return SectionFilter(
            heading, kind, "", 0, words_before, False,
            f"markup_error: {found} heading tags but {recognised} in MediaWiki's mw-heading "
            "wrapper; the parts can't be told apart, so the section is not used",
            markup_error=True)
    top = parts[0]
    for i, part in enumerate(parts):
        if i == 0:
            if kind == "broad" and heading in EPISODE_LIST_HEADINGS:
                reason = _content_reason(part, EPISODE_LEAD_MAX_SHARE)
                if reason:
                    part.kept, part.reason = False, (
                        f"episode-list lead under {heading!r}, {reason} (dropped from "
                        f"{EPISODE_LEAD_MAX_SHARE:.0%})")
            elif kind == "broad":
                reason = _content_reason(part)
                if reason:
                    part.kept, part.reason = False, reason
            continue
        parent = parts[part.parent] if part.parent is not None else top
        if part.parent is not None and part.parent != 0 and not parent.kept:
            part.kept, part.reason = False, f"under dropped subsection {parent.heading!r}"
            continue
        denied = nonplot_heading(part.heading)
        if denied:
            part.kept, part.reason = False, f"heading names non-plot content ({denied!r})"
            continue
        plot_like = is_plot_heading(part.heading)
        special = bool(SPECIALS_HEADING.search(part.heading))
        if kind == "broad" and not plot_like and not special and part.parent in (0, None):
            part.kept, part.reason = False, (
                "not a plot heading: under an episodes/seasons/overview section only season, "
                "part, arc, plot and specials subsections are kept")
            continue
        if kind == "broad" and plot_like:
            _trim_first_paragraph(part)
        if kind == "broad" or not plot_like:
            reason = _content_reason(part)
            if reason:
                part.kept, part.reason = False, reason
    kept = [p for p in parts if p.kept]
    if len(kept) == len(parts) and not any(p.trimmed for p in parts):
        text = html_to_text(html)
    else:
        text = html_to_text("".join(p.html for p in kept))
    words = word_count(text)
    parents = {p.parent for p in parts}
    dropped = [
        {"heading": p.heading or heading, "level": p.level, "words": p.words, "reason": p.reason}
        for i, p in enumerate(parts) if not p.kept and (p.words > 0 or i in parents)
    ]
    dropped += [
        {"heading": p.heading, "level": p.level, "words": p.trimmed["words"],
         "reason": "first paragraph of a season part reads as production or broadcast "
                   "writing", "paragraph": 1, "starts": p.trimmed["starts"]}
        for p in parts if p.kept and p.trimmed
    ]
    accepted, reason = True, None
    if kind == "broad" and words_before and words < BROAD_MIN_KEPT_SHARE * words_before:
        accepted = False
        reason = (f"mostly non-plot: kept {words} of {words_before} words (under "
                  f"{BROAD_MIN_KEPT_SHARE:.0%}), so the section doesn't count as a plot section")
    return SectionFilter(heading, kind, text if accepted else "", words if accepted else 0,
                         words_before, accepted, reason, dropped)

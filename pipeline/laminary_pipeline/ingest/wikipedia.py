"""Fetch a title's English Wikipedia plot section as plain text (CC BY-SA).

Flow for one title, all through the MediaWiki Action API on en.wikipedia.org:

1. (Only when the candidate has no enwiki title) look up the enwiki sitelink for the QID on
   www.wikidata.org (``wbgetentities``).
2. ``action=query``: resolve redirects, get the latest revision id and timestamp, the page's
   Wikidata item and whether it's a disambiguation page. The page's item must equal the
   candidate's QID, so a summary can never belong to a different work.
3. ``action=parse&oldid=<rev>&prop=sections``: list sections of that exact revision.
4. ``action=parse&oldid=<rev>&section=<n>&prop=text``: render the chosen section (with its
   subsections) and strip it to plain text (``text.html_to_text``).

Everything after step 2 is pinned to one revision id, so it is cached forever and the stored
``content_sha256`` always describes a reproducible text.

Section choice: the first heading, in the priority order below for the title's type, whose text
has at least ``MIN_WORDS`` words after non-plot parts are dropped (``sections.py``, DECISIONS
2026-10-02: production, ratings, broadcast and similar subsections are dropped, and a broad
"Episodes"/"Seasons"/"Series overview" section that is mostly non-plot doesn't count). Every
section tried is recorded under ``section_checks`` with what was dropped and why. Titles with no
such section are skipped with a recorded reason and never annotated (DECISIONS 2026-09-26:
150-word minimum; never guess from a title).

Series fallback (DECISIONS 2026-10-01): when a series' main article is too short or has no plot
section, its verified per-season articles are tried (``seasons.py``). Such a record has
``via: "season_articles"`` and a ``sources`` list (one entry per season article, each with its
own schema-shaped ``source`` and ``text``) instead of the single ``source`` and ``text``; the
main article's result is kept under ``main_article``.

Episode-table fallback (fetcher 1.5.0, DECISIONS 2026-10-02): when the season-article prose
join also yields no usable text (under the 150-word minimum, or nothing), the per-episode
summaries in the episode tables of the same verified season pages (else the verified episode-list
page) are joined instead (``episodes.py``). Such a record also has ``via: "season_articles"``,
plus ``via_detail: "episode_table"`` (on the record and on each entry of ``sources``) and an
``episode_tables`` report: pages used and their Wikidata evidence, episodes used per season, the
partly included season, and what was left out over the cap. ``season_articles`` keeps the
prose attempt.

Richer text for priority series (fetcher 1.5.2, DECISIONS 2026-10-02): for one of the ten
priority series (``priority.PRIORITY_SERIES``) whose main article passes with fewer than
``RICHER_TEXT_WORDS`` (500) words, the season-article / episode-table path above is tried as
well (run rules included), and the longer text is used (the main article on a tie or when the
other path fails). Either way the record carries ``richer_text``: the rule, the threshold, which
text was chosen and both word counts. Other titles are unchanged.

Series run rules (fetcher 1.5.1, DECISIONS 2026-10-02): for a series listed in
``runs.SERIES_RUN_RULES`` (Doctor Who: the 2005 revival only), both fallbacks use only the
pages and list-page headings of that run, and the season total ignores Wikidata P2437. The
record then carries ``series_run_rule`` (the rule's name) and ``series_run_ignored`` (the pages
it left out, with the reason); list-page headings it skipped are in
``episode_tables.tables_skipped``.
"""


from __future__ import annotations

import urllib.parse
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from laminary_pipeline.annotation import require_wikipedia_sources
from laminary_pipeline.ingest.episodes import (
    VIA_DETAIL_EPISODE_TABLE,
    EpisodeJoin,
    SeasonEpisodes,
    join_episodes,
    list_page_seasons,
    parse_tables,
    season_episodes,
    season_page_tables,
    season_text,
)
from laminary_pipeline.ingest.http import HttpClient, HttpError
from laminary_pipeline.ingest.priority import PREFER_EPISODE_TEXT, PRIORITY_SERIES
from laminary_pipeline.ingest.seasons import (
    LEAD_BLOCK_CEILING,
    MAX_SEASON_NUMBER,
    MAX_SEASON_SOURCES,
    SEASON_WORD_CAP,
    STUB_SEASON_WORDS,
    YEAR_UNIT,
    Page,
    SeasonFinder,
    SeasonResult,
    season_coverage,
    year_coverage,
)
from laminary_pipeline.ingest.sections import SectionFilter, filter_section, normalize_heading
from laminary_pipeline.ingest.text import html_to_text, sha256_text, word_count

FETCHER_VERSION = "1.5.8"  # 1.1.0: per-season articles for thin series; 1.2.0: lead block
# of stub seasons (under STUB_SEASON_WORDS, 500) before the first full season; 1.3.0: stubs
# alone when the lead block is over the 6,000-word ceiling; 1.4.0 (DECISIONS 2026-10-02):
# non-plot subsections dropped (sections.py), MediaWiki "Cite error" text stripped, Wikidata
# P179/P361 evidence per season page, season coverage, lead_block_ceiling key; 1.5.0
# (DECISIONS 2026-10-02): episode-table fallback for series (episodes.py); 1.5.1 (DECISIONS
# 2026-10-02): per-title series run rules (runs.py; Doctor Who uses its 2005 revival only);
# 1.5.2 (DECISIONS 2026-10-02): episode-list pages verified by a main-article link when
# Wikidata states nothing (seasons.py), and richer text for thin priority series; 1.5.3
# (QA): the list page must link back to the main article, only a P179/P361 to a TV series
# rejects it, list titles match case-sensitively after the first letter; 1.5.4: run rules may
# offset Wikidata's series ordinal (Doctor Who: series N is ordinal N + 26); 1.5.5: a run
# rule may number by year (CID: list-page year headings; sources carry ``year``); 1.5.6
# (QA): only the year rule reads year headings, from the heading path; the list page's
# back-link must be in its lead; "television program" counts as a TV series
# 1.5.7 (QA): a two-part episode row (rowspan="2" title cell) keeps its title and both
# numbers ("S1E1–2"), instead of "S1E2:" with no title; 1.5.8 (DECISIONS 2026-10-02):
# episode-summary trivia stored apart from the plot (``trivia``), a single pilot is episode 0
# of the first season, and per-title episode-text overrides (Seinfeld)
MIN_WORDS = 150
# A priority series whose main-article text passes with fewer words than this also tries the
# season-article / episode-table path and keeps the longer text (fetcher 1.5.2, DECISIONS
# 2026-10-02). The same 500 words as a stub season.
RICHER_TEXT_WORDS = STUB_SEASON_WORDS
RICHER_TEXT_RULE = "priority_series_richer_text"
OVERRIDE_REASON = "per-title override"  # PREFER_EPISODE_TEXT (Seinfeld, 1.5.8)
API_URL = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
ARTICLE_BASE = "https://en.wikipedia.org/wiki/"
PERMALINK_BASE = "https://en.wikipedia.org/w/index.php"
LATEST_REVISION_TTL = 7 * 24 * 3600  # re-check the latest revision weekly

# Wikipedia text is CC BY-SA 4.0 from the 2023 Terms of Use change; older revisions were
# published under CC BY-SA 3.0. Revisions are labeled by their timestamp.
CC_BY_SA_4_FROM = "2023-06-29T00:00:00Z"

# Headings, normalized (lower case, markup stripped). Series articles often keep their story in
# a season-by-season overview; tables inside it are stripped, so an overview that is only an
# episode table falls through to the next heading.
MOVIE_HEADINGS = (
    "plot", "plot summary", "synopsis", "story", "storyline", "plot overview", "summary",
    "premise",
)
TV_HEADINGS = (
    "plot", "synopsis", "plot summary", "series overview", "season synopses", "seasons",
    "plot overview", "story", "storyline", "summary", "overview", "episodes", "premise",
)

# Skip reasons (recorded in the plot file; see data/README.md).
SKIP_NO_ARTICLE = "no_enwiki_article"
SKIP_MISSING_PAGE = "missing_page"
SKIP_DISAMBIGUATION = "disambiguation_page"
SKIP_QID_MISMATCH = "qid_mismatch"
SKIP_NO_SECTION = "no_plot_section"
SKIP_TOO_SHORT = "too_short"
SKIP_FETCH_ERROR = "fetch_error"  # transient: retried on the next run
SKIP_SEASON_TOO_LONG = "season_too_long"  # lone full season over LEAD_BLOCK_CEILING
VIA_SEASON_ARTICLES = "season_articles"


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def article_url(page_title: str) -> str:
    return ARTICLE_BASE + urllib.parse.quote(page_title.replace(" ", "_"), safe="()_,'!:-.*/")


def permalink(page_title: str, revid: int | str) -> str:
    query = urllib.parse.urlencode({"title": page_title.replace(" ", "_"), "oldid": str(revid)})
    return f"{PERMALINK_BASE}?{query}"


def license_for(revision_timestamp: str) -> str:
    return "CC-BY-SA-4.0" if revision_timestamp >= CC_BY_SA_4_FROM else "CC-BY-SA-3.0"


def headings_for(media_type: str) -> tuple[str, ...]:
    return TV_HEADINGS if media_type == "tv_series" else MOVIE_HEADINGS


@dataclass(frozen=True)
class Section:
    index: str
    heading: str
    line: str


def plot_sections(
    sections: list[dict[str, Any]], media_type: str, page_title: str
) -> list[Section]:
    """Top-level sections of this page that may hold the plot, in priority order."""
    order = headings_for(media_type)
    found: dict[str, Section] = {}
    for s in sections:
        index = str(s.get("index", ""))
        if not index.isdigit() or str(s.get("toclevel")) != "1":
            continue  # transcluded ("T-1") or subsection
        if s.get("fromtitle") and s["fromtitle"].replace("_", " ") != page_title:
            continue
        heading = normalize_heading(s.get("line", ""))
        if heading in order and heading not in found:
            found[heading] = Section(index, heading, s.get("line", ""))
    return [found[h] for h in order if h in found]


class PlotFetcher:
    def __init__(
        self,
        client: HttpClient,
        *,
        min_words: int = MIN_WORDS,
        clock: Callable[[], str] = now_utc,
        season_articles: bool = True,
        season_word_cap: int = SEASON_WORD_CAP,
    ) -> None:
        self.client = client
        self.min_words = min_words
        self.clock = clock
        self.season_articles = season_articles
        self.season_word_cap = season_word_cap

    # ---------- API calls ----------

    def _api(self, params: dict[str, Any], *, cache_ttl: float | None) -> dict[str, Any]:
        full = {"format": "json", "formatversion": "2", "maxlag": "5", **params}
        data = self.client.get_json(API_URL, full, cache_ttl=cache_ttl)
        if "error" in data:
            err = data["error"]
            raise HttpError(200, API_URL, f"{err.get('code')}: {err.get('info')}")
        return data

    def enwiki_title_for(self, qid: str) -> str | None:
        params = {
            "action": "wbgetentities", "ids": qid, "props": "sitelinks", "sitefilter": "enwiki",
            "format": "json", "formatversion": "2", "maxlag": "5",
        }
        data = self.client.get_json(WIKIDATA_API_URL, params, cache_ttl=LATEST_REVISION_TTL)
        entity = (data.get("entities") or {}).get(qid) or {}
        link = (entity.get("sitelinks") or {}).get("enwiki")
        return link.get("title") if link else None

    def latest_revision(self, title: str) -> dict[str, Any]:
        data = self._api(
            {
                "action": "query", "titles": title, "redirects": "1",
                "prop": "revisions|pageprops", "rvprop": "ids|timestamp",
                "ppprop": "wikibase_item|disambiguation",
            },
            cache_ttl=LATEST_REVISION_TTL,
        )
        pages = data.get("query", {}).get("pages", [])
        return pages[0] if pages else {"missing": True}

    def sections(self, revid: int) -> list[dict[str, Any]]:
        data = self._api({"action": "parse", "oldid": str(revid), "prop": "sections"},
                         cache_ttl=None)
        return data["parse"]["sections"]

    def section_html(self, revid: int, index: str) -> str:
        data = self._api(
            {
                "action": "parse", "oldid": str(revid), "section": index, "prop": "text",
                "disableeditsection": "1", "disablelimitreport": "1", "disabletoc": "1",
            },
            cache_ttl=None,
        )
        return str(data["parse"]["text"])

    def page_html(self, revid: int) -> str:
        """The whole rendered page of one revision (for its episode tables)."""
        data = self._api(
            {
                "action": "parse", "oldid": str(revid), "prop": "text",
                "disableeditsection": "1", "disablelimitreport": "1", "disabletoc": "1",
            },
            cache_ttl=None,
        )
        return str(data["parse"]["text"])

    def section_text(self, revid: int, index: str) -> str:
        """The whole section as text, nothing dropped (for inspection; the fetcher uses
        ``section_filtered``)."""
        return html_to_text(self.section_html(revid, index))

    def section_filtered(
        self, revid: int, index: str, heading: str, media_type: str = "tv_series"
    ) -> SectionFilter:
        """The section's plot-like parts only (``sections.filter_section``; series only, a
        film's plot section is used whole)."""
        return filter_section(self.section_html(revid, index), heading, media_type)

    # ---------- main entry ----------

    def fetch(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Build the plot record for one candidate. Never raises for content problems: those
        become ``status: skipped`` with a reason. Network failures become ``fetch_error``."""
        qid = candidate["qid"]
        media_type = candidate.get("media_type", "movie")
        result: dict[str, Any] = {
            "fetcher_version": FETCHER_VERSION,
            "qid": qid,
            "status": "skipped",
            "skip_reason": None,
            "skip_detail": None,
            "candidate": {
                k: candidate.get(k)
                for k in ("title", "year", "media_type", "bucket", "language", "decade",
                          "tmdb_id", "imdb_id", "series_status", "series_status_basis",
                          "number_of_seasons")
            },
            "min_words": self.min_words,
            "retrieved_at": self.clock(),
        }
        try:
            return self._fetch_into(result, candidate, qid, media_type)
        except HttpError as e:
            return {**result, "skip_reason": SKIP_FETCH_ERROR, "skip_detail": str(e)[:300]}

    def _fetch_into(
        self, result: dict[str, Any], candidate: dict[str, Any], qid: str, media_type: str
    ) -> dict[str, Any]:
        title = candidate.get("enwiki_title") or self.enwiki_title_for(qid)
        if not title:
            return {**result, "skip_reason": SKIP_NO_ARTICLE}
        page = self.latest_revision(title)
        if page.get("missing") or not page.get("revisions"):
            return {**result, "skip_reason": SKIP_MISSING_PAGE, "skip_detail": title}
        page_title = page["title"]
        props = page.get("pageprops") or {}
        result["page_title"] = page_title
        result["page_id"] = page.get("pageid")
        if "disambiguation" in props:
            return {**result, "skip_reason": SKIP_DISAMBIGUATION}
        page_qid = props.get("wikibase_item")
        if page_qid != qid:
            return {**result, "skip_reason": SKIP_QID_MISMATCH,
                    "skip_detail": f"page {page_title!r} is {page_qid}, expected {qid}"}
        rev = page["revisions"][0]
        revid, rev_ts = int(rev["revid"]), rev["timestamp"]
        result.update(revision=str(revid), revision_timestamp=rev_ts,
                      permalink=permalink(page_title, revid))

        all_sections = self.sections(revid)
        result["sections_seen"] = [
            normalize_heading(s.get("line", "")) for s in all_sections
            if str(s.get("toclevel")) == "1"
        ]
        candidates = plot_sections(all_sections, media_type, page_title)
        if not candidates:
            return self._try_seasons({**result, "skip_reason": SKIP_NO_SECTION}, media_type)

        best: tuple[int, Section] | None = None
        checks: list[dict[str, Any]] = []
        result["section_checks"] = checks
        for section in candidates:
            filtered = self.section_filtered(revid, section.index, section.heading,
                                             media_type)
            checks.append(filtered.record(section.index))
            if filtered.words >= self.min_words:
                ok = self._ok(result, page_title, revid, rev_ts, section, filtered)
                return self._prefer_richer(ok, media_type)
            if best is None or filtered.words > best[0]:
                best = (filtered.words, section)
        assert best is not None
        words, section = best
        trimmed = any(c["dropped"] or not c["accepted"] for c in checks)
        skipped = {
            **result,
            "skip_reason": SKIP_TOO_SHORT,
            "skip_detail": f"longest plot-like section {section.heading!r} has {words} words"
            + (" after non-plot parts were dropped" if trimmed else ""),
            "section": {"heading": section.heading, "index": section.index},
            "word_count": words,
        }
        return self._try_seasons(skipped, media_type)

    # ---------- priority series: richer text (fetcher 1.5.2) ----------

    def _prefer_richer(self, ok: dict[str, Any], media_type: str) -> dict[str, Any]:
        """For a priority series whose main article passed with under RICHER_TEXT_WORDS words,
        also try the season-article / episode-table path and keep the longer text. For a title
        in PREFER_EPISODE_TEXT (Seinfeld) that path's text is used whenever it is usable,
        whatever its length (reason "per-title override")."""
        override = ok["qid"] in PREFER_EPISODE_TEXT
        if (ok["qid"] not in PRIORITY_SERIES or media_type != "tv_series"
                or not self.season_articles
                or (ok["word_count"] >= RICHER_TEXT_WORDS and not override)):
            return ok
        why = (OVERRIDE_REASON if override else
               f"main article under {RICHER_TEXT_WORDS} words")
        attempt = {k: v for k, v in ok.items() if k not in ("source", "text")}
        attempt.update(status="skipped", skip_reason=None,
                       skip_detail=f"main article passed with {ok['word_count']} words; "
                       f"tried the season-article / episode-table path ({why})")
        other = self._try_seasons(attempt, media_type)
        other_words = other["word_count"] if other.get("status") == "ok" else 0
        if override:
            chosen = other if other.get("status") == "ok" else ok
        else:
            chosen = other if other_words > ok["word_count"] else ok
        report = {
            "rule": RICHER_TEXT_RULE,
            "reason": why,
            "threshold_words": RICHER_TEXT_WORDS,
            "chosen": "main_article" if chosen is ok else (
                other.get("via_detail") or VIA_SEASON_ARTICLES),
            "main_article_words": ok["word_count"],
            "alternative_words": other_words,
            "alternative_status": other.get("status"),
            "alternative_detail": None if other.get("status") == "ok"
            else (other.get("skip_detail") or other.get("skip_reason")),
        }
        if chosen is other:
            return {**other, "richer_text": report}
        if "series_run_rule" in other:
            # the rule shaped only the text that was not used (QA nit): noted in the report,
            # not as the record's own series_run_rule
            report["alternative_series_run_rule"] = other["series_run_rule"]
        return {**ok, "richer_text": report}

    # ---------- series: per-season articles ----------

    def _try_seasons(self, skipped: dict[str, Any], media_type: str) -> dict[str, Any]:
        """The main article failed; for a series, try its verified season articles."""
        if media_type != "tv_series":
            return skipped
        if not self.season_articles:
            # marked, so plots runs don't re-fetch it every time (QA nit 5); --refresh retries
            return {**skipped, "season_articles": {"status": "disabled"}}
        found = SeasonFinder(self, cap=self.season_word_cap).find(
            skipped["qid"], skipped["page_title"], int(skipped["page_id"]),
            int(skipped["revision"]),
        )
        main = {k: skipped.get(k) for k in ("skip_reason", "skip_detail", "section",
                                            "word_count", "revision", "permalink",
                                            "section_checks")}
        if found.run_rule is not None:
            # a per-title series run rule (fetcher 1.5.1, runs.py): recorded on the file,
            # whether the title ends ok or skipped
            skipped = {**skipped, "series_run_rule": found.run_rule.name,
                       "series_run_ignored": found.run_ignored}
        report = {
            "used": [t.page.title for t in found.texts],
            # Wikidata evidence that placed each used page in this series (DECISIONS
            # 2026-10-02): its item and the matched P179/P361 statement
            "evidence": [
                {"title": t.page.title, **t.page.evidence.record()}
                for t in found.texts if t.page.evidence is not None
            ],
            "verified_seasons": found.verified_seasons,
            "skipped": found.skipped,
            "left_out_over_cap": found.left_out_over_cap,
            "used_list_page": found.used_list_page,
            "word_cap": self.season_word_cap,
            "lead_block_ceiling": LEAD_BLOCK_CEILING,  # "season_one_ceiling" before 1.4.0
            "stub_season_words": STUB_SEASON_WORDS,
        }
        if found.too_long is not None:
            # the prose is too long to use; the episode tables may still be (DECISIONS
            # 2026-10-02, QA S2). Stays season_too_long when they don't give 150 words.
            return self._try_episode_tables({
                **skipped,
                "skip_reason": SKIP_SEASON_TOO_LONG,
                "skip_detail": f"first full season (no stub seasons before it) is over "
                f"{LEAD_BLOCK_CEILING} words: "
                f"{found.too_long}",
                "season_articles": report,
            }, main, found)
        if found.words < self.min_words:
            detail = (
                f"{skipped.get('skip_detail') or skipped['skip_reason']}; season articles: "
                f"{len(found.texts)} usable with {found.words} words"
            )
            return self._try_episode_tables(
                {**skipped, "skip_detail": detail, "season_articles": report}, main, found
            )
        coverage = season_coverage(
            [t.page.season for t in found.texts], found.verified_seasons,
            (skipped.get("candidate") or {}).get("number_of_seasons"),
            use_wikidata_total=found.run_rule is None,
        )
        return self._ok_seasons(skipped, main, found, report, coverage)

    # ---------- series: episode tables (fetcher 1.5.0) ----------

    def _collect_episode_seasons(
        self, found: SeasonResult, report: dict[str, Any]
    ) -> tuple[list[tuple[Page, SeasonEpisodes]], list[int]]:
        """Seasons with episode summaries, in season order, each with its page; plus the season
        numbers an episode-list page names (for the season total). Season pages are fetched one
        at a time and only until their episodes pass the cap (later pages could not be used);
        the episode-list page only when no season page has any summary."""
        cap = self.season_word_cap
        collected: list[tuple[Page, SeasonEpisodes]] = []
        words = 0
        # a run rule numbering by year (CID, fetcher 1.5.5): list-page year headings are the units
        unit = YEAR_UNIT if found.run_rule and found.run_rule.numbering == YEAR_UNIT else "season"
        for i, page in enumerate(found.season_pages):
            if words > cap or len(collected) >= MAX_SEASON_SOURCES:
                report["pages_not_fetched"] = [p.title for p in found.season_pages[i:]]
                break
            assert page.season is not None
            tables = parse_tables(self.page_html(page.revid))
            used, skipped_tables = season_page_tables(tables)
            report["tables_skipped"] += [{"title": page.title, **t} for t in skipped_tables]
            se = season_episodes(page.season, used)
            report["episodes_without_summary"] += se.without_summary
            if not se.episodes:
                report["pages_skipped"].append({
                    "title": page.title,
                    "reason": "no episode summaries in its episode tables" if used
                    else "no episode table"})
                continue
            collected.append((page, se))
            words += sum(e.words for e in se.episodes)
        if collected or not found.list_pages:
            report["page_kind"] = "season_pages" if collected else None
            return collected, []
        # Episode-list pages in order (the plain page, then pages split by year, season range
        # or part); seasons must keep rising across pages. Fetched one at a time, until the
        # summaries pass the cap.
        listed: list[int] = []  # every season heading seen, for the season total only
        used: list[int] = []  # seasons with summaries, in order: these must keep rising
        listed_before: list[int] = []  # season headings on earlier pages (restart check)
        for i, page in enumerate(found.list_pages):
            if (words > cap or len(collected) >= MAX_SEASON_SOURCES) and unit == YEAR_UNIT:
                # years: the later pages' headings still set the span the coverage line
                # states ("of 1998–2025"), so they are read for headings only, never for text
                years, _ = list_page_seasons(parse_tables(self.page_html(page.revid)),
                                             found.run_rule)
                listed += [n for n, _ in years]
                report.setdefault("pages_read_for_years_only", []).append(page.title)
                continue
            if words > cap or len(collected) >= MAX_SEASON_SOURCES:
                report["pages_not_fetched"] = [p.title for p in found.list_pages[i:]]
                break
            seasons, skipped_tables = list_page_seasons(
                parse_tables(self.page_html(page.revid)), found.run_rule)
            report["tables_skipped"] += [{"title": page.title, **t} for t in skipped_tables]
            used_here = 0
            for n, tables in seasons:
                if unit != YEAR_UNIT and n > MAX_SEASON_NUMBER:  # years: checked by heading
                    report["tables_skipped"].append({"title": page.title,
                                                     "heading": f"season {n}",
                                                     "reason": "season number over 100"})
                    continue
                if used and n <= used[-1]:
                    report["tables_skipped"].append({
                        "title": page.title, "heading": f"season {n}",
                        "reason": f"season {n} after season {used[-1]} on an earlier page"})
                    continue
                se = season_episodes(n, tables, unit)
                report["episodes_without_summary"] += se.without_summary
                if se.episodes and listed_before and n <= max(listed_before):
                    # an earlier page (without summaries) already numbered a season this high:
                    # two numbering runs (a revival restarting at "Series 1"). Which run "of N"
                    # counts is not clear, so these seasons are not used (QA S4, for Hari).
                    report["numbering_restart"].append({
                        "title": page.title, "season": n,
                        "earlier_pages_up_to": max(listed_before)})
                    continue
                listed.append(n)
                if se.episodes:
                    collected.append((page, se))
                    used.append(n)
                    words += sum(e.words for e in se.episodes)
                    used_here += 1
            listed_before += [n for n, _ in seasons]
            if not used_here:
                report["pages_skipped"].append({"title": page.title,
                                                "reason": "no episode summaries under a season "
                                                "heading"})
        if collected:
            report["page_kind"] = "episode_list_page"
        return collected, listed

    def _try_episode_tables(
        self, skipped: dict[str, Any], main: dict[str, Any], found: SeasonResult
    ) -> dict[str, Any]:
        """The season-article prose failed too; join the per-episode summaries from the episode
        tables of the same verified pages (``episodes.py``), else stay skipped."""
        report: dict[str, Any] = {
            "page_kind": None,
            "pages_used": [],
            "evidence": [],
            "episodes_used": 0,
            "by_season": [],
            "partial_season": None,
            "left_out_over_cap": {"episodes": 0, "seasons": []},
            "pages_not_fetched": [],
            "stopped_by": None,
            "partial_reason": None,
            "numbering_restart": [],
            "episodes_without_summary": 0,
            "pages_skipped": [],
            "tables_skipped": [],
            "word_cap": self.season_word_cap,
        }
        collected, listed = self._collect_episode_seasons(found, report)
        joined = join_episodes([se for _, se in collected], self.season_word_cap,
                               MAX_SEASON_SOURCES)
        page_for = {se.season: page for page, se in collected}
        available = {se.season: len(se.episodes) for _, se in collected}
        # trivia of every season read (1.5.8): stored with its source, never in the text
        trivia = [{"episode": t.marker, "rule": t.rule, "text": t.text,
                   "words": word_count(t.text), "ref": article_url(page.title),
                   "revision": str(page.revid), "license": license_for(page.timestamp),
                   "retrieved_at": skipped["retrieved_at"]}
                  for page, se in collected for t in se.trivia]
        report["trivia"] = {"items": len(trivia), "words": sum(t["words"] for t in trivia),
                            "by_rule": dict(Counter(t["rule"] for t in trivia))}
        used_pages: list[Page] = []
        for s in joined.seasons:
            if page_for[s.season] not in used_pages:
                used_pages.append(page_for[s.season])
        report.update(
            pages_used=[p.title for p in used_pages],
            evidence=[{"title": p.title, **p.evidence.record()}
                      for p in used_pages if p.evidence is not None],
            episodes_used=sum(len(s.episodes) for s in joined.seasons),
            by_season=[{"season": s.season, "episodes": len(s.episodes),
                        "with_summary": available[s.season]} for s in joined.seasons],
            partial_season=joined.partial_season,
            partial_reason=joined.partial_reason,
            left_out_over_cap={"episodes": joined.left_out_episodes,
                               "seasons": joined.left_out_seasons},
            stopped_by=joined.stopped_by,
        )
        if joined.words < self.min_words:
            restart = (" (season numbering restarts across list pages; not used)"
                       if report["numbering_restart"] else "")
            detail = (f"{skipped['skip_detail']}; episode tables: "
                      f"{report['episodes_used']} episodes with {joined.words} words{restart}")
            return {**skipped, "skip_detail": detail[:300], "episode_tables": report}
        if found.run_rule is not None and found.run_rule.numbering == YEAR_UNIT:
            report["unit"] = YEAR_UNIT
            coverage = year_coverage([s.season for s in joined.seasons], listed,
                                     joined.partial_season)
            return self._ok_episodes(skipped, main, joined, page_for, report, coverage, trivia)
        coverage = season_coverage(
            [s.season for s in joined.seasons], sorted({*found.verified_seasons, *listed}),
            (skipped.get("candidate") or {}).get("number_of_seasons"),
            use_wikidata_total=found.run_rule is None,
        )
        assert coverage is not None  # every episode-table source has a season number
        if joined.partial_season is not None:
            coverage.update(partial_season=joined.partial_season, partial=True)
        return self._ok_episodes(skipped, main, joined, page_for, report, coverage, trivia)

    def _ok_episodes(
        self,
        result: dict[str, Any],
        main: dict[str, Any],
        joined: EpisodeJoin,
        page_for: dict[int, Page],
        report: dict[str, Any],
        coverage: dict[str, Any],
        trivia: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        parts = []
        unit_key = "year" if coverage.get("unit") == YEAR_UNIT else "season"
        for s in joined.seasons:
            page = page_for[s.season]
            text = season_text(s)
            parts.append({
                "page_title": page.title,
                "permalink": permalink(page.title, page.revid),
                "via_detail": VIA_DETAIL_EPISODE_TABLE,
                "episodes": [e.marker for e in s.episodes],
                "source": {
                    "kind": "wikipedia_plot",
                    "ref": article_url(page.title),
                    "revision": str(page.revid),
                    "retrieved_at": result["retrieved_at"],
                    "license": license_for(page.timestamp),
                    "word_count": word_count(text),
                    "content_sha256": sha256_text(text),
                    unit_key: s.season,
                },
                "text": text,
            })
        require_wikipedia_sources([p["source"] for p in parts])  # fail closed, every page
        out = {k: v for k, v in result.items()
               if k not in ("skip_detail", "word_count", "section_checks")}
        return {
            **out,
            "status": "ok",
            "skip_reason": None,
            "skip_detail": None,
            "via": VIA_SEASON_ARTICLES,
            "via_detail": VIA_DETAIL_EPISODE_TABLE,
            "main_article": main,
            "section": {"heading": "episode tables", "index": None},
            "word_count": sum(p["source"]["word_count"] for p in parts),
            "sources": parts,
            "episode_tables": report,
            "coverage": coverage,
            # trivia moved out of the episode summaries (1.5.8, DECISIONS 2026-10-02): kept for
            # a future trivia feature; never part of ``sources`` text, the model or gold input
            "trivia": trivia or [],
        }

    def _ok_seasons(
        self,
        result: dict[str, Any],
        main: dict[str, Any],
        found: SeasonResult,
        report: dict[str, Any],
        coverage: dict[str, Any] | None,
    ) -> dict[str, Any]:
        parts = []
        for t in found.texts:
            source: dict[str, Any] = {
                "kind": "wikipedia_plot",
                "ref": article_url(t.page.title),
                "revision": str(t.page.revid),
                "retrieved_at": result["retrieved_at"],
                "license": license_for(t.page.timestamp),
                "word_count": t.words,
                "content_sha256": sha256_text(t.text),
            }
            if t.page.season is not None:
                source["season"] = t.page.season
            parts.append({
                "page_title": t.page.title,
                "permalink": permalink(t.page.title, t.page.revid),
                "sections": t.headings,
                "section_checks": t.checks,
                "source": source,
                "text": t.text,
            })
        require_wikipedia_sources([p["source"] for p in parts])  # fail closed, every page
        out = {k: v for k, v in result.items()
               if k not in ("skip_detail", "word_count", "section_checks")}
        extra = {"coverage": coverage} if coverage is not None else {}
        return {
            **out,
            "status": "ok",
            "skip_reason": None,
            "skip_detail": None,
            "via": VIA_SEASON_ARTICLES,
            "main_article": main,
            "section": {"heading": "season articles", "index": None},
            "word_count": found.words,
            "sources": parts,
            "season_articles": report,
            **extra,
        }

    def _ok(
        self,
        result: dict[str, Any],
        page_title: str,
        revid: int,
        rev_ts: str,
        section: Section,
        filtered: SectionFilter,
    ) -> dict[str, Any]:
        text, words = filtered.text, filtered.words
        source = {
            "kind": "wikipedia_plot",
            "ref": article_url(page_title),
            "revision": str(revid),
            "retrieved_at": result["retrieved_at"],
            "license": license_for(rev_ts),
            "word_count": words,
            "content_sha256": sha256_text(text),
        }
        require_wikipedia_sources([source])  # fail closed: same gate as the prompt builder
        return {
            **result,
            "status": "ok",
            "section": {"heading": section.heading, "index": section.index,
                        "dropped": filtered.dropped},
            "word_count": words,
            "source": source,
            "text": text,
        }

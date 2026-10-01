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
has at least ``MIN_WORDS`` words. Titles with no such section are skipped with a recorded reason
and never annotated (DECISIONS 2026-09-26: 150-word minimum; never guess from a title).

Series fallback (DECISIONS 2026-10-01): when a series' main article is too short or has no plot
section, its verified per-season articles are tried (``seasons.py``). Such a record has
``via: "season_articles"`` and a ``sources`` list (one entry per season article, each with its
own schema-shaped ``source`` and ``text``) instead of the single ``source`` and ``text``; the
main article's result is kept under ``main_article``.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from laminary_pipeline.annotation import require_wikipedia_sources
from laminary_pipeline.ingest.http import HttpClient, HttpError
from laminary_pipeline.ingest.seasons import (
    SEASON_ONE_CEILING,
    SEASON_WORD_CAP,
    STUB_SEASON_WORDS,
    SeasonFinder,
    SeasonResult,
)
from laminary_pipeline.ingest.text import html_to_text, sha256_text, word_count

FETCHER_VERSION = "1.2.0"  # 1.1.0: per-season articles for thin series; 1.2.0: lead block
# of stub seasons (under STUB_SEASON_WORDS, 500) before the first full season
MIN_WORDS = 150
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
SKIP_SEASON_TOO_LONG = "season_too_long"  # season lead block over SEASON_ONE_CEILING
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


def normalize_heading(line: str) -> str:
    text = re.sub(r"<[^>]+>", "", line)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text.rstrip(":.")


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

    def section_text(self, revid: int, index: str) -> str:
        data = self._api(
            {
                "action": "parse", "oldid": str(revid), "section": index, "prop": "text",
                "disableeditsection": "1", "disablelimitreport": "1", "disabletoc": "1",
            },
            cache_ttl=None,
        )
        return html_to_text(data["parse"]["text"])

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
                          "tmdb_id", "imdb_id", "series_status", "series_status_basis")
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
        for section in candidates:
            text = self.section_text(revid, section.index)
            words = word_count(text)
            if words >= self.min_words:
                return self._ok(result, page_title, revid, rev_ts, section, text, words)
            if best is None or words > best[0]:
                best = (words, section)
        assert best is not None
        words, section = best
        skipped = {
            **result,
            "skip_reason": SKIP_TOO_SHORT,
            "skip_detail": f"longest plot-like section {section.heading!r} has {words} words",
            "section": {"heading": section.heading, "index": section.index},
            "word_count": words,
        }
        return self._try_seasons(skipped, media_type)

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
                                            "word_count", "revision", "permalink")}
        report = {
            "used": [t.page.title for t in found.texts],
            "skipped": found.skipped,
            "left_out_over_cap": found.left_out_over_cap,
            "used_list_page": found.used_list_page,
            "word_cap": self.season_word_cap,
            "season_one_ceiling": SEASON_ONE_CEILING,
            "stub_season_words": STUB_SEASON_WORDS,
        }
        if found.too_long is not None:
            return {
                **skipped,
                "skip_reason": SKIP_SEASON_TOO_LONG,
                "skip_detail": f"first full season (with any stub seasons before it) is over "
                f"{SEASON_ONE_CEILING} words: "
                f"{found.too_long}"[:300],
                "season_articles": report,
            }
        if found.words < self.min_words:
            detail = (
                f"{skipped.get('skip_detail') or skipped['skip_reason']}; season articles: "
                f"{len(found.texts)} usable with {found.words} words"
            )
            return {**skipped, "skip_detail": detail[:300], "season_articles": report}
        return self._ok_seasons(skipped, main, found, report)

    def _ok_seasons(
        self,
        result: dict[str, Any],
        main: dict[str, Any],
        found: SeasonResult,
        report: dict[str, Any],
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
                "source": source,
                "text": t.text,
            })
        require_wikipedia_sources([p["source"] for p in parts])  # fail closed, every page
        out = {k: v for k, v in result.items() if k not in ("skip_detail", "word_count")}
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
        }

    def _ok(
        self,
        result: dict[str, Any],
        page_title: str,
        revid: int,
        rev_ts: str,
        section: Section,
        text: str,
        words: int,
    ) -> dict[str, Any]:
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
            "section": {"heading": section.heading, "index": section.index},
            "word_count": words,
            "source": source,
            "text": text,
        }
